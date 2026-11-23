"""Discover actual source qualities without silently changing resolution."""
import math
import re


def size_of(fmt, duration):
    value = fmt.get('filesize') or fmt.get('filesize_approx')
    if not value and duration and fmt.get('tbr'):
        value = duration * float(fmt['tbr']) * 1000 / 8
    return int(value) if isinstance(value, (int, float)) and math.isfinite(value) and value > 0 else None


def quality_key(fmt):
    return {'height': int(fmt.get('height') or 0), 'width': int(fmt.get('width') or 0),
            'fps': round(float(fmt.get('fps') or 0)), 'range': fmt.get('dynamic_range') or 'SDR'}


def size_label(value):
    if value is None:
        return 'size unknown'
    if value >= 1024**3:
        return f'~{value / 1024**3:.1f} GB'
    return f'~{max(1, round(value / 1024**2))} MB'


def discover_qualities(info, max_bytes):
    formats = info.get('formats') or ([info] if info.get('url') else [])
    duration = info.get('duration') or 0
    usable = [(i, f) for i, f in enumerate(formats)
              if not f.get('has_drm') and f.get('url') and
              re.fullmatch(r'[A-Za-z0-9_.-]+', str(f.get('format_id') or 'best'))]
    audios = [(i, f) for i, f in usable if f.get('vcodec') in (None, 'none') and f.get('acodec') not in (None, 'none')]
    groups = {}
    for index, video in usable:
        if video.get('vcodec') in (None, 'none') or video.get('ext') not in ('mp4', 'webm', 'mov', 'm4v', 'mkv'):
            continue
        key = quality_key(video)
        groups.setdefault(tuple(key.items()), []).append((index, video))
    choices = []
    for grouped, videos in groups.items():
        key = dict(grouped)
        for profile in ('iphone', 'original'):
            candidates = []
            for index, video in videos:
                h264 = str(video.get('vcodec') or '').startswith(('avc', 'h264'))
                if profile == 'iphone' and (not h264 or key['range'] != 'SDR'):
                    continue
                ext = 'webm' if str(video.get('vcodec') or '').startswith(('vp8', 'vp08')) else 'mp4'
                if profile == 'iphone':
                    ext = 'mp4'
                tracks = [(None, None)] if video.get('acodec') not in (None, 'none') or not audios else audios
                for audio_index, audio in tracks:
                    if ext == 'webm' and audio and not str(audio.get('acodec') or '').startswith(('opus', 'vorbis')):
                        continue
                    video_size = size_of(video, duration)
                    audio_size = size_of(audio, duration) if audio else 0
                    known = video_size is not None and audio_size is not None
                    estimate = video_size + audio_size if known else None
                    if profile == 'iphone' and audio and not str(audio.get('acodec') or '').startswith(('mp4a', 'aac')) and known:
                        estimate = video_size + max(audio_size, int(duration * 128000 / 8))
                    if estimate is not None:
                        estimate = int(estimate * 1.03)
                    fits = estimate is None or estimate <= max_bytes
                    selector = str(video.get('format_id') or 'best')
                    if audio:
                        selector += '+' + str(audio['format_id'])
                    aac = audio and str(audio.get('acodec') or '').startswith(('mp4a', 'aac'))
                    audio_rank = (int(bool(aac)) if profile == 'iphone' else 0, audio_index or 0)
                    candidates.append({'selector': selector, 'bytes': estimate, 'fits': fits,
                                       'rank': (fits, index, *audio_rank), 'ext': ext,
                                       'codec': video.get('vcodec') or 'unknown'})
            if not candidates:
                continue
            candidates.sort(key=lambda c: c['rank'], reverse=True)
            chosen = candidates[0]
            alternatives = [c for c in candidates if c['fits']] if chosen['fits'] else [chosen]
            selectors = list(dict.fromkeys(c['selector'] for c in alternatives))[:8]
            edge = min(key['height'], key['width']) if key['width'] and key['height'] else key['height']
            resolution = f'{edge}p' if edge else 'Original resolution'
            if edge >= 4320:
                resolution += ' (8K)'
            elif edge >= 2160:
                resolution += ' (4K)'
            if key['fps']:
                resolution += f" {key['fps']}fps"
            if key['range'] != 'SDR':
                resolution += ' ' + key['range']
            label = resolution + (' · iPhone MP4' if profile == 'iphone' else ' · original ' + chosen['ext'].upper())
            label += ' · ' + size_label(chosen['bytes'])
            if not chosen['fits']:
                label += ' · exceeds current hosting capacity'
            choices.append({**key, 'profile': profile, 'label': label, 'resolution': resolution,
                            'ext': chosen['ext'], 'estimated_bytes': chosen['bytes'],
                            'downloadable': chosen['fits'], 'codec': chosen['codec'],
                            'selector': '/'.join(selectors)})
    # Put the highest downloadable choice first for the existing site's default.
    choices.sort(key=lambda c: (c['downloadable'], c['height'], c['fps'], c['range'], c['profile'] == 'iphone'), reverse=True)
    return choices


def choose_exact_quality(info, selected, max_bytes):
    for choice in discover_qualities(info, max_bytes):
        if all(choice.get(k) == selected.get(k) for k in ('height', 'width', 'fps', 'range', 'profile')):
            return choice
    raise ValueError('The selected quality is no longer available from this source. Check the link again.')
