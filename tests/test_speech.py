import asyncio
import io
import struct
import wave

import pytest

from core.speech import SpeechPart, build_speech_parts, concatenate_audio, replace_words, synthesize_parts
from core.stream_audio import WavStreamDecoder


def wav_bytes(rate=24000, channels=1, value=1000, frames=240):
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack('<h', value) * channels * frames)
    return out.getvalue()


def test_scopes_and_original_clip_priority():
    parts = build_speech_parts(
        '{user_name}说：{message}，固定Alice',
        {'user_name': 'Alice', 'message': 'Alice咕咕嘎嘎Alice'},
        {'Alice': '小爱'},
        {'Alice': '艾丽丝', '咕咕嘎嘎': '不应覆盖音频'},
        {'咕咕': 'short.wav', '咕咕嘎嘎': 'long.wav'},
    )
    assert parts == [SpeechPart('小爱说：艾丽丝'), SpeechPart('咕咕嘎嘎', 'long.wav'), SpeechPart('艾丽丝，固定Alice')]


def test_literal_longest_single_pass():
    assert replace_words('abcd ab a.b', {'ab': 'abcd', 'abcd': 'X', 'a.b': '点', '': 'bad'}) == 'X abcd 点'


def test_template_escaping_and_repeated_fields():
    parts = build_speech_parts(
        '{{x}}{user_name}:{message}{message}', {'user_name': '{message}', 'message': '音'}, {}, {}, {'音': 'x.wav'}
    )
    assert parts == [SpeechPart('{x}{message}:'), SpeechPart('音', 'x.wav'), SpeechPart('音', 'x.wav')]


@pytest.mark.asyncio
async def test_clip_order_and_resampling(tmp_path):
    clip = tmp_path / '音频.wav'
    clip.write_bytes(wav_bytes(48000, 2, value=2000, frames=480))
    calls = []

    class Service:
        async def text_to_speech(self, text):
            calls.append(text)
            return wav_bytes(value=1000 if text == '前' else 3000)

    result = await synthesize_parts([SpeechPart('前'), SpeechPart('触发', str(clip)), SpeechPart('后')], Service())
    assert calls == ['前', '后']
    with wave.open(io.BytesIO(result)) as wav:
        assert (wav.getframerate(), wav.getnchannels()) == (48000, 2)
        samples = struct.unpack('<' + 'h' * wav.getnframes() * 2, wav.readframes(wav.getnframes()))
        # Away from resampler edges: verify the actual audio ordering.
        assert abs(samples[200] - 1000) < 30
        assert samples[1200] == 2000
        assert abs(samples[2200] - 3000) < 30


@pytest.mark.asyncio
async def test_missing_clip_fails_before_tts(tmp_path):
    class Service:
        async def text_to_speech(self, text):
            pytest.fail('Must validate clips before generating')

    with pytest.raises(ValueError, match='无法读取'):
        await synthesize_parts([SpeechPart('前'), SpeechPart('x', str(tmp_path / 'missing.wav'))], Service())


@pytest.mark.parametrize('packet_size', [1, 3, 17, 44, 1024])
def test_incremental_wav_arbitrary_packet_boundaries(packet_size):
    data = bytearray(wav_bytes())
    data[4:8] = data[40:44] = b'\xff' * 4
    parser = WavStreamDecoder()
    chunks = []
    for start in range(0, len(data), packet_size):
        chunks += parser.feed(data[start : start + packet_size])
    parser.finish()
    assert b''.join(c.data for c in chunks) == data[44:]
    assert all(c.sample_rate == 24000 and c.channels == 1 for c in chunks)


def test_truncated_stream_detected():
    parser = WavStreamDecoder()
    parser.feed(wav_bytes()[:-2])
    with pytest.raises(ValueError, match='不完整'):
        parser.finish()


def test_invalid_stream_detected():
    with pytest.raises(ValueError, match='不是 WAV'):
        WavStreamDecoder().feed(b'{"error":"broken"}')


