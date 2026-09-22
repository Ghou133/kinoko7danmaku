"""Explain GPU exhaustion without replaying failed speech or masking disconnects."""

import asyncio

import httpx
import pytest

from tests.test_speech import wav_bytes


OOM = {'code': 'cuda_out_of_memory', 'message': 'untrusted private body', 'needs_restart': True}
OOM_HEALTH = {
    'status': 'error', 'ready': False, 'model_loaded': True,
    'needs_restart': True, 'last_error': OOM,
}
DOTS_SCHEMA = {
    'info': {'title': 'dots.tts 弹幕朗读服务'},
    'paths': {'/tts': {'post': {}}, '/tts/stream': {'post': {}}},
}


@pytest.fixture
def dots(config):
    from tts_service import dots as module

    config.dotsApiUrl.value = 'http://127.0.0.1:9881/proxy/tts/stream'
    config.dotsSpeed.value = 1.0
    config.dotsVolume.value = 1.0
    return module


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    calls, options = [], []

    async def handle(request):
        calls.append(request)
        result = handler(request)
        return await result if asyncio.iscoroutine(result) else result

    def client(**kwargs):
        assert kwargs['trust_env'] is False
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    return calls, options


async def speak(service, streaming):
    if streaming:
        return [chunk async for chunk in service.stream_speech('测试')]
    return await service.text_to_speech('测试')


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('body', [OOM, {'detail': OOM}])
async def test_structured_503_reports_oom_without_retry(dots, monkeypatch, streaming, body):
    calls, _ = mock_http(monkeypatch, lambda _: httpx.Response(503, json=body))
    with pytest.raises(ValueError) as raised:
        await speak(dots.DotsTTSService(), streaming)
    assert '显存不足' in str(raised.value)
    assert '重启 dots.tts API' in str(raised.value)
    assert 'untrusted' not in str(raised.value)
    assert len(calls) == 1 and calls[0].method == 'POST'


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('body', [
    {'detail': 'CUDA out of memory untrusted private body'},
    {'code': 'unknown', 'message': 'untrusted private body'},
])
async def test_other_http_errors_do_not_echo_remote_body_or_claim_oom(dots, monkeypatch, streaming, body):
    calls, _ = mock_http(monkeypatch, lambda _: httpx.Response(500, json=body))
    with pytest.raises(ValueError) as raised:
        await speak(dots.DotsTTSService(), streaming)
    assert '500' in str(raised.value)
    assert '显存不足' not in str(raised.value)
    assert 'untrusted' not in str(raised.value)
    assert len(calls) == 1


class BrokenStream(httpx.AsyncByteStream):
    def __init__(self, request, *, partial):
        self.request = request
        self.partial = partial

    async def __aiter__(self):
        if self.partial:
            yield wav_bytes()[:50]
        raise httpx.RemoteProtocolError('incomplete chunked read', request=self.request)


@pytest.mark.asyncio
@pytest.mark.parametrize('partial', [False, True])
async def test_broken_stream_checks_health_once_and_preserves_incremental_audio(dots, config, monkeypatch, partial):
    def handle(request):
        if request.method == 'POST':
            return httpx.Response(200, stream=BrokenStream(request, partial=partial))
        assert request.url.path == '/proxy/health'
        return httpx.Response(200, json=OOM_HEALTH)

    calls, options = mock_http(monkeypatch, handle)
    service = dots.DotsTTSService()
    config.dotsApiUrl.value = 'http://localhost:1/changed'
    stream = service.stream_speech('测试')
    if partial:
        # Audio remains incremental: the diagnostic only runs after a failure.
        assert (await anext(stream)).data
        assert len(calls) == 1
    with pytest.raises(ValueError) as raised:
        await anext(stream)
    message = str(raised.value)
    assert '显存不足' in message and '重启 dots.tts API' in message
    assert ('已收到部分音频' if partial else '未收到可播放音频') in message
    assert 'untrusted' not in message
    assert [(r.method, r.url.path) for r in calls] == [
        ('POST', '/proxy/tts/stream'), ('GET', '/proxy/health'),
    ]
    assert options[1] == {
        'trust_env': False, 'follow_redirects': False, 'timeout': dots._OOM_HEALTH_TIMEOUT,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('health_failure', ['unreachable', 'timeout', 'malformed', 'other-error', 'healthy', 'unauthorized'])
async def test_diagnostic_failure_keeps_original_disconnect_and_never_replays(dots, monkeypatch, health_failure):
    monkeypatch.setattr(dots, '_OOM_HEALTH_TIMEOUT', 0.01)

    async def handle(request):
        if request.method == 'POST':
            return httpx.Response(200, stream=BrokenStream(request, partial=False))
        if health_failure == 'unreachable':
            raise httpx.ConnectError('untrusted private body', request=request)
        if health_failure == 'timeout':
            await asyncio.sleep(10)
        if health_failure == 'malformed':
            return httpx.Response(200, text='untrusted private body')
        if health_failure == 'unauthorized':
            return httpx.Response(401, json=OOM_HEALTH)
        return httpx.Response(200, json=(
            {'status': 'error', 'last_error': {'code': 'unknown', 'message': 'untrusted private body'}}
            if health_failure == 'other-error' else {'status': 'ok', 'last_error': OOM}
        ))

    calls, _ = mock_http(monkeypatch, handle)
    with pytest.raises(ValueError) as raised:
        await speak(dots.DotsTTSService(), True)
    assert '流式连接被服务端中断' in str(raised.value)
    assert '显存不足' not in str(raised.value)
    assert 'untrusted' not in str(raised.value)
    assert isinstance(raised.value.__cause__, httpx.RemoteProtocolError)
    assert [r.method for r in calls] == ['POST', 'GET']


@pytest.mark.asyncio
async def test_oom_health_is_unavailable_with_accurate_route_reason(monkeypatch):
    from core import tts_availability as availability

    calls, _ = mock_http(monkeypatch, lambda r: httpx.Response(
        200, json=OOM_HEALTH if r.url.path == '/health' else DOTS_SCHEMA,
    ))
    result = await availability.check_service_availability(
        'dots_tts', 'http://127.0.0.1:9881', force=True,
    )
    assert result.available is False
    assert '显存不足' in result.detail and '重启' in result.detail
    assert '尚未加载' not in result.detail
    assert 'untrusted' not in result.detail
    assert [r.method for r in calls] == ['GET', 'GET']
    availability.invalidate_service_availability('dots_tts', 'http://127.0.0.1:9881')


@pytest.mark.asyncio
async def test_health_oom_does_not_bypass_dots_identity(monkeypatch):
    from core import tts_availability as availability

    mock_http(monkeypatch, lambda r: httpx.Response(
        200, json=OOM_HEALTH if r.url.path == '/health' else {},
    ))
    result = await availability.check_service_availability(
        'dots_tts', 'http://127.0.0.1:9881', force=True,
    )
    assert result.available is False
    assert '不是兼容' in result.detail
    assert '显存不足' not in result.detail
    availability.invalidate_service_availability('dots_tts', 'http://127.0.0.1:9881')
