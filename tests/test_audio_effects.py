import asyncio
import io
import math
import struct
import wave

import pytest

from core.audio_effects import apply_gain, transform_pcm, transform_wav
from core.stream_audio import PCMChunk

FFMPEG = r'E:\AITTS\ffmpeg\bin\ffmpeg.exe'


def tone(seconds=2, rate=24000):
    return b''.join(
        struct.pack('<h', round(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(int(rate * seconds))
    )


def test_gain_clipping_and_mute():
    chunk = PCMChunk(struct.pack('<hhh', 20000, -20000, 400), 24000, 1)
    assert struct.unpack('<hhh', apply_gain(chunk, 2).data) == (32767, -32768, 800)
    assert apply_gain(chunk, 0).data == b'\0' * 6
    assert apply_gain(chunk, 1) is chunk


@pytest.mark.asyncio
@pytest.mark.parametrize('speed', [0.5, 1.5, 2.0])
async def test_tempo_duration_and_pitch(speed):
    pcm = tone()

    async def source():
        for offset in range(0, len(pcm), 1920):
            yield PCMChunk(pcm[offset : offset + 1920], 24000, 1)

    output = b''.join([chunk.data async for chunk in transform_pcm(source(), speed, 0.5, FFMPEG)])
    samples = struct.unpack('<' + 'h' * (len(output) // 2), output)
    duration = len(samples) / 24000
    assert abs(duration - 2 / speed) < 0.12
    crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
    assert abs(crossings / duration - 440) < 8
    assert max(samples) <= 4100


@pytest.mark.asyncio
async def test_tempo_is_incremental_before_input_ends():
    ready = asyncio.Event()
    release = asyncio.Event()

    async def source():
        yield PCMChunk(tone(seconds=1), 24000, 1)
        ready.set()
        await release.wait()
        yield PCMChunk(tone(seconds=0.5), 24000, 1)

    stream = transform_pcm(source(), 1.5, 1, FFMPEG)
    try:
        first = await asyncio.wait_for(anext(stream), 5)
        assert first.data
        assert not release.is_set()
        release.set()
        async for _ in stream:
            pass
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_source_failure_propagates_without_hanging():
    async def source():
        yield PCMChunk(tone(seconds=0.5), 24000, 1)
        raise ValueError('source failed')

    async def collect():
        return [c async for c in transform_pcm(source(), 1.2, 1, FFMPEG)]

    with pytest.raises(ValueError, match='source failed'):
        await asyncio.wait_for(collect(), 5)


@pytest.mark.asyncio
async def test_cancel_stream_closes_source():
    closed = asyncio.Event()

    async def source():
        try:
            while True:
                yield PCMChunk(tone(seconds=0.2), 24000, 1)
                await asyncio.sleep(0.01)
        finally:
            closed.set()

    stream = transform_pcm(source(), 1.2, 1, FFMPEG)
    await asyncio.wait_for(anext(stream), 5)
    await asyncio.wait_for(stream.aclose(), 5)
    assert closed.is_set()


@pytest.mark.asyncio
async def test_nonstream_wav_gain():
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(tone())
    result = await transform_wav(buf.getvalue(), 1, 0, '')
    with wave.open(io.BytesIO(result)) as wav:
        assert wav.getnframes() == 48000
        assert not any(wav.readframes(48000))
