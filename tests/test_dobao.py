"""Local DoBao HTTP contract, complete audio, and shared request scheduling."""

import asyncio
import io
import json
import time
import traceback
import wave

from copy import deepcopy

import httpx
import pytest

from tts_service import dobao
from tts_service.dobao import DoBaoTTSService
from core.dobao_voices import CLASSIC_DOBAO_VOICE, DEFAULT_DOBAO_VOICE, get_dobao_voices


@pytest.fixture
def dobao_config(config):
    names = ('dobaoApiUrl', 'dobaoVoice', 'dobaoSpeed', 'dobaoTimeout')
    saved = {name: deepcopy(getattr(config, name).value) for name in names}
    config.dobaoApiUrl.value = 'http://127.0.0.1:9882'
    config.dobaoVoice.value = 'taozi'
    config.dobaoSpeed.value = 1.0
    config.dobaoTimeout.value = 90
    yield config
    for name, value in saved.items():
        getattr(config, name).value = value


def wav_bytes(rate=24000):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b'\x01\x00' * 24)
    return output.getvalue()


def mock_client(monkeypatch, handler):
    original = httpx.AsyncClient
    options = []

    def create(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', create)
    return options


@pytest.mark.asyncio
async def test_request_snapshot_complete_wav_and_no_client_credentials(dobao_config, monkeypatch):
    dobao_config.dobaoApiUrl.value = 'http://127.0.0.1:9882/tts/'
    dobao_config.dobaoVoice.value = 'taozi-classic'
    dobao_config.dobaoSpeed.value = 1.2
    dobao_config.dobaoTimeout.value = 37
    service = DoBaoTTSService()
    dobao_config.dobaoVoice.value = 'taozi'
    dobao_config.dobaoSpeed.value = 1.0
    requests = []
    audio = wav_bytes()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=audio, headers={'Content-Type': 'audio/wav'})

    options = mock_client(monkeypatch, handler)
    assert await service.text_to_speech('你好，桃子。') == audio
    assert len(requests) == 1
    request = requests[0]
    assert request.method == 'POST'
    assert str(request.url) == 'http://127.0.0.1:9882/tts'
    assert json.loads(request.content) == {
        'text': '你好，桃子。', 'voice': CLASSIC_DOBAO_VOICE, 'speed': 1.2, 'format': 'wav',
    }
    assert 'cookie' not in request.headers and 'authorization' not in request.headers
    assert options == [{'timeout': 37, 'trust_env': False, 'follow_redirects': False}]
    assert not hasattr(service, 'stream_speech')


@pytest.mark.asyncio
@pytest.mark.parametrize('voice,expected', [
    ('taozi', DEFAULT_DOBAO_VOICE), ('taozi-classic', CLASSIC_DOBAO_VOICE),
    ('', CLASSIC_DOBAO_VOICE), (None, CLASSIC_DOBAO_VOICE),
])
async def test_voice_override_is_an_independent_snapshot(dobao_config, monkeypatch, voice, expected):
    dobao_config.dobaoVoice.value = 'taozi-classic'
    service = DoBaoTTSService(voice=voice)
    assert dobao_config.dobaoVoice.value == 'taozi-classic'
    dobao_config.dobaoVoice.value = 'taozi'
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=wav_bytes())

    mock_client(monkeypatch, handler)
    await service.text_to_speech('独立音色')
    assert len(sent) == 1 and sent[0]['voice'] == expected
    assert dobao_config.dobaoVoice.value == 'taozi'


@pytest.mark.asyncio
@pytest.mark.parametrize('voice', ['   ', 'x' * 201, 'voice\ncontrol', 123])
async def test_invalid_voice_override_is_rejected_before_network(dobao_config, monkeypatch, voice):
    service = DoBaoTTSService(voice=voice)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **_: pytest.fail('must not make a request'))
    with pytest.raises(ValueError, match='请选择豆包音色'):
        await service.text_to_speech('测试')