@pytest.mark.asyncio
async def test_clip_only_message_skips_template_quotes(tmp_path):
    clip = tmp_path / 'clip.wav'
    clip.write_bytes(wav_bytes())

    class Service:
        async def text_to_speech(self, text):
            pytest.fail('Do not synthesize bare template punctuation')

    parts = build_speech_parts('"{message}"', {'message': '咕'}, {}, {}, {'咕': str(clip)})
    assert await synthesize_parts(parts, Service())


@pytest.mark.asyncio
async def test_stop_cancels_current_and_pending_jobs(config):
    from core.player import audio_player

    started = asyncio.Event()

    async def active():
        started.set()
        await asyncio.Event().wait()

    async def pending():
        pytest.fail('Pending job should be cancelled')

    audio_player.start_worker()
    a = asyncio.create_task(audio_player.play_job_async(active))
    await started.wait()
    b = asyncio.create_task(audio_player.play_job_async(pending))
    await asyncio.sleep(0)
    await audio_player.stop_worker()
    results = await asyncio.gather(a, b, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)


@pytest.mark.asyncio
async def test_stream_device_closes_on_error(config, monkeypatch):
    from core.player import audio_player
    from core.stream_audio import PCMChunk

    events = []

    class Device:
        def write(self, data):
            events.append('write')

        def stop_stream(self):
            events.append('stop')

        def close(self):
            events.append('close')

    monkeypatch.setattr(audio_player.p, 'open', lambda **kwargs: Device())

    async def chunks():
        yield PCMChunk(b'\0\0', 48000, 1)
        raise ValueError('stream interrupted')

    with pytest.raises(ValueError, match='interrupted'):
        await audio_player.play_pcm_stream(chunks())
    assert events == ['write', 'stop', 'close']


@pytest.mark.asyncio
async def test_queue_atomicity_and_error_recovery(config):
    from core.player import audio_player

    events = []

    async def first():
        events.append('a1')
        await asyncio.sleep(0.01)
        events.append('clip')
        await asyncio.sleep(0.01)
        events.append('a2')

    async def second():
        events.append('b')

    async def failed():
        raise ValueError('test-error')

    audio_player.start_worker()
    try:
        await asyncio.gather(audio_player.play_job_async(first), audio_player.play_job_async(second))
        assert events == ['a1', 'clip', 'a2', 'b']
        with pytest.raises(ValueError, match='test-error'):
            await audio_player.play_job_async(failed)
        await audio_player.play_job_async(second)
        await asyncio.wait_for(audio_player.audio_queue.join(), 1)
    finally:
        await audio_player.stop_worker()


@pytest.mark.asyncio
async def test_streamed_template_pipeline(config, monkeypatch, tmp_path):
    import tts_service
    from core.player import audio_player
    from core.speech import speak_template
    from core.stream_audio import PCMChunk

    clip = tmp_path / 'clip.wav'
    clip.write_bytes(wav_bytes())
    config.aliasDict.value = {'u': '用户'}
    config.messageAliasDict.value = {'x': '正文'}
    config.audioClipDict.value = {'咕': str(clip)}
    config.dotsStreaming.value = True
    events = []

    class Service:
        async def stream_speech(self, text):
            events.append(text)
            yield PCMChunk(b'\0\0', 24000, 1)

    monkeypatch.setattr(tts_service, 'get_tts_service', Service)

    async def consume(chunks):
        async for _ in chunks:
            pass

    monkeypatch.setattr(audio_player, 'play_pcm_stream', consume)
    monkeypatch.setattr(audio_player, 'play_bytes', lambda _: events.append('FILE'))
    audio_player.start_worker()
    try:
        await speak_template('{user_name}:{message}', user_name='u', message='x咕x')
        assert events == ['用户:正文', 'FILE', '正文']
    finally:
        await audio_player.stop_worker()
