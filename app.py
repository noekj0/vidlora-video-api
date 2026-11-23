"""Vidlora optional self-hosted resolver for authorized, public media.

This is an example service. It does not bypass logins, DRM, private content,
creator restrictions, or remove embedded watermarks. Use only with content you
are permitted to save, and review platform terms before activating it.
"""
import asyncio
import base64
from collections import defaultdict, deque
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sys
import tempfile
import time
from urllib.parse import urlsplit, parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.background import BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import yt_dlp


PUBLIC_URL = os.getenv('VIDLORA_PUBLIC_URL', '').rstrip('/')
ALLOWED_ORIGINS = [v.strip() for v in os.getenv('VIDLORA_ALLOWED_ORIGINS', 'https://vidlora.download,https://www.vidlora.download,http://localhost:8765').split(',') if v.strip()]
ENABLE_SOCIAL = os.getenv('VIDLORA_ENABLE_SOCIAL', 'false').lower() == 'true'
MAX_VIDEO_BYTES = max(5, min(200, int(os.getenv('VIDLORA_MAX_VIDEO_MB', '80')))) * 1024 * 1024
SECRET = os.getenv('VIDLORA_SIGNING_KEY') or secrets.token_urlsafe(48)

app = FastAPI(title='Vidlora Video API', docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=False, allow_methods=['POST', 'GET'], allow_headers=['Content-Type'])

# Starter-only in-process limits. Replace with a distributed limiter, abuse protection,
# and storage quotas before running this as a high-traffic public service.
_limiter: dict[str, deque] = defaultdict(deque)
_download_lock = asyncio.Semaphore(2)
_metadata_lock = asyncio.Semaphore(2)


