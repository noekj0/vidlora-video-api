"""Offline API behavior tests. These mock yt-dlp; they do not verify any social-media site."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# If yt-dlp isn't installed in the test runner, provide a minimal stub for import.
if importlib.util.find_spec('yt_dlp') is None:
    fake = types.ModuleType('yt_dlp')
    fake.utils = types.SimpleNamespace(DownloadError=RuntimeError)
    sys.modules['yt_dlp'] = fake
import app as api

client = TestClient(api.app)


@pytest.fixture(autouse=True)
def reset_download_cache(monkeypatch):
    import asyncio
    import shutil
    api._limiter.clear()
    monkeypatch.setattr(api, '_download_lock', asyncio.Semaphore(2))
    monkeypatch.setattr(api, '_creation_lock', asyncio.Lock())
    yield
    for job in api._downloads.values():
        shutil.rmtree(job.folder, ignore_errors=True)
    api._downloads.clear()


def test_health():
    assert client.get('/health').status_code == 200


def test_reject_arbitrary_urls(monkeypatch):
    monkeypatch.setattr(api, 'ENABLE_SOCIAL', True)
    result = client.post('/api/resolve', json={'url': 'https://127.0.0.1/private', 'source': 'direct', 'authorized': True})
    assert result.status_code == 400
    result = client.post('/api/resolve', json={'url': 'https://youtube.com.attacker.invalid/watch?v=abc', 'authorized': True})
    assert result.status_code == 400


def test_real_download_route(monkeypatch, tmp_path):
    monkeypatch.setattr(api, 'ENABLE_SOCIAL', True)
    monkeypatch.setattr(api, 'PUBLIC_URL', 'https://vidlora-video-api.onrender.com')
    folders = []
    async def worker(action, url, folder, timeout):
        folders.append(folder)
        if action == 'metadata':
            return {'title': 'My test video'}
        make_file(folder)
        return {'ok': True}
    monkeypatch.setattr(api, 'run_worker', worker)
    url = 'https://www.youtube.com/watch?v=abcdefghijk'
    res = client.post('/api/resolve', json={'url': url, 'source': 'youtube', 'authorized': True})
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload['title'] == 'My test video'
    assert payload['formats'][0]['url'].startswith('https://vidlora-video-api.onrender.com/api/download/')
    token = payload['formats'][0]['url'].split('/')[-1]
    media = client.get('/api/download/' + token)
    assert media.status_code == 200, media.text
    assert 'attachment' in media.headers['content-disposition']
    assert media.content == b'fake-mp4-test-bytes'
    assert not folders[0].exists()  # Metadata workspace is removed immediately.
    assert folders[-1].exists()  # Safari may resume with another range request.
    assert api._download_lock._value == 2


def make_file(folder):
    f = folder / 'vidlora.mp4'
    f.write_bytes(b'fake-mp4-test-bytes')
    return f


def test_cors():
    res = client.options('/api/resolve', headers={'Origin': 'https://vidlora.download', 'Access-Control-Request-Method': 'POST'})
    assert res.headers.get('access-control-allow-origin') == 'https://vidlora.download'


def test_permission_required(monkeypatch):
    monkeypatch.setattr(api, 'ENABLE_SOCIAL', True)
    assert client.post('/api/resolve', json={'url': 'https://youtu.be/abcdefghijk'}).status_code == 400


def test_platform_block_is_explained(monkeypatch):
    monkeypatch.setattr(api, 'ENABLE_SOCIAL', True)
    async def blocked(*args):
        raise api.SourceBlocked()
    monkeypatch.setattr(api, 'run_worker', blocked)
    api._limiter.clear()
    result = client.post('/api/resolve', json={'url': 'https://youtu.be/abcdefghijk', 'authorized': True})
    assert result.status_code == 422
    assert 'hosting server' in result.json()['detail']


def test_invalid_and_expired_signatures():
    assert client.get('/api/download/' + api.sign_url('https://youtu.be/abcdefghijk', -5)).status_code in (403, 503)
    with pytest.raises(api.HTTPException):
        api.unsign(api.sign_url('https://youtu.be/abcdefghijk') + 'tampered')


def test_expired_browser_link_has_readable_error(monkeypatch):
    monkeypatch.setattr(api, 'ENABLE_SOCIAL', True)
    token = api.sign_url('https://youtu.be/abcdefghijk', -5)
    browser = client.get('/api/download/' + token, headers={'Accept': 'text/html'})
    assert browser.status_code == 403
    assert browser.headers['content-type'].startswith('text/html')
    assert 'Download link expired or invalid.' in browser.text
    assert 'Return to Vidlora' in browser.text
    api_client = client.get('/api/download/' + token)
    assert api_client.status_code == 403
    assert api_client.json()['detail'] == 'Download link expired or invalid.'


def test_worker_failure_cleans_up(monkeypatch):
    monkeypatch.setattr(api, 'ENABLE_SOCIAL', True)
    folders = []
    async def fail(action, url, folder, timeout):
        folders.append(folder)
        (folder / 'partial.part').write_bytes(b'partial')
        raise TimeoutError('test timeout')
    monkeypatch.setattr(api, 'run_worker', fail)
    api._limiter.clear()
    token = api.sign_url('https://youtu.be/abcdefghijk')
    assert client.get('/api/download/' + token).status_code == 504
    assert all(not folder.exists() for folder in folders)
    assert api._download_lock._value == 2


def test_real_worker_is_terminated_on_timeout(tmp_path, monkeypatch):
    import asyncio
    import os
    import time
    # Exercise a real child process rather than mocking cancellation behavior.
    worker = tmp_path / 'worker.py'
    worker.write_text('import time, pathlib, sys, subprocess\np=pathlib.Path(sys.argv[3])\np.joinpath("started").write_text("yes")\nsubprocess.Popen([sys.executable, "-c", "import time,pathlib,sys; time.sleep(3); pathlib.Path(sys.argv[1]).write_text(\'bad\')", str(p / "orphan")])\ntime.sleep(10)\n')
    monkeypatch.setattr(api, '__file__', str(tmp_path / 'app.py'))
    with pytest.raises(TimeoutError):
        asyncio.run(api.run_worker('metadata', 'https://youtu.be/abcdefghijk', tmp_path, 1))
    time.sleep(3)
    assert (tmp_path / 'started').exists()
    assert not (tmp_path / 'orphan').exists()


def test_worker_console_output_cannot_corrupt_result(tmp_path, monkeypatch):
    import asyncio
    worker = tmp_path / 'worker.py'
    worker.write_text('import pathlib, sys\nprint("[download] 100% complete")\npathlib.Path(sys.argv[3], "result.json").write_text(\'{"ok": true}\')\n')
    monkeypatch.setattr(api, '__file__', str(tmp_path / 'app.py'))
    assert asyncio.run(api.run_worker('download', 'https://youtu.be/abcdefghijk', tmp_path, 10)) == {'ok': True}


def test_size_budget_includes_audio_and_chooses_smaller_video(monkeypatch):
    monkeypatch.setattr(api, 'MAX_VIDEO_BYTES', 1000)
    info = {'formats': [
        {'format_id':'audio', 'ext':'m4a', 'vcodec':'none', 'acodec':'aac', 'filesize':300},
        {'format_id':'small', 'ext':'mp4', 'vcodec':'avc1', 'acodec':'none', 'height':360, 'filesize':600},
        {'format_id':'large', 'ext':'mp4', 'vcodec':'avc1', 'acodec':'none', 'height':720, 'filesize':900},
    ]}
    assert api.format_within_budget(info) == 'small+audio'


def test_long_video_can_use_smaller_opus_audio(monkeypatch):
    monkeypatch.setattr(api, 'MAX_VIDEO_BYTES', 1000)
    info = {'formats': [
        {'format_id':'aac', 'ext':'m4a', 'vcodec':'none', 'acodec':'aac', 'abr':128, 'filesize':1200},
        {'format_id':'opus', 'ext':'webm', 'vcodec':'none', 'acodec':'opus', 'abr':48, 'filesize':200},
        {'format_id':'video', 'ext':'mp4', 'vcodec':'avc1', 'acodec':'none', 'height':240, 'filesize':600},
    ]}
    assert api.format_within_budget(info) == 'video+opus'
    info['formats'].pop(1)
    with pytest.raises(api.VideoSizeLimit):
        api.format_within_budget(info)


def test_rejected_audio_falls_back_to_another_real_track(monkeypatch):
    monkeypatch.setattr(api, 'MAX_VIDEO_BYTES', 1000)
    formats = [
        {'format_id':'broken', 'ext':'m4a', 'vcodec':'none', 'acodec':'aac', 'abr':128, 'filesize':200, 'url':'https://example.invalid/broken'},
        {'format_id':'working', 'ext':'webm', 'vcodec':'none', 'acodec':'opus', 'abr':48, 'filesize':150, 'url':'https://example.invalid/working'},
        {'format_id':'video', 'ext':'mp4', 'vcodec':'avc1', 'acodec':'none', 'height':360, 'filesize':600, 'url':'https://example.invalid/video'},
    ]
    info = {'id':'test', 'title':'Permitted test', 'extractor':'test', 'formats':formats}
    opts = api.extractor_options()
    opts['format'] = api.format_within_budget(info)
    def probes(items, warning=True):
        yield from (f for f in items if f['format_id'] != 'broken')
    with api.yt_dlp.YoutubeDL(opts) as downloader:
        monkeypatch.setattr(downloader, '_check_formats', probes)
        result = downloader.process_ie_result(info, download=False)
    assert [f['format_id'] for f in result['requested_formats']] == ['video', 'working']


def test_iphone_selection_rejects_av1_even_when_smaller(monkeypatch):
    monkeypatch.setattr(api, 'MAX_VIDEO_BYTES', 1000)
    formats = [
        {'format_id':'a', 'ext':'m4a', 'vcodec':'none', 'acodec':'aac', 'filesize':100},
        {'format_id':'h264', 'ext':'mp4', 'vcodec':'avc1', 'acodec':'none', 'height':360, 'filesize':500},
        {'format_id':'av1', 'ext':'mp4', 'vcodec':'av01', 'acodec':'none', 'height':720, 'filesize':300},
    ]
    assert api.format_within_budget({'formats':formats}) == 'h264+a'


def test_native_save_reuses_file_for_head_and_ranges(monkeypatch):
    from urllib.parse import quote
    monkeypatch.setattr(api, 'ENABLE_SOCIAL', True)
    calls = []
    async def worker(action, url, folder, timeout):
        calls.append(folder)
        make_file(folder)
        return {'title':'My real title café'}
    monkeypatch.setattr(api, 'run_worker', worker)
    token = api.sign_url('https://youtu.be/abcdefghijk', title='My real title café')
    with TestClient(api.app) as browser:
        page = browser.get('/api/download/' + token, headers={'Accept':'text/html'})
        assert page.status_code == 200
        assert 'blob:' not in page.text and 'Preparing an iPhone-compatible MP4' in page.text
        initial = browser.get('/api/download/' + token)
        status = browser.get('/api/status/' + token).json()
        assert status['ready'] and status['filename'] == 'My real title café.mp4'
        head = browser.head(status['url'])
        assert head.status_code == 200 and head.content == b''
        assert head.headers['accept-ranges'] == 'bytes'
        assert quote(status['filename'], safe='') in head.headers['content-disposition']
        first = browser.get(status['url'], headers={'Range':'bytes=0-3'})
        second = browser.get(status['url'], headers={'Range':'bytes=4-7', 'If-Range':first.headers['etag']})
        assert first.status_code == second.status_code == 206
        assert first.content + second.content == initial.content[:8]
        assert first.headers['etag'] == second.headers['etag'] == head.headers['etag']
        assert len(calls) == 1
        assert calls[0].exists()
        job = next(iter(api._downloads.values()))
        job.expires = 0
        job.readers = 1
        api.prune_downloads()
        assert calls[0].exists()  # Never remove a file while a range is being sent.
        job.readers = 0
        api.prune_downloads()
        assert not calls[0].exists()


def test_safe_filename_removes_paths_and_header_injection():
    name = api.safe_filename('../Real title\r\n/another\\name')
    assert name.endswith('.mp4') and '/' not in name and '\\' not in name
    assert '\r' not in name and '\n' not in name
    assert '\u202e' not in api.safe_filename('My \u202evideo.mp4')
    assert api.safe_filename('Real name.mp4') == 'Real name.mp4'
    assert len(api.safe_filename('🎬' * 200).encode('utf-8')) <= 204
