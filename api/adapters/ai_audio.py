"""Private bounded audio validation and temporary storage. Never serves audio URLs."""
import io
import os
import time
from pathlib import Path
from uuid import uuid4

import av

from api.domain.ai_jobs import AIError


FORMATS = {
    'audio/wav': ({'wav'}, '.wav'), 'audio/x-wav': ({'wav'}, '.wav'),
    'audio/mp4': ({'mov', 'mp4', 'm4a', '3gp', '3g2', 'mj2'}, '.m4a'),
    'audio/m4a': ({'mov', 'mp4', 'm4a', '3gp', '3g2', 'mj2'}, '.m4a'),
    'audio/x-m4a': ({'mov', 'mp4', 'm4a', '3gp', '3g2', 'mj2'}, '.m4a'),
    'audio/mpeg': ({'mp3'}, '.mp3'), 'audio/mp3': ({'mp3'}, '.mp3'),
    'audio/ogg': ({'ogg'}, '.ogg'), 'application/ogg': ({'ogg'}, '.ogg'),
    'audio/webm': ({'matroska', 'webm'}, '.webm'),
    'audio/flac': ({'flac'}, '.flac'),
}


def validate_audio(data, mime_type, duration_ms, max_bytes=10485760, max_duration_ms=60000):
    if len(data) > max_bytes:
        raise AIError('AUDIO_TOO_LARGE', 413)
    if type(duration_ms) is not int or not 1 <= duration_ms <= max_duration_ms:
        raise AIError('AUDIO_TOO_LONG', 422)
    mime = mime_type.split(';', 1)[0].strip().lower()
    if mime not in FORMATS or not data:
        raise AIError('AUDIO_FORMAT_UNSUPPORTED', 415)
    try:
        with av.open(io.BytesIO(data), mode='r', options={'protocol_whitelist': 'file,pipe'}) as container:
            if not set(container.format.name.split(',')) & FORMATS[mime][0]:
                raise AIError('AUDIO_FORMAT_UNSUPPORTED', 415)
            if len(container.streams.audio) != 1 or len(container.streams) != 1:
                raise AIError('AUDIO_FORMAT_UNSUPPORTED', 415)
            stream = container.streams.audio[0]
            # Header duration is only an early rejection; every accepted frame is decoded.
            if container.duration is not None and container.duration / av.time_base > max_duration_ms / 1000 + 0.1:
                raise AIError('AUDIO_TOO_LONG', 422)
            seconds, frames, decoded_bytes = 0.0, 0, 0
            started = time.monotonic()
            for frame in container.decode(stream):
                frames += 1
                if not frame.sample_rate or frame.sample_rate > 192000 or len(frame.layout.channels) > 2:
                    raise AIError('AUDIO_FORMAT_UNSUPPORTED', 415)
                seconds += frame.samples / frame.sample_rate
                decoded_bytes += sum(plane.buffer_size for plane in frame.planes)
                if seconds > max_duration_ms / 1000 + 0.025:
                    raise AIError('AUDIO_TOO_LONG', 422)
                if decoded_bytes > 100 * 1024 * 1024 or frames > 20000 or time.monotonic() - started > 10:
                    raise AIError('AUDIO_DECODE_LIMIT', 422)
            if frames == 0 or seconds <= 0:
                raise AIError('AUDIO_FORMAT_UNSUPPORTED', 415)
            if abs(seconds * 1000 - duration_ms) > 1500:
                raise AIError('AUDIO_DURATION_MISMATCH', 422)
    except AIError:
        raise
    except (av.FFmpegError, ValueError, EOFError, OSError):
        raise AIError('AUDIO_FORMAT_UNSUPPORTED', 415) from None
    return mime, max(1, round(seconds * 1000))


class PrivateAudioStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def validate(self, data, mime_type, duration_ms, max_bytes, max_duration_ms):
        return validate_audio(data, mime_type, duration_ms, max_bytes, max_duration_ms)

    def save(self, data, mime_type):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.root / (str(uuid4()) + FORMATS[mime_type][1])
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        return str(target)

    def _path(self, value):
        path = Path(value).resolve()
        if path.parent != self.root or path.suffix not in {item[1] for item in FORMATS.values()}:
            raise ValueError('Audio object is outside the private store')
        return path

    def delete(self, value):
        self._path(value).unlink(missing_ok=True)

    def purge(self, age_seconds=3600):
        if not self.root.exists():
            return 0
        count = 0
        for path in self.root.iterdir():
            if path.is_file() and not path.is_symlink() and time.time() - path.stat().st_mtime >= age_seconds:
                try:
                    self.delete(path)
                    count += 1
                except ValueError:
                    continue
        return count