async def run_worker(action: str, url: str, folder: Path, timeout: float):
    """Own the entire process tree until it exits, including on cancellation."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).with_name('worker.py')), action, url,
        str(folder), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name != 'nt',
    )
    async def watch():
        while process.returncode is None:
            # Allow room for separate tracks and merging, but bound temporary disk use.
            size = 0
            for path in folder.glob('*'):
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except FileNotFoundError:
                    pass  # yt-dlp may rename a completed fragment between checks.
            if size > MAX_VIDEO_BYTES * 3:
                raise ValueError('Temporary storage limit exceeded')
            await asyncio.sleep(.25)
    monitor = asyncio.create_task(watch())
    communication = asyncio.create_task(process.communicate())
    try:
        done, _ = await asyncio.wait([communication, monitor], timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if communication not in done:
            if monitor in done:
                monitor.result()
            raise TimeoutError('Source took too long to respond')
        stdout, stderr = communication.result()
        if process.returncode:
            logging.warning('Video worker failed: %s', stderr.decode(errors='replace')[-1500:])
            raise ValueError('Source could not provide this public video')
        return json.loads(stdout)
    finally:
        if process.returncode is None:
            if os.name == 'nt':
                killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(process.pid), '/T', '/F',
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await killer.wait()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await process.wait()
        monitor.cancel()
        await asyncio.gather(monitor, communication, return_exceptions=True)


class TemporaryVideoResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            shutil.rmtree(self.path.parent, ignore_errors=True)
            _download_lock.release()

class ResolveRequest(BaseModel):
    url: str = Field(min_length=12, max_length=2000)
    source: str = Field(default='', max_length=30)
    authorized: bool = False


def get_platform(url: str) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise HTTPException(400, detail='Invalid video URL.')
    if parts.scheme != 'https' or parts.username or parts.password or port not in (None, 443):
        raise HTTPException(400, detail='Public HTTPS links only.')
    host = (parts.hostname or '').lower().rstrip('.')
    allowed = {
        'youtube': ('youtube.com', 'youtu.be'),
        'instagram': ('instagram.com',),
        'tiktok': ('tiktok.com',),
    }
    for platform, domains in allowed.items():
        if any(host == d or host.endswith('.' + d) for d in domains):
            path = parts.path.rstrip('/')
            if platform == 'youtube':
                video_id = path[1:] if host == 'youtu.be' else (
                    parse_qs(parts.query).get('v', [''])[0] if path == '/watch' else
                    path.split('/')[-1] if path.startswith(('/shorts/', '/embed/')) else '')
                valid = bool(re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id))
            elif platform == 'instagram':
                valid = bool(re.fullmatch(r'/(?:p|reel|tv)/[A-Za-z0-9_-]+', path))
            else:
                valid = bool(re.fullmatch(r'/@[^/]+/video/\d+', path) or
                             (host in ('vm.tiktok.com', 'vt.tiktok.com') and re.fullmatch(r'/[A-Za-z0-9]+', path)) or
                             re.fullmatch(r'/t/[A-Za-z0-9]+', path))
            if not valid:
                raise HTTPException(400, detail='Paste an individual public video link, rather than a profile or playlist.')
            return platform
    raise HTTPException(400, detail='Only supported public video-platform links are accepted by this backend.')


def enforce_rate_limit(key: str, count: int, window: int):
    now = time.monotonic()
    for stale in [k for k, values in _limiter.items() if not values or values[-1] < now - 3600]:
        del _limiter[stale]
    if key not in _limiter and len(_limiter) >= 10000:
        raise HTTPException(503, detail='Server busy. Try again later.')
    q = _limiter[key]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= count:
        raise HTTPException(429, detail='Too many requests. Try again later.')
    q.append(now)


def extractor_options() -> dict:
    return {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'skip_download': True,
        'socket_timeout': 10,
        'retries': 1,
        'fragment_retries': 1,
        'extractor_retries': 1,
        'playlistend': 1,
        'js_runtimes': {'node': {}},
        'ignoreerrors': False,
    }


def get_metadata(url: str):
    with yt_dlp.YoutubeDL(extractor_options()) as downloader:
        info = downloader.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise ValueError('No downloadable information returned by source.')
    if info.get('_type') == 'playlist':
        raise ValueError('Only individual video links are supported.')
    if info.get('is_live') or info.get('has_drm') or info.get('availability') in ('private', 'premium_only', 'subscriber_only', 'needs_auth'):
        raise ValueError('Only public, unrestricted videos are supported.')
    return info


def sign_url(url: str, max_age: int = 600):
    payload = {'u': url, 'e': int(time.time()) + max_age}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
    signature = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + '.' + signature


def unsign(token: str):
    try:
        raw, signature = token.rsplit('.', 1)
        expected = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError('invalid signature')
        data = json.loads(base64.urlsafe_b64decode(raw + ('=' * (-len(raw) % 4))))
        if int(data['e']) < int(time.time()):
            raise ValueError('expired')
        url = data['u']
        get_platform(url)
        return url
    except Exception:
        raise HTTPException(403, detail='Download link expired or invalid.')


def enforce_video_size(progress):
    if progress.get('status') == 'downloading' and (progress.get('downloaded_bytes') or 0) > MAX_VIDEO_BYTES:
        raise yt_dlp.utils.DownloadError('Maximum permitted file size exceeded')


def download_video(url: str, folder: Path):
    opts = extractor_options()
    opts.update({
        'skip_download': False,
        'format': 'b[ext=mp4][height<=720]/bv[ext=mp4][height<=720]+ba[ext=m4a]',
        'outtmpl': str(folder / 'vidlora.%(ext)s'),
        'restrictfilenames': True,
        'max_filesize': MAX_VIDEO_BYTES,
        'progress_hooks': [enforce_video_size],
        'merge_output_format': 'mp4',
        'continuedl': False,
        'overwrites': False,
    })
    with yt_dlp.YoutubeDL(opts) as downloader:
        info = downloader.extract_info(url, download=False)
        if not isinstance(info, dict) or info.get('_type') == 'playlist' or info.get('is_live') or info.get('has_drm'):
            raise ValueError('Only individual public videos are supported.')
        downloader.process_ie_result(info, download=True)
    path = folder / 'vidlora.mp4'
    if not path.is_file():
        raise ValueError('No completed video file could be downloaded from this source.')
    if path.stat().st_size > MAX_VIDEO_BYTES:
        raise ValueError('This video is too large for this service.')
    return path


@app.get('/health')
def health():
    return {'ok': True, 'downloads_enabled': ENABLE_SOCIAL}


@app.post('/api/resolve')
async def resolve(data: ResolveRequest, request: Request):
    if not ENABLE_SOCIAL:
        raise HTTPException(503, detail='Social video processing has not been enabled by the site owner.')
    if not data.authorized:
        raise HTTPException(400, detail='Confirm you own this video or have permission to save it.')
    get_platform(data.url)
    enforce_rate_limit('resolve:' + (request.client.host if request.client else ''), 8, 600)
    try:
        await asyncio.wait_for(_metadata_lock.acquire(), timeout=1)
    except TimeoutError:
        raise HTTPException(503, detail='Server busy. Try again shortly.')
    folder = Path(tempfile.mkdtemp(prefix='vidlora-metadata-'))
    try:
        info = await run_worker('metadata', data.url, folder, 35)
    except Exception as ex:
        raise HTTPException(422, detail='Unable to resolve this public video. The source may require authorization or restrict downloads.') from ex
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        _metadata_lock.release()
    if info.get('is_live'):
        raise HTTPException(422, detail='Live streams are not supported.')
    if info.get('filesize') and info['filesize'] > MAX_VIDEO_BYTES:
        raise HTTPException(422, detail='Video is too large for this service.')
    token = sign_url(data.url)
    # For the public Render URL, build a secure link from the actual assigned hostname.
    # A custom domain should be configured explicitly using VIDLORA_PUBLIC_URL.
    if PUBLIC_URL:
        public_base = PUBLIC_URL
    else:
        host = (request.url.hostname or '').lower()
        if host.endswith('.onrender.com'):
            public_base = 'https://' + host
        elif host in ('localhost', '127.0.0.1'):
            public_base = str(request.base_url).rstrip('/')
        else:
            raise HTTPException(503, detail='Public API hostname not configured. Set VIDLORA_PUBLIC_URL.')
    return {
        'title': str(info.get('title') or 'Video').strip()[:150],
        # The service does not expose remote thumbnails or personal data by default.
        'formats': [{'label': 'Best available video (up to 720p)', 'url': public_base + '/api/download/' + token, 'ext': 'mp4'}],
    }


@app.get('/api/download/{token}')
async def download(token: str, request: Request, background_tasks: BackgroundTasks):
    if not ENABLE_SOCIAL:
        raise HTTPException(503, detail='Social video processing disabled.')
    url = unsign(token)
    enforce_rate_limit('download:' + (request.client.host if request.client else ''), 4, 3600)
    try:
        await asyncio.wait_for(_download_lock.acquire(), timeout=1)
    except TimeoutError:
        raise HTTPException(503, detail='Server busy. Try again shortly.')
    folder = Path(tempfile.mkdtemp(prefix='vidlora-'))
    try:
        await run_worker('download', url, folder, 240)
        path = folder / 'vidlora.mp4'
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_VIDEO_BYTES:
            raise ValueError('No completed video within the size limit')
        media_type = 'video/mp4' if path.suffix.lower() == '.mp4' else 'application/octet-stream'
        return TemporaryVideoResponse(path, media_type=media_type, filename='vidlora-video'+path.suffix)
    except BaseException as ex:
        shutil.rmtree(folder, ignore_errors=True)
        _download_lock.release()
        if not isinstance(ex, Exception):
            raise
        raise HTTPException(422, detail='Download failed or source does not permit this file.') from ex
