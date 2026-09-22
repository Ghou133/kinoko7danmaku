"""Queued dots requests retain their voice, endpoint and playback settings."""

from copy import deepcopy
import json

import httpx
import pytest

from tests.test_speech import wav_bytes


@pytest.fixture
def dots_config(config):
    saved = {
        name: deepcopy(getattr(config, name).value)
        for name in dir(config)
        if name.startswith('dots') and hasattr(getattr(config, name), 'value')
    }
    yield config
    for name, value in saved.items():
        getattr(config, name).value = value


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_queued_settings_and_reference_are_frozen(dots_config, monkeypatch, streaming):
    import tts_service.dots as module

    initial = {
        'dotsApiUrl': 'http://127.0.0.1:9881/tts/stream/',
        'dotsVoice': 'first.wav',
        'dotsPromptText': '',
        'dotsLanguage': 'zh',
        'dotsNumSteps': 4,
        'dotsNormalizeText': False,
        'dotsStreaming': streaming,
        'dotsSpeed': 1.2,
        'dotsVolume': 0.7,
        'dotsFfmpegPath': 'first-ffmpeg.exe',
        'dotsTimeout': 87,
    }
    later = {
        'dotsApiUrl': 'http://127.0.0.1:9999',
        'dotsVoice': 'second.wav',
        'dotsPromptText': '新的手动参考文本',
        'dotsLanguage': 'en',
        'dotsNumSteps': 8,
        'dotsNormalizeText': True,
        'dotsStreaming': not streaming,
        'dotsSpeed': 0.8,
        'dotsVolume': 1.4,
        'dotsFfmpegPath': 'second-ffmpeg.exe',
        'dotsTimeout': 12,
    }
    for name, value in initial.items():
        getattr(dots_config, name).value = value

    references = []

    def reference(voice):
        references.append(voice)
        return ('E:/presets/' + voice, '入队时的自动参考文本')

    monkeypatch.setattr(module, 'local_reference', reference)
    service = module.DotsTTSService()
    assert references == ['first.wav']
    for name, value in later.items():
        getattr(dots_config, name).value = value
    # Also freeze the resolved preset, not merely its configured name.
    monkeypatch.setattr(module, 'local_reference', lambda _: ('E:/new.wav', '后来修改的文本'))

    effects = []

    async def transform_wav(audio, speed, volume, ffmpeg):
        effects.append((speed, volume, ffmpeg))
        return audio

    async def transform_pcm(chunks, speed, volume, ffmpeg):
        effects.append((speed, volume, ffmpeg))
        async for chunk in chunks:
            yield chunk

    monkeypatch.setattr(module, 'transform_wav', transform_wav)
    monkeypatch.setattr(module, 'transform_pcm', transform_pcm)
    requests, client_options = [], []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=wav_bytes())

    original_client = httpx.AsyncClient

    def client(**kwargs):
        client_options.append(kwargs)
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    assert service.streaming_enabled is streaming
    assert service.api_url == initial['dotsApiUrl']
    if streaming:
        chunks = [chunk async for chunk in service.stream_speech('排队的弹幕')]
        assert chunks and chunks[0].data
    else:
        assert await service.text_to_speech('排队的弹幕') == wav_bytes()

    assert str(requests[0].url) == 'http://127.0.0.1:9881/tts' + ('/stream' if streaming else '')
    assert json.loads(requests[0].content) == {
        'text': '排队的弹幕',
        'voice': 'E:/presets/first.wav',
        'prompt_text': '入队时的自动参考文本',
        'language': 'zh',
        'num_steps': 4,
        'normalize_text': False,
    }
    assert effects == [(1.2, 0.7, 'first-ffmpeg.exe')]
    assert client_options == [{'trust_env': False, 'timeout': 87}]
    fresh = module.DotsTTSService()
    assert fresh.streaming_enabled is not streaming
    assert fresh._request('新弹幕') == ('http://127.0.0.1:9999/tts', {
        'text': '新弹幕',
        'voice': 'E:/new.wav',
        'prompt_text': '新的手动参考文本',
        'language': 'en',
        'num_steps': 8,
        'normalize_text': True,
    })