@pytest.mark.asyncio
@pytest.mark.parametrize('voice', [row['id'] for row in get_dobao_voices()] + ['future_custom_voice_bigtts'])
async def test_all_catalog_and_future_voice_ids_are_sent_unchanged(dobao_config, monkeypatch, voice):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=wav_bytes())

    mock_client(monkeypatch, handler)
    await DoBaoTTSService(voice=f'  {voice}  ').text_to_speech('任意豆包音色')
    assert len(sent) == 1 and sent[0]['voice'] == voice


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code,expected', [
    (503, 'LOGIN_REQUIRED', '登录'),
    (429, 'BUSY', '等待 7 秒'),
    (502, 'UPSTREAM_ERROR', '豆包拒绝'),
    (503, 'UPSTREAM_PAUSED', '手动恢复'),
    (503, 'UPSTREAM_BLOCKED', '豆包限制'),
    (429, 'UPSTREAM_RATE_LIMITED', '频率受限'),
    (401, 'UPSTREAM_AUTH', '重新登录'),
    (302, 'unknown', '302'),
    (401, 'unknown', '拒绝访问'),
    (500, {'reflected': 'test-only-cookie'}, '500'),
])
async def test_errors_are_sanitized_and_never_retried(dobao_config, monkeypatch, status, code, expected):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={'error': {'code': code, 'message': 'test-only-cookie'}},
                              headers={'Retry-After': '7', 'Location': 'https://example.invalid'})

    mock_client(monkeypatch, handler)
    with pytest.raises(ValueError) as raised:
        await DoBaoTTSService().text_to_speech('测试')
    assert expected in str(raised.value)
    assert 'test-only-cookie' not in ''.join(traceback.format_exception(raised.value))
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [httpx.ReadTimeout, httpx.ConnectError])
async def test_network_errors_hide_reflected_credentials(dobao_config, monkeypatch, error_type):
    calls = []

    def handler(request):
        calls.append(request)
        raise error_type('test-only-cookie', request=request)

    mock_client(monkeypatch, handler)
    with pytest.raises(ValueError) as raised:
        await DoBaoTTSService().text_to_speech('测试')
    assert 'test-only-cookie' not in ''.join(traceback.format_exception(raised.value))
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [b'', b'{"secret":"test-only-cookie"}', wav_bytes()[:-3], wav_bytes(16000)])
async def test_rejects_non_audio_and_truncated_or_wrong_format_wav(dobao_config, monkeypatch, body):
    mock_client(monkeypatch, lambda _: httpx.Response(200, content=body))
    with pytest.raises(ValueError, match='完整.*WAV'):
        await DoBaoTTSService().text_to_speech('测试')


@pytest.mark.asyncio
@pytest.mark.parametrize('url', [
    'file:///tmp/a', 'http://user:test-only-cookie@127.0.0.1:9882',
    'http://127.0.0.1:9882?cookie=test-only-cookie', 'http://127.0.0.1:bad',
])
async def test_unsafe_or_invalid_address_rejected_before_network(dobao_config, monkeypatch, url):
    dobao_config.dobaoApiUrl.value = url
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **_: pytest.fail('must not make a request'))
    with pytest.raises(ValueError) as raised:
        await DoBaoTTSService().text_to_speech('测试')
    assert 'test-only-cookie' not in ''.join(traceback.format_exception(raised.value))


@pytest.mark.asyncio
async def test_different_adapters_serialize_and_keep_real_three_second_interval(dobao_config, monkeypatch):
    starts = []
    active = 0
    peak = 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        starts.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.03)
        active -= 1
        return httpx.Response(200, content=wav_bytes())

    mock_client(monkeypatch, handler)
    await asyncio.gather(DoBaoTTSService().text_to_speech('一'), DoBaoTTSService().text_to_speech('二'))
    assert peak == 1
    assert len(starts) == 2 and starts[1] - starts[0] >= 2.99


