"""Incremental PCM16 gain and pitch-preserving tempo for dots.tts."""

import asyncio
import io
import shutil
import subprocess
import tempfile
import wave

from array import array
from pathlib import Path

from core.stream_audio import PCMChunk, WavStreamDecoder


def apply_gain(chunk: PCMChunk, volume: float) -> PCMChunk:
    if volume == 1.0:
        return chunk
    samples = array('h')
    samples.frombytes(chunk.data)
    scaled = array('h', (max(-32768, min(32767, round(sample * volume))) for sample in samples))
    return PCMChunk(scaled.tobytes(), chunk.sample_rate, chunk.channels)


def find_ffmpeg(configured: str) -> str:
    executable = configured.strip() or shutil.which('ffmpeg')
    if not executable or not Path(executable).is_file():
        raise ValueError('调整 dots.tts 语速需要 FFmpeg，请在设置中填写 ffmpeg.exe 路径')
    return str(executable)


async def transform_pcm(chunks, speed: float, volume: float, ffmpeg_path: str):
    """Keep streaming at non-default speeds; never buffer the whole utterance."""
    if not 0.5 <= speed <= 2.0 or not 0 <= volume <= 2.0:
        raise ValueError('语速应为 0.5–2.0，音量应为 0–2.0')
    if speed == 1.0:
        try:
            async for chunk in chunks:
                yield apply_gain(chunk, volume)
        finally:
            await chunks.aclose()
        return

    executable = find_ffmpeg(ffmpeg_path)
    process = None
    feeder = None
    pending_io = set()
    errors = tempfile.TemporaryFile()

    async def threaded(function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        pending_io.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                pending_io.discard(task)

    try:
        try:
            first = await anext(chunks)
        except StopAsyncIteration:
            return
        rate, channels = first.sample_rate, first.channels
        process = subprocess.Popen(
            [
                executable,
                '-hide_banner',
                '-loglevel',
                'error',
                '-nostdin',
                '-probesize',
                '32',
                '-analyzeduration',
                '0',
                '-f',
                's16le',
                '-ar',
                str(rate),
                '-ac',
                str(channels),
                '-i',
                'pipe:0',
                '-af',
                f'atempo={speed}',
                '-f',
                's16le',
                '-flush_packets',
                '1',
                'pipe:1',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )

        def write(data):
            process.stdin.write(data)
            process.stdin.flush()

        async def feed():
            try:
                await threaded(write, first.data)
                async for chunk in chunks:
                    if (chunk.sample_rate, chunk.channels) != (rate, channels):
                        raise ValueError('音频流中途改变了采样格式')
                    await threaded(write, chunk.data)
            finally:
                await threaded(process.stdin.close)

        feeder = asyncio.create_task(feed())
        buffered = bytearray()
        frames = 0
        while data := await threaded(process.stdout.read1, 8192):
            buffered.extend(data)
            count = len(buffered) // (channels * 2) * (channels * 2)
            if count:
                frames += count // (channels * 2)
                yield apply_gain(PCMChunk(bytes(buffered[:count]), rate, channels), volume)
                del buffered[:count]
        await feeder
        code = await threaded(process.wait)
        if code:
            errors.seek(0)
            raise ValueError(f'FFmpeg 变速失败: {errors.read(2000).decode(errors="replace")}')
        if buffered or not frames:
            raise ValueError('FFmpeg 未返回完整音频')
    finally:
        if process is not None and process.poll() is None:
            process.kill()
        if feeder is not None:
            if not feeder.done():
                feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        if pending_io:
            await asyncio.gather(*pending_io, return_exceptions=True)
        if process is not None:
            await asyncio.to_thread(process.wait)
            process.stdin.close()
            process.stdout.close()
        errors.close()
        await chunks.aclose()


async def transform_wav(audio: bytes, speed: float, volume: float, ffmpeg_path: str) -> bytes:
    if speed == 1.0 and volume == 1.0:
        return audio
    parser = WavStreamDecoder()
    decoded = parser.feed(audio)
    parser.finish()

    async def source():
        for chunk in decoded:
            yield chunk

    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(decoded[0].channels)
        wav.setsampwidth(2)
        wav.setframerate(decoded[0].sample_rate)
        async for chunk in transform_pcm(source(), speed, volume, ffmpeg_path):
            wav.writeframes(chunk.data)
    return output.getvalue()
