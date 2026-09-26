import asyncio
from types import SimpleNamespace

import httpx
import pytest

from core import tts_availability as availability


DOTS_SCHEMA = {
    'info': {'title': 'dots.tts 弹幕朗读服务'},
    'paths': {'/tts': {'post': {}}, '/tts/stream': {'post': {}}},
}
GPT_SCHEMA = {
    'paths': {'/tts': {'post': {}}, '/set_gpt_weights': {'get': {}},
              '/set_sovits_weights': {'get': {}}},
}
GPT_STATUS = {'protocol': 1, 'atomic_model_selection': True, 'resident_model_reuse': True}


@pytest.fixture(autouse=True)
def isolated_cache():
    availability._cache.clear()
    yield
    availability._cache.clear()


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    calls = []

    async def handle(request):
        assert request.method == 'GET'
        assert request.url.path.split('/')[-1] not in ('tts', 'set_gpt_weights', 'set_sovits_weights')
        calls.append(request)
        response = handler(request)
        return await response if asyncio.iscoroutine(response) else response

    def client(**kwargs):
        assert kwargs['trust_env'] is False
        assert kwargs['follow_redirects'] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(availability.httpx, 'AsyncClient', client)
    return calls


def dots_response(request, *, loaded=True, status='ok', schema=DOTS_SCHEMA):
    if request.url.path.endswith('/health'):
        return httpx.Response(200, json={'status': status, 'model_loaded': loaded})
    assert request.url.path.endswith('/openapi.json')
    return httpx.Response(200, json=schema)


@pytest.mark.asyncio
async def test_dots_confirms_model_ready_and_service_identity(monkeypatch):
    calls = mock_http(monkeypatch, dots_response)
    result = await availability.check_service_availability('dots_tts', 'http://localhost:9881')
    assert result.available
    assert [call.url.path for call in calls] == ['/health', '/openapi.json']


@pytest.mark.asyncio
@pytest.mark.parametrize('overrides,available,detail', [
    ({}, True, '登录信息已配置'),
    ({'upstream_verified': False}, True, '首次合成'),
    ({'credential_configured': False}, False, '尚未登录'),
    ({'upstream_paused': {'code': 'UPSTREAM_BLOCKED'}}, False, '已暂停'),
    ({'service': 'unrelated'}, False, '不是兼容'),
    ({'status': 'stopped'}, False, '不是兼容'),
])
async def test_dobao_local_health_requires_login_and_unpaused_state(monkeypatch, overrides, available, detail):
    document = {'service': 'dobao-local-api', 'status': 'running', 'credential_configured': True,
                'upstream_verified': True, 'upstream_paused': None}
    document.update(overrides)
    calls = mock_http(monkeypatch, lambda _: httpx.Response(200, json=document))
    result = await availability.check_service_availability('dobao_tts', 'http://127.0.0.1:9882/tts/')
    assert result.available is available
    assert detail in result.detail
    assert [call.url.path for call in calls] == ['/health']


@pytest.mark.asyncio
async def test_doubao_force_refresh_observes_stop_and_restart_without_synthesis(monkeypatch):
    ready = True

    def handle(request):
        if not ready:
            raise httpx.ConnectError('test-only-private-value', request=request)
        return httpx.Response(200, json={
            'service': 'dobao-local-api', 'status': 'running',
            'credential_configured': True, 'upstream_paused': None,
        })

    calls = mock_http(monkeypatch, handle)
    check = lambda **kwargs: availability.check_service_availability(
        'dobao_tts', 'http://127.0.0.1:9882', **kwargs,
    )
    assert (await check()).available
    ready = False
    assert (await check()).available  # UI status can use its short cache.
    result = await check(force=True)
    assert not result.available and 'test-only-private-value' not in result.detail
    ready = True
    assert (await check(force=True)).available
    assert [request.url.path for request in calls] == ['/health'] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize('loaded,status', [(False, 'loading'), (False, 'ok'), (True, 'loading'), (1, 'ok')])
async def test_dots_rejects_unready_model(monkeypatch, loaded, status):
    mock_http(monkeypatch, lambda r: dots_response(r, loaded=loaded, status=status))
    result = await availability.check_service_availability('dots_tts', 'http://localhost:9881')
    assert not result.available
    assert '尚未加载' in result.detail


@pytest.mark.asyncio
@pytest.mark.parametrize('schema', [GPT_SCHEMA, {}, {'info': {'title': 'dots.tts'}, 'paths': {}}])
async def test_dots_rejects_other_service_despite_healthy_status(monkeypatch, schema):
    mock_http(monkeypatch, lambda r: dots_response(r, schema=schema))
    result = await availability.check_service_availability('dots_tts', 'http://localhost:9880')
    assert not result.available
    assert '不是兼容' in result.detail


