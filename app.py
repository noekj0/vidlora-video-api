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
import html
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
import subprocess
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote
from urllib.parse import urlsplit, parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.background import BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import yt_dlp
from qualities import discover_qualities, choose_exact_quality


PUBLIC_URL = os.getenv('VIDLORA_PUBLIC_URL', '').rstrip('/')
ALLOWED_ORIGINS = [v.strip() for v in os.getenv('VIDLORA_ALLOWED_ORIGINS', 'https://vidlora.download,https://www.vidlora.download,http://localhost:8765').split(',') if v.strip()]
ENABLE_SOCIAL = os.getenv('VIDLORA_ENABLE_SOCIAL', 'false').lower() == 'true'
MAX_VIDEO_BYTES = max(5, min(10240, int(os.getenv('VIDLORA_MAX_DOWNLOAD_MB', '300')))) * 1024 * 1024
TEMP_STORAGE_BYTES = max(128, int(os.getenv('VIDLORA_TEMP_STORAGE_MB', '768'))) * 1024 * 1024
SECRET = os.getenv('VIDLORA_SIGNING_KEY') or secrets.token_urlsafe(48)
VIDEO_FORMAT = 'bestvideo*+bestaudio/best'


@asynccontextmanager
async def lifespan(application):
    async def collect():
        while True:
            await asyncio.sleep(30)
            prune_downloads()
    collector = asyncio.create_task(collect())
    try:
        yield
    finally:
        collector.cancel()
        tasks = [j.task for j in _downloads.values() if j.task and not j.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(collector, *tasks, return_exceptions=True)
        for job in _downloads.values():
            shutil.rmtree(job.folder, ignore_errors=True)
        _downloads.clear()

app = FastAPI(title='Vidlora Video API', docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=False,
    allow_methods=['POST', 'GET', 'HEAD'], allow_headers=['Content-Type', 'Range', 'If-Range'],
    expose_headers=['Content-Disposition', 'Content-Length', 'Content-Range', 'Accept-Ranges', 'ETag'])


@app.exception_handler(HTTPException)
async def readable_download_error(request: Request, error: HTTPException):
    # Existing website links open the API in a browser tab. Keep API clients on
    # JSON, but give visitors a useful page rather than a raw JSON document.
    if request.url.path.startswith(('/api/download/', '/api/file/')) and 'text/html' in request.headers.get('accept', ''):
        message = html.escape(str(error.detail))
        return HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Vidlora download</title><style>body{font:18px/1.6 system-ui;'
            'background:#f3f7ff;color:#142145;max-width:620px;margin:12vh auto;padding:24px}'
            'a{color:#087aff}h1{line-height:1.2}</style><h1>Video could not be downloaded</h1>'
            f'<p>{message}</p><p><a href="https://vidlora.download/#online">'
            'Return to Vidlora and check the link again</a></p></html>',
            status_code=error.status_code, headers=error.headers,
        )
    return JSONResponse({'detail': error.detail}, status_code=error.status_code, headers=error.headers)

# Starter-only in-process limits. Replace with a distributed limiter, abuse protection,
# and storage quotas before running this as a high-traffic public service.
_limiter: dict[str, deque] = defaultdict(deque)
_download_lock = asyncio.Semaphore(1)
_metadata_lock = asyncio.Semaphore(1)


class SourceBlocked(ValueError):
    pass


class VideoSizeLimit(ValueError):
    pass


BLOCKED_MESSAGE = 'This video platform is blocking requests from our hosting server. This video cannot be downloaded here right now.'


async def run_worker(action: str, url: str, folder: Path, timeout: float):
    """Own the entire process tree until it exits, including on cancellation."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).with_name('worker.py')), action, url,
        str(folder), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name != 'nt',
    )
    async def watch():
        while True:
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
            other = sum(folder_size(j.folder) for j in _downloads.values() if j.folder != folder)
            if size + other > TEMP_STORAGE_BYTES:
                raise VideoSizeLimit('The selected download exceeds the available temporary storage.')
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
            failure = stderr.decode(errors='replace').lower()
            if 'not a bot' in failure:
                raise SourceBlocked('YouTube requires a sign-in check from this hosting server. Please try again later.')
            if 'http error 403' in failure:
                raise SourceBlocked('The video source rejected the media file with HTTP 403. Please try another permitted video.')
            if any(message in failure for message in ('videosizelimit', 'no format fits the server size limit', 'maximum permitted file size exceeded')):
                raise VideoSizeLimit('No available video quality fits the server size limit.')
            if process.returncode == -9:
                raise ValueError('The server ran out of resources while preparing this video.')
            raise ValueError('Source could not provide this public video')
        return json.loads((folder / 'result.json').read_text(encoding='utf-8'))
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


class CachedVideoResponse(FileResponse):
    def __init__(self, job):
        self.job = job
        fallback = unicodedata.normalize('NFKD', job.filename).encode('ascii', 'ignore').decode()
        if not fallback.rsplit('.', 1)[0].strip(' .-'):
            fallback = 'video.' + job.ext
        disposition = f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(job.filename, safe="")}'
        super().__init__(job.folder / ('vidlora.' + job.ext), media_type='video/' + job.ext, headers={
            'Content-Disposition': disposition, 'Cache-Control': 'private, max-age=3600',
            'X-Content-Type-Options': 'nosniff',
        })

    async def __call__(self, scope, receive, send):
        self.job.readers += 1
        self.job.last_access = time.time()
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.job.readers -= 1
            self.job.last_access = time.time()

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
        'noprogress': True,
        'no_warnings': True,
        'noplaylist': True,
        'skip_download': True,
        'format': VIDEO_FORMAT,
        # Probe selected streams before offering them. Metadata alone can succeed
        # even when the source rejects the actual media URL with HTTP 403.
        'check_formats': 'selected',
        'socket_timeout': 10,
        'retries': 1,
        'fragment_retries': 1,
        'extractor_retries': 1,
        'playlistend': 1,
        'js_runtimes': {'node': {}},
        'ignoreerrors': False,
        **({'ffmpeg_location': os.environ['VIDLORA_FFMPEG_PATH']} if os.getenv('VIDLORA_FFMPEG_PATH') else {}),
    }


def format_within_budget(info):
    """Choose a real stream or pair whose estimated combined size fits storage."""
    formats = info.get('formats') or []
    budget = MAX_VIDEO_BYTES * .97  # Leave room for MP4 container overhead.
    def size(f):
        return f.get('filesize') or f.get('filesize_approx') or float('inf')
    converted_audio = (info.get('duration') or 0) * audio_bitrate(info) * 1000 / 8
    audio = sorted([f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') not in (None, 'none')
                    and f.get('ext') in ('m4a', 'webm')], key=lambda f: f.get('abr') or f.get('tbr') or 0, reverse=True)
    choices = []
    for index, video in enumerate(formats):
        if video.get('ext') != 'mp4' or not str(video.get('vcodec') or '').startswith(('avc', 'h264')) or video.get('has_drm'):
            continue
        height = video.get('height') or 0
        if height > 720:
            continue
        if video.get('acodec') not in (None, 'none'):
            if size(video) <= budget:
                choices.append((height, index, str(video['format_id'])))
        else:
            for track in audio:
                if size(video) + max(size(track), converted_audio) <= budget:
                    choices.append((height, index, str(video['format_id']) + '+' + str(track['format_id'])))
    if choices:
        # A source can advertise a stream but reject its media URL. Let yt-dlp
        # probe a bounded list and fall through to another real combination.
        ordered = sorted(choices, key=lambda c: (c[0], c[1]), reverse=True)
        return '/'.join(list(dict.fromkeys(c[2] for c in ordered))[:8])
    if formats and all(size(f) == float('inf') for f in formats):
        return VIDEO_FORMAT  # Some platforms do not advertise sizes; enforce while downloading.
    raise VideoSizeLimit('No format fits the server size limit.')


def audio_bitrate(info):
    return 128


def safe_filename(title, ext='mp4'):
    title = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', ' ', str(title or 'Video'))
    title = ''.join(c for c in title if not unicodedata.category(c).startswith('C'))
    title = re.sub(r'\s+', ' ', title).strip(' .')[:140].rstrip(' .')
    for suffix in ('.mp4', '.webm'):
        if title.lower().endswith(suffix):
            title = title[:-len(suffix)].rstrip(' .')
            break
    while len(title.encode('utf-8')) > 200:
        title = title[:-1]
    return (title or 'Video') + '.' + ext


def get_metadata(url: str):
    options = extractor_options()
    options['check_formats'] = False  # Inspect sizes before probing candidate media.
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise ValueError('No downloadable information returned by source.')
    if info.get('_type') == 'playlist':
        raise ValueError('Only individual video links are supported.')
    if info.get('is_live') or info.get('has_drm') or info.get('availability') in ('private', 'premium_only', 'subscriber_only', 'needs_auth'):
        raise ValueError('Only public, unrestricted videos are supported.')
    info['qualities'] = discover_qualities(info, min(MAX_VIDEO_BYTES, int(TEMP_STORAGE_BYTES / 2.2)))
    return info


def sign_url(url: str, max_age: int = 3600, title: str = '', quality=None):
    payload = {'u': url, 'e': int(time.time()) + max_age, 't': str(title)[:150]}
    if quality is not None:
        payload['q'] = {k: quality.get(k) for k in ('height', 'width', 'fps', 'range', 'profile', 'estimated_bytes', 'ext', 'label')}
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


def download_video(url: str, folder: Path, selected=None):
    opts = extractor_options()
    opts.update({
        'skip_download': False,
        'check_formats': False,
        'format': VIDEO_FORMAT,
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
        choice = choose_exact_quality(info, selected, MAX_VIDEO_BYTES) if selected else None
        if choice and not choice['downloadable']:
            raise VideoSizeLimit('The selected quality exceeds the current hosting capacity.')
        output_ext = choice['ext'] if choice else 'mp4'
        profile = choice['profile'] if choice else 'iphone'
        downloader.params['merge_output_format'] = output_ext
        downloader.params['check_formats'] = 'selected'
        downloader.format_selector = downloader.build_format_selector(choice['selector'] if choice else format_within_budget(info))
        info = downloader.process_ie_result(info, download=True)
        expected = Path(downloader.prepare_filename(info))
    path = folder / ('vidlora.' + output_ext)
    if not path.is_file() and expected.is_file() and expected.parent.resolve() == folder.resolve():
        path = expected
    if not path.is_file():
        raise ValueError('No completed video file could be downloaded from this source.')
    if path.stat().st_size > MAX_VIDEO_BYTES:
        raise VideoSizeLimit('No format fits the server size limit.')
    # Preserve the chosen video stream; normalize audio only for the iPhone profile.
    executable = os.getenv('VIDLORA_FFMPEG_PATH') or shutil.which('ffmpeg')
    if not executable:
        raise ValueError('FFmpeg is required for iPhone-compatible MP4 output.')
    ready = folder / ('prepared.' + output_ext)
    args = [executable, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(path),
            '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'copy']
    if profile == 'iphone':
        args += ['-tag:v', 'avc1']
    if profile == 'original' or str(info.get('acodec') or '').startswith(('mp4a', 'aac')):
        args += ['-c:a', 'copy']
    else:
        args += ['-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', str(audio_bitrate(info)) + 'k', '-ar', '48000', '-ac', '2']
    if output_ext == 'mp4':
        args += ['-movflags', '+faststart']
    subprocess.run(args + ['-metadata', 'title=' + str(info.get('title') or 'Video'), str(ready)], check=True, timeout=240,
                   stdout=subprocess.DEVNULL)
    if ready.stat().st_size > MAX_VIDEO_BYTES:
        raise VideoSizeLimit('No format fits the server size limit.')
    final_path = folder / ('vidlora.' + output_ext)
    ready.replace(final_path)
    if path != final_path:
        path.unlink(missing_ok=True)
    (folder / 'media.json').write_text(json.dumps({'title': info.get('title') or 'Video', 'ext': output_ext}), encoding='utf-8')
    return final_path


@app.get('/health')
def health():
    return {'ok': True, 'downloads_enabled': ENABLE_SOCIAL, 'api_version': '1.4.0', 'max_download_mb': MAX_VIDEO_BYTES // 1024**2}


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
        info = await run_worker('metadata', data.url, folder, 75)
    except SourceBlocked as ex:
        raise HTTPException(422, detail=str(ex) or BLOCKED_MESSAGE) from ex
    except VideoSizeLimit as ex:
        raise HTTPException(413, detail=str(ex) or 'The source exceeds the current hosting capacity.') from ex
    except Exception as ex:
        raise HTTPException(422, detail='Unable to resolve this public video. The source may require authorization or restrict downloads.') from ex
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        _metadata_lock.release()
    if info.get('is_live'):
        raise HTTPException(422, detail='Live streams are not supported.')
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
    options = info.get('qualities')
    if options is None:  # Backwards-compatible metadata test/adapter contract.
        options = [{'label':'MP4 video', 'ext':'mp4', 'profile':'iphone'}]
    if not options:
        raise HTTPException(422, detail='No public downloadable video formats are available from this source.')
    return {
        'title': str(info.get('title') or 'Video').strip()[:150],
        # The service does not expose remote thumbnails or personal data by default.
        'formats': [{'label': q['label'], 'url': public_base + '/api/download/' + sign_url(data.url, title=info.get('title') or 'Video', quality=q),
                     'ext':q['ext'], 'downloadable':q.get('downloadable',True), 'estimated_bytes':q.get('estimated_bytes'),
                     'iphone_compatible':q.get('profile')=='iphone'} for q in options],
    }


@dataclass
class PreparedDownload:
    folder: Path
    expires: float
    filename: str
    task: object = None
    error: str = ''
    status: int = 200
    readers: int = 0
    ext: str = 'mp4'
    last_access: float = 0


_downloads = {}
_creation_lock = asyncio.Lock()


def folder_size(folder):
    total = 0
    for path in folder.glob('*'):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


def prune_downloads(required=0):
    for key, job in list(_downloads.items()):
        if job.expires <= time.time() and (not job.task or job.task.done()) and not job.readers:
            shutil.rmtree(job.folder, ignore_errors=True)
            del _downloads[key]
    if required:
        for key, job in sorted(list(_downloads.items()), key=lambda item:item[1].last_access):
            if sum(folder_size(j.folder) for j in _downloads.values()) + required <= TEMP_STORAGE_BYTES and len(_downloads) < 8:
                break
            if (not job.task or job.task.done()) and not job.readers and time.time() - job.last_access >= 60:
                shutil.rmtree(job.folder, ignore_errors=True)
                del _downloads[key]


async def prepare_file(job, url):
    try:
        result = await run_worker('download', url, job.folder, 480)
        job.ext = result.get('ext') or 'mp4'
        if job.ext not in ('mp4', 'webm'):
            raise ValueError('Unsupported output format')
        path = job.folder / ('vidlora.' + job.ext)
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_VIDEO_BYTES:
            raise ValueError('No completed video within the size limit')
        if result.get('title'):
            job.filename = safe_filename(result['title'], job.ext)
    except BaseException as error:
        job.error = 'Download failed or source does not permit this file.'
        job.status = 422
        if isinstance(error, SourceBlocked):
            job.error = str(error) or BLOCKED_MESSAGE
        elif isinstance(error, VideoSizeLimit):
            job.error, job.status = 'The selected quality exceeds this host’s download or temporary-storage capacity. No lower quality was substituted.', 413
        elif isinstance(error, TimeoutError):
            job.error, job.status = 'The source took too long to prepare this video. Please try again later.', 504
        job.expires = time.time() + 60
        shutil.rmtree(job.folder, ignore_errors=True)
        if not isinstance(error, Exception):
            raise
    finally:
        _download_lock.release()
        job.last_access = time.time()


async def get_download(token, request):
    if not ENABLE_SOCIAL:
        raise HTTPException(503, detail='Social video processing disabled.')
    url = unsign(token)
    raw = token.rsplit('.', 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
    quality = payload.get('q') or {}
    estimated = quality.get('estimated_bytes') or MAX_VIDEO_BYTES
    if estimated > MAX_VIDEO_BYTES or estimated * 2.2 > TEMP_STORAGE_BYTES:
        raise HTTPException(413, detail=f'This selected quality needs about {round(estimated / 1024**2)} MB. The current host supports files up to {MAX_VIDEO_BYTES // 1024**2} MB. Larger downloads require more hosting storage; the selected resolution has not been changed.')
    key = hashlib.sha256(token.encode()).hexdigest()
    async with _creation_lock:
        prune_downloads()
        if key in _downloads:
            return _downloads[key]
        prune_downloads(int(estimated * 2.2))
        if len(_downloads) >= 8 or sum(folder_size(j.folder) for j in _downloads.values()) + estimated * 2.2 > TEMP_STORAGE_BYTES:
            raise HTTPException(503, detail='Temporary download storage is busy. Please try again later.')
        enforce_rate_limit('download:' + (request.client.host if request.client else ''), 4, 3600)
        try:
            await asyncio.wait_for(_download_lock.acquire(), timeout=1)
        except TimeoutError:
            raise HTTPException(503, detail='Server busy. Try again shortly.')
        folder = None
        try:
            folder = Path(tempfile.mkdtemp(prefix='vidlora-'))
            (folder / 'request.json').write_text(json.dumps({'quality':payload.get('q')}), encoding='utf-8')
        except BaseException:
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
            _download_lock.release()
            raise
        job = PreparedDownload(folder, min(payload['e'], time.time() + 3600), safe_filename(payload.get('t') or 'Video'))
        job.last_access = time.time()
        _downloads[key] = job
        job.task = asyncio.create_task(prepare_file(job, url))
        return job


def preparation_page(token):
    # The save link is shown after preparation and requires a fresh, native tap.
    # No large Blob URLs or automatic async clicks are involved on iPhone.
    prefix = '/api/status/' + quote(token, safe='')
    raw = token.rsplit('.', 1)[0]
    quality = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))).get('q') or {}
    message = 'Preparing an iPhone-compatible MP4. ' if quality.get('profile','iphone') == 'iphone' else 'Preparing the selected original-format video. Original codecs may need a compatible player on iPhone. '
    message += str(quality.get('label') or '')
    page = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Vidlora download</title>
<style>body{font:18px/1.6 system-ui;background:#f3f7ff;color:#142145;max-width:620px;margin:10vh auto;padding:24px}a{color:#087aff}#save{display:inline-block;background:#087aff;color:white;border-radius:12px;padding:12px 20px} [hidden]{display:none!important}</style>
<h1>Prepare your video</h1><p id="status" role="status">PREPARATION_MESSAGE Keep this page open; long videos can take several minutes.</p>
<p><a id="save" hidden>Download video</a></p><p>On iPhone, check Files → Downloads after saving. Prepared files are kept temporarily, for up to one hour, to support resuming.</p>
<p><a href="https://vidlora.download/#online">Return to Vidlora</a></p>
<script>const statusUrl=''' + json.dumps(prefix) + '''; const status=document.getElementById('status'),save=document.getElementById('save');
async function poll(){try{const r=await fetch(statusUrl,{cache:'no-store'}),d=await r.json();if(!r.ok||d.error){status.textContent=d.error||d.detail||'Could not prepare the video.';return;}if(d.ready){status.textContent=d.filename;save.href=d.url;save.textContent='Download video';save.hidden=false;return;}setTimeout(poll,1500);}catch(e){status.textContent='Connection interrupted. Reload this page to check your prepared download.';}}poll();</script></html>'''
    return HTMLResponse(page.replace('PREPARATION_MESSAGE', html.escape(message)), headers={'Cache-Control':'no-store'})


@app.api_route('/api/download/{token}', methods=['GET', 'HEAD'])
async def download(token: str, request: Request):
    job = await get_download(token, request)
    if request.method == 'GET' and 'text/html' in request.headers.get('accept', ''):
        return preparation_page(token)
    await asyncio.shield(job.task)
    if job.error:
        raise HTTPException(job.status, detail=job.error)
    return CachedVideoResponse(job)


@app.get('/api/status/{token}')
async def download_status(token: str, request: Request):
    job = await get_download(token, request)
    if job.error:
        return JSONResponse({'error':job.error}, status_code=job.status)
    ready = job.task.done()
    return {'ready':ready, 'filename':job.filename,
            'url': '/api/file/' + quote(token, safe='') + '/' + quote(job.filename, safe='') if ready else None}


@app.api_route('/api/file/{token}/{name}', methods=['GET', 'HEAD'])
async def prepared_file(token: str, name: str, request: Request):
    unsign(token)
    prune_downloads()
    job = _downloads.get(hashlib.sha256(token.encode()).hexdigest())
    if not job or not job.task.done():
        raise HTTPException(404, detail='Prepared file unavailable. Return to Vidlora and check the link again.')
    if job.error:
        raise HTTPException(job.status, detail=job.error)
    return CachedVideoResponse(job)
