"""Seed-TTS 2.0 protocol checks using an in-memory HTTP server."""

import asyncio
import base64
import io
import json
import traceback
import uuid
import wave

from copy import deepcopy

import httpx
import pytest

from tts_service.seed_tts import SAMPLE_RATE, SEED_TTS_URL, SeedTTSService


@pytest.fixture
def seed_config(config):
    names = (
        'seedTtsApiKey', 'seedTtsVoice', 'seedTtsSpeed',
        'seedTtsStreaming', 'seedTtsTimeout',
    )
    saved = {name: deepcopy(getattr(config, name).value) for name in names}
    config.seedTtsApiKey.value = 'test-only-secret'
    config.seedTtsVoice.value = 'zh_female_vv_uranus_bigtts'
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


def frame(*, code=0, data=None, **extra):
    return json.dumps({'code': code, 'data': data, **extra}, ensure_ascii=False).encode() + b'\n'


@pytest.mark.asyncio
async def test_request_snapshot_override_and_wav(seed_config, monkeypatch):
    seed_config.seedTtsSpeed.value = 32
    seed_config.seedTtsTimeout.value = 37
    seed_config.seedTtsStreaming.value = True
    service = SeedTTSService(speaker=' zh_male_dayi_uranus_bigtts ')
    seed_config.seedTtsApiKey.value = 'changed-secret'
    seed_config.seedTtsVoice.value = 'changed-voice'
    seed_config.seedTtsSpeed.value = -10
    seed_config.seedTtsTimeout.value = 5
    seed_config.seedTtsStreaming.value = False

    requests = []
    pcm = b'\x01\x00\x02\x00\x03\x00'

    def handle(request):
        requests.append(request)
        body = frame(data=base64.b64encode(pcm[:3]).decode())
        body += frame(data=None, usage={'text_words': 2})
        body += frame(data=base64.b64encode(pcm[3:]).decode())
        body += frame(code=20000000, message='OK')
        return httpx.Response(200, content=body, headers={'Content-Type': 'application/json'})

    options = mock_client(monkeypatch, handle)
    wav_bytes = await service.text_to_speech('测试')
    with wave.open(io.BytesIO(wav_bytes), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, SAMPLE_RATE)
        assert wav.readframes(wav.getnframes()) == pcm
    assert service.streaming_enabled
    assert len(requests) == 1
    assert str(requests[0].url) == SEED_TTS_URL
    assert requests[0].headers['x-api-key'] == 'test-only-secret'
    assert requests[0].headers['x-api-resource-id'] == 'seed-tts-2.0'
    assert uuid.UUID(requests[0].headers['x-api-request-id'])
    assert requests[0].headers['content-type'] == 'application/json'
    assert json.loads(requests[0].content) == {
        'req_params': {
            'text': '测试',
            'speaker': 'zh_male_dayi_uranus_bigtts',
            'audio_params': {'format': 'pcm', 'sample_rate': SAMPLE_RATE, 'speech_rate': 32},
        },
    }
    assert options == [{'timeout': 37, 'follow_redirects': False}]


@pytest.mark.asyncio
async def test_stream_parses_json_across_transport_boundaries(seed_config, monkeypatch):
    pcm = b'\x01\x00\x02\x00'
    # JSON objects need not line up with HTTP chunks or even have a newline.
    response = frame(data=base64.b64encode(pcm[:1]).decode(), sentence={'text': '你'}).strip()
    response += frame(data=base64.b64encode(pcm[1:]).decode()).strip()
    response += frame(code=20000000).strip()
    split_utf8 = response.index('你'.encode()) + 1
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            # A transport fragment splits JSON, including a UTF-8 character.
            for part in (response[:12], response[12:split_utf8],
                         response[split_utf8:split_utf8 + 1], response[split_utf8 + 1:]):
                yield part

        async def aclose(self):
            closed.append(True)

    mock_client(monkeypatch, lambda _: httpx.Response(200, stream=Stream()))
    chunks = [chunk async for chunk in SeedTTSService().stream_speech('测试')]
    assert b''.join(chunk.data for chunk in chunks) == pcm
    assert all(chunk.sample_rate == SAMPLE_RATE and chunk.channels == 1 for chunk in chunks)
    assert closed