@pytest.mark.asyncio
async def test_gpt_bridge_recognized_without_loading_a_model(monkeypatch):
    calls = mock_http(monkeypatch, lambda r: httpx.Response(200, json=GPT_STATUS))
    result = await availability.check_service_availability('gpt_sovits', 'http://localhost:9880')
    assert result.available
    assert [r.url.path for r in calls] == ['/kinoko/status']


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_status', [404, 405])
async def test_gpt_official_api_fallback(monkeypatch, missing_status):
    def handle(request):
        return (httpx.Response(missing_status) if request.url.path == '/kinoko/status'
                else httpx.Response(200, json=GPT_SCHEMA))
    calls = mock_http(monkeypatch, handle)
    assert (await availability.check_service_availability('gpt_sovits', 'http://localhost:9880')).available
    assert [r.url.path for r in calls] == ['/kinoko/status', '/openapi.json']


@pytest.mark.asyncio
async def test_gpt_gradio_requires_all_adapter_operations(monkeypatch):
    names = ['get_tts_wav', '/change_gpt_weights', 'change_sovits_weights']

    def handle(request):
        if request.url.path != '/config':
            return httpx.Response(404)
        return httpx.Response(200, json={'dependencies': [{'api_name': n} for n in names]})

    calls = mock_http(monkeypatch, handle)
    assert (await availability.check_service_availability('gpt_sovits', 'http://localhost:9872')).available
    assert [r.url.path for r in calls] == ['/kinoko/status', '/openapi.json', '/config']
    names.pop()
    result = await availability.check_service_availability('gpt_sovits', 'http://localhost:9872', force=True)
    assert not result.available


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [{}, {'protocol': 1}, {'status': 'ok'}, {'protocol': 2, **{
    'atomic_model_selection': True, 'resident_model_reuse': True}}])
async def test_gpt_does_not_accept_generic_200(monkeypatch, status):
    calls = mock_http(monkeypatch, lambda r: httpx.Response(200, json=status))
    result = await availability.check_service_availability('gpt_sovits', 'http://localhost:9880')
    assert not result.available
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_gpt_openapi_must_have_weight_routes_and_methods(monkeypatch):
    def handle(request):
        if request.url.path == '/kinoko/status':
            return httpx.Response(404)
        if request.url.path == '/openapi.json':
            return httpx.Response(200, json=DOTS_SCHEMA)
        return httpx.Response(200, json={'dependencies': [{'api_name': 'predict'}]})
    mock_http(monkeypatch, handle)
    assert not (await availability.check_service_availability('gpt_sovits', 'http://localhost:9881')).available


@pytest.mark.asyncio
async def test_cloud_and_unsupported_services_never_probe(monkeypatch):
    def forbidden(**kwargs):
        pytest.fail('Cloud services must not make a readiness request')
    monkeypatch.setattr(availability.httpx, 'AsyncClient', forbidden)
    assert (await availability.check_service_availability('fish_audio', 'https://unused', force=True)).available
    assert not (await availability.check_service_availability('unknown', 'http://localhost')).available


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['', 'localhost:9881', 'file:///tmp/test', 'http://localhost:99999',
                                 'http://user:secret@localhost:9881', 'http://localhost/?key=secret'])
async def test_invalid_urls_fail_without_network_or_credentials(monkeypatch, url):
    def forbidden(**kwargs):
        pytest.fail('Invalid URLs must not make a request')
    monkeypatch.setattr(availability.httpx, 'AsyncClient', forbidden)
    result = await availability.check_service_availability('dots_tts', url)
    assert not result.available
    assert 'secret' not in result.detail


@pytest.mark.asyncio
async def test_equivalent_endpoint_urls_share_cache_and_preserve_proxy_prefix(monkeypatch):
    calls = mock_http(monkeypatch, dots_response)
    for url in [' HTTP://LOCALHOST:80/proxy/tts/stream/ ', 'http://localhost/proxy/tts',
                'http://localhost/proxy/', 'http://localhost/proxy/health']:
        assert (await availability.check_service_availability('dots_tts', url)).available
    assert [r.url.path for r in calls] == ['/proxy/health', '/proxy/openapi.json']


