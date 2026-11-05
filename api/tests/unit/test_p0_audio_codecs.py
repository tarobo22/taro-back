import io

import av
import pytest

from api.adapters.ai_audio import validate_audio
from api.domain.ai_jobs import AIError


def compressed(container_format, codec, seconds=1):
    output = io.BytesIO()
    with av.open(output, mode='w', format=container_format) as container:
        stream = container.add_stream(codec, rate=48000)
        stream.layout = 'mono'
        for _ in range(seconds * 48):
            frame = av.AudioFrame(format='s16', layout='mono', samples=1000)
            frame.sample_rate = 48000
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return output.getvalue()


@pytest.mark.parametrize('container_format,codec,mime', [
    ('mp4', 'aac', 'audio/mp4'), ('mp3', 'libmp3lame', 'audio/mpeg'),
    ('ogg', 'libopus', 'audio/ogg'), ('webm', 'libopus', 'audio/webm')])
def test_mobile_compressed_audio_is_actually_decoded_and_duration_checked(container_format, codec, mime):
    data = compressed(container_format, codec)
    accepted_mime, actual_ms = validate_audio(data, mime, 1000)
    assert accepted_mime == mime and 950 <= actual_ms <= 1100
    with pytest.raises(AIError, match='AUDIO_TOO_LONG'):
        validate_audio(compressed(container_format, codec, 61), mime, 1000)
    with pytest.raises(AIError, match='AUDIO_DURATION_MISMATCH'):
        validate_audio(data, mime, 10000)
    with pytest.raises(AIError, match='AUDIO_FORMAT_UNSUPPORTED'):
        validate_audio(data, 'audio/wav', 1000)