@pytest.mark.asyncio
async def test_first_audio_arrives_before_response_finishes(seed_config, monkeypatch):
    finished = []
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame(data=base64.b64encode(b'\x01\x00').decode())
            finished.append(True)
            yield frame(code=20000000)

        async def aclose(self):
            closed.append(True)

    mock_client(monkeypatch, lambda _: httpx.Response(200, stream=Stream()))
    stream = SeedTTSService().stream_speech('测试')
    assert (await anext(stream)).data == b'\x01\x00'
    assert not finished
    await stream.aclose()
    assert closed and not finished


@pytest.mark.asyncio
async def test_cancellation_closes_connection(seed_config, monkeypatch):
    started = asyncio.Event()
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame(data=base64.b64encode(b'\x01\x00').decode())
            started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    mock_client(monkeypatch, lambda _: httpx.Response(200, stream=Stream()))
    stream = SeedTTSService().stream_speech('测试')
    await anext(stream)
    task = asyncio.create_task(anext(stream))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [302, 401, 402, 403, 404, 429, 500, 503])
async def test_http_errors_do_not_expose_key_or_retry(seed_config, monkeypatch, status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, content=b'proxy reflected test-only-secret', headers={
            'Location': 'https://example.invalid/redirect',
        })

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError) as raised:
        await SeedTTSService().text_to_speech('测试')
    assert str(status) in str(raised.value)
    assert 'test-only-secret' not in ''.join(traceback.format_exception(raised.value))
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_transport_errors_are_sanitized(seed_config, monkeypatch, error_type):
    requests = []

    def handle(request):
        requests.append(request)
        raise error_type('proxy reflected test-only-secret', request=request)

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError) as raised:
        await SeedTTSService().text_to_speech('测试')
    assert 'test-only-secret' not in ''.join(traceback.format_exception(raised.value))
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    b'',
    b'{"code":0,"data":"AQ',
    frame(data='not valid base64'),
    frame(data=base64.b64encode(b'\x01').decode()),
    frame(code=40000000, message='test-only-secret'),
    b'{"code":0,"data":23}\n',
])
async def test_invalid_response_rejected_without_key_leak(seed_config, monkeypatch, body):
    mock_client(monkeypatch, lambda _: httpx.Response(200, content=body))
    with pytest.raises(ValueError) as raised:
        await SeedTTSService().text_to_speech('测试')
    assert 'test-only-secret' not in ''.join(traceback.format_exception(raised.value))


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('seedTtsApiKey', ''),
    ('seedTtsApiKey', 'bad\r\nInjected'),
    ('seedTtsApiKey', '中文'),
    ('seedTtsVoice', ''),
])
async def test_invalid_configuration_rejected_before_request(seed_config, monkeypatch, field, value):
    getattr(seed_config, field).value = value

    def forbidden(**kwargs):
        pytest.fail('invalid configuration must not send a request')

    monkeypatch.setattr(httpx, 'AsyncClient', forbidden)
    with pytest.raises(ValueError):
        await SeedTTSService().text_to_speech('测试')


@pytest.mark.asyncio
async def test_empty_user_binding_uses_global_voice(seed_config, monkeypatch):
    voices = []

    def handle(request):
        voices.append(json.loads(request.content)['req_params']['speaker'])
        return httpx.Response(200, content=frame(data=base64.b64encode(b'\x00\x00').decode()))

    mock_client(monkeypatch, handle)
    await SeedTTSService(speaker=' ').text_to_speech('测试')
    assert voices == ['zh_female_vv_uranus_bigtts']