@pytest.mark.asyncio
async def test_address_and_service_changes_do_not_reuse_cached_result(monkeypatch):
    def handle(request):
        return (httpx.Response(200, json=GPT_STATUS) if request.url.path == '/kinoko/status'
                else dots_response(request))
    calls = mock_http(monkeypatch, handle)
    assert (await availability.check_service_availability('dots_tts', 'http://localhost:9881')).available
    assert (await availability.check_service_availability('dots_tts', 'http://localhost:9882')).available
    assert (await availability.check_service_availability('gpt_sovits', 'http://localhost:9881')).available
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_positive_and_negative_cache_expiry_and_force_refresh(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(availability, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    loaded = [True]
    calls = mock_http(monkeypatch, lambda r: dots_response(r, loaded=loaded[0]))
    check = lambda **kw: availability.check_service_availability('dots_tts', 'http://localhost:9881', **kw)
    assert (await check()).available
    loaded[0] = False
    now[0] = 109.9
    assert (await check()).available
    assert len(calls) == 2
    now[0] = 110.0
    assert not (await check()).available
    assert len(calls) == 4
    loaded[0] = True
    now[0] = 111.9
    assert not (await check()).available
    assert len(calls) == 4
    now[0] = 112.0
    assert (await check()).available
    loaded[0] = False
    assert not (await check(force=True)).available
    assert len(calls) == 8


@pytest.mark.asyncio
async def test_overall_deadline_bounds_slow_checks_without_blocking_loop(monkeypatch):
    monkeypatch.setattr(availability, '_CHECK_TIMEOUT', 0.02)
    ticked = asyncio.Event()

    async def slow(request):
        await asyncio.sleep(0)
        ticked.set()
        await asyncio.sleep(10)
        return httpx.Response(200, json=GPT_STATUS)

    mock_http(monkeypatch, slow)
    result = await availability.check_service_availability('gpt_sovits', 'http://localhost:9880')
    assert not result.available
    assert '超时' in result.detail
    assert ticked.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['connection', 'bad-json', 'unauthorized', 'redirect', 'invalid-url'])
async def test_safe_error_details_do_not_leak_server_bodies_or_request_exceptions(monkeypatch, failure):
    secret = 'private-error-body-and-api-key'

    def handle(request):
        if failure == 'connection':
            raise httpx.ConnectError(secret, request=request)
        if failure == 'invalid-url':
            raise httpx.InvalidURL(secret)
        if failure == 'bad-json':
            return httpx.Response(200, text=secret)
        return httpx.Response(401 if failure == 'unauthorized' else 302, text=secret)

    calls = mock_http(monkeypatch, handle)
    result = await availability.check_service_availability('gpt_sovits', 'http://localhost:9880')
    assert not result.available
    assert secret not in result.detail
    assert 'localhost' not in result.detail
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_concurrent_checks_share_request_and_one_cancel_does_not_cancel_other(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=GPT_STATUS)

    calls = mock_http(monkeypatch, handle)
    one = asyncio.create_task(availability.check_service_availability('gpt_sovits', 'http://localhost:9880'))
    await started.wait()
    two = asyncio.create_task(availability.check_service_availability('gpt_sovits', 'http://localhost:9880', force=True))
    await asyncio.sleep(0)
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    release.set()
    assert (await two).available
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancelled_only_waiter_propagates_without_caching(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle(request):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
        return httpx.Response(200, json=GPT_STATUS)

    mock_http(monkeypatch, handle)
    task = asyncio.create_task(availability.check_service_availability('gpt_sovits', 'http://localhost:9880'))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), 1)
    assert not availability._cache


@pytest.mark.asyncio
async def test_invalidation_discards_only_selected_key_without_retry(monkeypatch):
    calls = mock_http(monkeypatch, lambda r: httpx.Response(200, json=GPT_STATUS))
    for port in (9880, 9882):
        assert (await availability.check_service_availability('gpt_sovits', f'http://localhost:{port}')).available
    availability.invalidate_service_availability('gpt_sovits', 'http://localhost:9880/tts/')
    assert len(calls) == 2
    assert (await availability.check_service_availability('gpt_sovits', 'http://localhost:9882')).available
    assert len(calls) == 2
    assert (await availability.check_service_availability('gpt_sovits', 'http://localhost:9880')).available
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_inflight_probe_cannot_restore_invalidated_cache(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=GPT_STATUS)

    mock_http(monkeypatch, handle)
    task = asyncio.create_task(availability.check_service_availability('gpt_sovits', 'http://localhost:9880'))
    await started.wait()
    availability.invalidate_service_availability('gpt_sovits', 'http://localhost:9880')
    release.set()
    assert (await task).available
    assert not availability._cache


@pytest.mark.asyncio
async def test_fallbacks_share_one_deadline(monkeypatch):
    monkeypatch.setattr(availability, '_CHECK_TIMEOUT', 0.15)

    async def handle(request):
        await asyncio.sleep(0.1)
        return (httpx.Response(404) if request.url.path == '/kinoko/status'
                else httpx.Response(200, json=GPT_SCHEMA))

    calls = mock_http(monkeypatch, handle)
    result = await availability.check_service_availability('gpt_sovits', 'http://localhost:9880')
    assert not result.available
    assert '超时' in result.detail
    assert [r.url.path for r in calls] == ['/kinoko/status', '/openapi.json']
