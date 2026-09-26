"""Hosted Fish Audio contracts without issuing remote or billed requests."""

import asyncio
import io
import json
import traceback
import wave

from copy import deepcopy

import httpx
import pytest

from tts_service.fish_audio import FISH_AUDIO_TTS_URL, FishAudioService


@pytest.fixture
def fish_config(config):
    saved = {
        name: deepcopy(getattr(config, name).value)
        for name in dir(config)
        if name.startswith('fishAudio') and hasattr(getattr(config, name), 'value')
    }
    config.fishAudioApiKey.value = 'test-only-secret'
    config.fishAudioReferenceId.value = 'test-voice-id'
    yield config
    for name, value in saved.items():
        getattr(config, name).value = value


def mock_client(monkeypatch, handler):
    original = httpx.AsyncClient
    options = []

    def create(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', create)
    return options


@pytest.mark.asyncio
@pytest.mark.parametrize('whole', [False, True])
async def test_snapshot_parameters_headers_and_pcm_frames(fish_config, monkeypatch, whole):
    initial = {
        'fishAudioApiKey': 'test-only-secret',
        'fishAudioReferenceId': 'old-voice-id',
        'fishAudioModel': 's1',
        'fishAudioSpeed': 1.3,
        'fishAudioVolume': -2.5,
        'fishAudioTemperature': 0.3,
        'fishAudioTopP': 0.6,
        'fishAudioStreaming': not whole,
        'fishAudioLatency': 'balanced',
        'fishAudioTimeout': 37,
    }
    for name, value in initial.items():
        getattr(fish_config, name).value = value
    service = FishAudioService()
    fish_config.fishAudioApiKey.value = 'changed-test-only-secret'
    fish_config.fishAudioReferenceId.value = 'changed-voice-id'
    fish_config.fishAudioModel.value = 's2-pro'
    fish_config.fishAudioSpeed.value = 0.7
    fish_config.fishAudioVolume.value = 6
    fish_config.fishAudioTemperature.value = 0.9
    fish_config.fishAudioTopP.value = 0.9
    fish_config.fishAudioStreaming.value = whole
    fish_config.fishAudioLatency.value = 'low'
    fish_config.fishAudioTimeout.value = 5
    pcm = b'\x01\x00\x02\x00\x03\x00'
    requests = []
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield pcm[:1]
            yield pcm[1:3]
            yield pcm[3:]

        async def aclose(self):
            closed.append(True)

    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=Stream(), headers={'Content-Type': 'audio/pcm'})

    options = mock_client(monkeypatch, handle)
    assert service.streaming_enabled is not whole
    if whole:
        audio = await service.text_to_speech('测试内容')
        with wave.open(io.BytesIO(audio), 'rb') as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 44100)
            assert wav.readframes(wav.getnframes()) == pcm
    else:
        chunks = [chunk async for chunk in service.stream_speech('测试内容')]
        assert b''.join(chunk.data for chunk in chunks) == pcm
        assert all(len(chunk.data) % 2 == 0 and chunk.sample_rate == 44100 and chunk.channels == 1 for chunk in chunks)
    assert closed
    assert len(requests) == 1
    assert str(requests[0].url) == FISH_AUDIO_TTS_URL
    assert requests[0].headers['authorization'] == 'Bearer test-only-secret'
    assert requests[0].headers['model'] == 's1'
    assert requests[0].headers['content-type'] == 'application/json'
    assert json.loads(requests[0].content) == {
        'text': '测试内容',
        'reference_id': 'old-voice-id',
        'format': 'pcm',
        'sample_rate': 44100,
        'temperature': 0.3,
        'top_p': 0.6,
        'prosody': {'speed': 1.3, 'volume': -2.5, 'normalize_loudness': True},
        'latency': 'balanced',
        'normalize': True,
    }
    assert options == [{'timeout': 37, 'follow_redirects': False}]


@pytest.mark.asyncio
async def test_first_audio_before_completion_and_close_releases_connection(fish_config, monkeypatch):
    finished, closed = [], []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'\x01\x00'
            finished.append(True)
            yield b'\x02\x00'

        async def aclose(self):
            closed.append(True)

    mock_client(monkeypatch, lambda _: httpx.Response(200, stream=Stream()))
    stream = FishAudioService().stream_speech('测试')
    assert (await anext(stream)).data == b'\x01\x00'
    assert not finished
    await stream.aclose()
    assert closed and not finished


@pytest.mark.asyncio
async def test_cancellation_while_waiting_closes_stream(fish_config, monkeypatch):
    started = asyncio.Event()
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'\x01\x00'
            started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    mock_client(monkeypatch, lambda _: httpx.Response(200, stream=Stream()))
    stream = FishAudioService().stream_speech('测试')
    await anext(stream)
    task = asyncio.create_task(anext(stream))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [302, 401, 402, 403, 404, 422, 429, 500, 503])
async def test_error_bodies_never_expose_credentials_or_retry(fish_config, monkeypatch, status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            status,
            content=b'proxy reflected Bearer test-only-secret',
            headers={'Location': 'https://example.invalid/redirect'},
        )

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError) as raised:
        await FishAudioService().text_to_speech('测试')
    assert str(status) in str(raised.value)
    assert 'test-only-secret' not in ''.join(traceback.format_exception(raised.value))
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_network_errors_are_sanitized(fish_config, monkeypatch, error_type):
    def handle(request):
        raise error_type('proxy reflected test-only-secret', request=request)

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError) as raised:
        await FishAudioService().text_to_speech('测试')
    assert 'test-only-secret' not in ''.join(traceback.format_exception(raised.value))


@pytest.mark.asyncio
@pytest.mark.parametrize('body,media_type', [
    (b'', 'audio/pcm'),
    (b'\x01\x00\x02', 'audio/pcm'),
    (b'{"key":"test-only-secret"}', 'application/json'),
    (b'RIFFbad-format', 'audio/wav'),
])
async def test_invalid_audio_rejected(fish_config, monkeypatch, body, media_type):
    mock_client(monkeypatch, lambda _: httpx.Response(200, content=body, headers={'Content-Type': media_type}))
    with pytest.raises(ValueError) as raised:
        await FishAudioService().text_to_speech('测试')
    assert 'test-only-secret' not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('fishAudioApiKey', ''),
    ('fishAudioApiKey', 'test-only-secret\r\nInjected'),
    ('fishAudioApiKey', '中文'),
    ('fishAudioReferenceId', ''),
])
async def test_missing_or_invalid_configuration_fails_before_request(fish_config, monkeypatch, field, value):
    getattr(fish_config, field).value = value

    def forbidden(**kwargs):
        pytest.fail('invalid configuration must not send a request')

    monkeypatch.setattr(httpx, 'AsyncClient', forbidden)
    with pytest.raises(ValueError):
        await FishAudioService().text_to_speech('测试')


def test_new_service_registration_and_visible_choices(fish_config):
    from core.const import SUPPORTED_SERVICES, VISIBLE_TTS_SERVICES
    from models.service import ServiceType
    from tts_service import get_tts_service

    fish_config.activeTTS.value = ServiceType.FISH_AUDIO
    assert isinstance(get_tts_service(), FishAudioService)
    assert list(VISIBLE_TTS_SERVICES) == [
        ServiceType.DOTS, ServiceType.GPT_SOVITS, ServiceType.FISH_AUDIO, ServiceType.DOBAO,
    ]
    assert ServiceType.EDGE in SUPPORTED_SERVICES
    assert ServiceType.EDGE not in VISIBLE_TTS_SERVICES