@pytest.mark.asyncio
async def test_delayed_client_setup_does_not_shorten_server_arrival_interval(dobao_config, monkeypatch):
    original = httpx.AsyncClient
    arrivals = []
    clients = 0

    def handler(request):
        arrivals.append(asyncio.get_running_loop().time())
        if len(arrivals) > 1 and arrivals[-1] - arrivals[-2] < 3.0:
            return httpx.Response(429, json={'error': {'code': 'BUSY'}}, headers={'Retry-After': '1'})
        return httpx.Response(200, content=wav_bytes())

    def create(**kwargs):
        nonlocal clients
        clients += 1
        if clients == 1:
            # Client initialization can block on its first import/configuration.
            # The API measures arrivals, so this time must not consume spacing.
            time.sleep(0.35)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', create)
    await asyncio.gather(DoBaoTTSService().text_to_speech('一'), DoBaoTTSService().text_to_speech('二'))
    assert len(arrivals) == 2
    assert arrivals[1] - arrivals[0] >= 3.0


@pytest.mark.asyncio
async def test_cancelled_waiter_never_sends_and_does_not_block_next_request(dobao_config, monkeypatch):
    monkeypatch.setattr(dobao, 'MIN_REQUEST_INTERVAL', 0.03)
    first_started, release = asyncio.Event(), asyncio.Event()
    texts = []

    async def handler(request):
        text = json.loads(request.content)['text']
        texts.append(text)
        if text == '一':
            first_started.set()
            await release.wait()
        return httpx.Response(200, content=wav_bytes())

    mock_client(monkeypatch, handler)
    first = asyncio.create_task(DoBaoTTSService().text_to_speech('一'))
    await first_started.wait()
    cancelled = asyncio.create_task(DoBaoTTSService().text_to_speech('二'))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    last = asyncio.create_task(DoBaoTTSService().text_to_speech('三'))
    release.set()
    await asyncio.wait_for(asyncio.gather(first, last), 1)
    assert texts == ['一', '三']


@pytest.mark.asyncio
async def test_cancelled_inflight_request_releases_gate_and_preserves_interval(dobao_config, monkeypatch):
    monkeypatch.setattr(dobao, 'MIN_REQUEST_INTERVAL', 0.05)
    started = asyncio.Event()
    starts = []

    async def handler(request):
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 1:
            started.set()
            await asyncio.Event().wait()
        return httpx.Response(200, content=wav_bytes())

    mock_client(monkeypatch, handler)
    first = asyncio.create_task(DoBaoTTSService().text_to_speech('一'))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(DoBaoTTSService().text_to_speech('二'), 1)
    assert len(starts) == 2 and starts[1] - starts[0] >= 0.045


@pytest.mark.asyncio
async def test_cancel_during_spacing_sends_nothing_and_retry_after_delays_next_manual_call(dobao_config, monkeypatch):
    starts = []

    def handler(request):
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 1:
            return httpx.Response(429, json={'error': {'code': 'BUSY'}}, headers={'Retry-After': '1'})
        return httpx.Response(200, content=wav_bytes())

    monkeypatch.setattr(dobao, 'MIN_REQUEST_INTERVAL', 0.02)
    mock_client(monkeypatch, handler)
    with pytest.raises(ValueError, match='未自动重试'):
        await DoBaoTTSService().text_to_speech('一')
    cancelled = asyncio.create_task(DoBaoTTSService().text_to_speech('二'))
    await asyncio.sleep(0.01)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert len(starts) == 1
    await asyncio.wait_for(DoBaoTTSService().text_to_speech('三'), 2)
    assert len(starts) == 2 and starts[1] - starts[0] >= 0.99


def test_registration_preserves_default_until_explicit_selection(dobao_config):
    from core.const import VISIBLE_TTS_SERVICES
    from models.service import ServiceType
    from tts_service import get_tts_service

    previous = dobao_config.activeTTS.value
    service = DoBaoTTSService()
    assert dobao_config.activeTTS.value == previous
    assert ServiceType.DOBAO in VISIBLE_TTS_SERVICES
    dobao_config.activeTTS.value = ServiceType.DOBAO
    assert isinstance(get_tts_service(), DoBaoTTSService)
    assert not hasattr(service._settings, 'cookie')
