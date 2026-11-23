"""Isolated yt-dlp job; the API terminates this process tree on timeout."""
import json
from pathlib import Path
import sys
from app import download_video, get_metadata

if __name__ == '__main__':
    action, url, folder = sys.argv[1:]
    if action == 'metadata':
        info = get_metadata(url)
        result = {key: info.get(key) for key in ('title', 'filesize', 'is_live', 'height', 'qualities')}
    else:
        request_path = Path(folder, 'request.json')
        selected = json.loads(request_path.read_text(encoding='utf-8')).get('quality') if request_path.exists() else None
        download_video(url, Path(folder), selected)
        result = json.loads(Path(folder, 'media.json').read_text(encoding='utf-8'))
    # Keep the protocol separate from yt-dlp/FFmpeg console output.
    Path(folder, 'result.json').write_text(json.dumps(result), encoding='utf-8')
