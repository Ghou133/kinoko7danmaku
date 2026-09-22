import json

import httpx
import pytest

from tests.test_speech import wav_bytes


@pytest.mark.asyncio
async def test_dots_payload_and_snapshot_config(config, monkeypatch):
    from tts_service.dots import DotsTTSService

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=wav_bytes())

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    config.dotsVoice.value = '我的音色.wav'
    config.dotsPromptText.value = '参考文本'
    config.dotsLanguage.value = 'zh'
    config.dotsNumSteps.value = 4
    config.dotsApiUrl.value = 'http://localhost:9881/tts/'
    adapter = DotsTTSService()
    config.dotsApiUrl.value = 'http://localhost:9999'
    config.dotsPromptText.value = '新参考文本'
    await adapter.text_to_speech('你好')
    assert str(requests[0].url) == 'http://localhost:9881/tts'
    assert json.loads(requests[0].content) == {
        'text': '你好',
        'voice': '我的音色.wav',
        'prompt_text': '参考文本',
        'language': 'zh',
        'num_steps': 4,
        'normalize_text': True,
    }


@pytest.mark.asyncio
async def test_stream_yields_before_response_complete(config, monkeypatch):
    from tts_service.dots import DotsTTSService

    ended = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            data = wav_bytes()
            yield data[:50]
            yield data[50:]
            ended.append(True)

    def handle(request):
        assert request.url.path == '/tts/stream'
        return httpx.Response(200, stream=Stream())

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    stream = DotsTTSService().stream_speech('你好')
    chunk = await anext(stream)
    assert chunk.data and not ended
    async for _ in stream:
        pass
    assert ended


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [(200, b'{}'), (422, b'bad voice')])
async def test_invalid_response(config, monkeypatch, status, body):
    from tts_service.dots import DotsTTSService

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        'AsyncClient',
        lambda **kw: original(transport=httpx.MockTransport(lambda _: httpx.Response(status, content=body)), **kw),
    )
    with pytest.raises(ValueError):
        await DotsTTSService().text_to_speech('你好')


def test_switch_engine_without_restart(config):
    from tts_service import get_tts_service

    config.activeTTS.value = 'edge'
    assert type(get_tts_service()).__name__ == 'EdgeService'
    config.activeTTS.value = 'dots_tts'
    assert type(get_tts_service()).__name__ == 'DotsTTSService'


def test_settings_and_audio_test_construct(config):
    from gui.view.settings import SettingsInterface
    from gui.view.audio_test import AudioTestInterface

    settings = SettingsInterface()
    preview = AudioTestInterface()
    assert settings.aliasDictCard.config_item is config.aliasDict
    assert settings.messageAliasDictCard.config_item is config.messageAliasDict
    assert settings.audioClipDictCard.config_item is config.audioClipDict
    assert preview.user_name_edit is not None


def test_new_sliders_persist_actual_values(config):
    from gui.view.settings import SettingsInterface
    from gui.components.float_range_setting_card import FloatRangeSettingCard

    config.dotsSpeed.value = 1.0
    config.dotsVolume.value = 1.0
    settings = SettingsInterface()
    sliders = {card.configItem: card for card in settings.dotsGroup.findChildren(FloatRangeSettingCard)}
    sliders[config.dotsSpeed].slider.setValue(15)
    sliders[config.dotsVolume].slider.setValue(6)
    assert config.dotsSpeed.value == 1.5
    assert config.dotsVolume.value == 0.6


def test_reference_priority_and_manual_text(config, monkeypatch):
    import tts_service.dots as module

    monkeypatch.setattr(module, 'local_reference', lambda _: ('E:/presets/voice.wav', '自动参考文本'))
    config.dotsPromptText.value = ''
    _, payload = module.DotsTTSService()._request('你好')
    assert payload['voice'] == 'E:/presets/voice.wav'
    assert payload['prompt_text'] == '自动参考文本'
    config.dotsPromptText.value = '手动文本'
    assert module.DotsTTSService()._request('你好')[1]['prompt_text'] == '手动文本'
