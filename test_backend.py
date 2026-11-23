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
    assert all(not folder.exists() for folder in folders)
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
    assert client.get('/api/download/' + token).status_code == 422
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
