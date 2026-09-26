"""Short, read-only readiness checks for locally hosted TTS services.

These checks never synthesize speech or change the loaded model. Cloud services
are used directly: a local probe cannot establish their API-key or voice access.
"""

import asyncio
import time
import weakref
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx


@dataclass(frozen=True)
class ServiceAvailability:
    available: bool
    detail: str


@dataclass
class _PendingProbe:
    task: asyncio.Task | None = None
    waiters: int = 0
    cacheable: bool = True


_CHECK_TIMEOUT = 1.0
_POSITIVE_TTL = 10.0
_NEGATIVE_TTL = 2.0
_MAX_CACHE_ENTRIES = 128
_cache: dict[tuple[str, str], tuple[float, ServiceAvailability]] = {}
_pending = weakref.WeakKeyDictionary()


def _base_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if (parts.scheme.lower() not in ('http', 'https') or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise ValueError('invalid service URL')
    port = parts.port  # Also validates malformed/out-of-range port numbers.
    hostname = parts.hostname.lower()
    if ':' in hostname:
        hostname = f'[{hostname}]'
    if port is not None and port != {'http': 80, 'https': 443}[parts.scheme.lower()]:
        hostname += f':{port}'
    path = parts.path.rstrip('/')
    for suffix in ('/kinoko/status', '/kinoko/tts', '/tts/stream', '/openapi.json', '/health', '/config', '/tts'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit((parts.scheme.lower(), hostname, path.rstrip('/'), '', ''))


def _json(response: httpx.Response) -> dict:
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError('invalid status document')
    return data


def _has_routes(document: dict, routes: dict[str, str]) -> bool:
    paths = document.get('paths')
    return isinstance(paths, dict) and all(
        isinstance(paths.get(path), dict) and method in paths[path]
        for path, method in routes.items()
    )


async def _dots_status(client: httpx.AsyncClient, base_url: str) -> ServiceAvailability:
    response = await client.get(base_url + '/health')
    response.raise_for_status()
    health = _json(response)
    # The bundled dots server has no service-name field in /health. Its OpenAPI
    # title and both speech routes distinguish it from another service's health.
    response = await client.get(base_url + '/openapi.json')
    response.raise_for_status()
    schema = _json(response)
    info = schema.get('info')
    title = info.get('title') if isinstance(info, dict) else None
    if (not isinstance(title, str) or not title.casefold().startswith('dots.tts')
            or not _has_routes(schema, {'/tts': 'post', '/tts/stream': 'post'})):
        return ServiceAvailability(False, '该地址不是兼容的 dots.tts 服务')
    last_error = health.get('last_error')
    if (health.get('status') == 'error' and isinstance(last_error, dict)
            and last_error.get('code') == 'cuda_out_of_memory'):
        return ServiceAvailability(False, 'dots.tts 显存不足，请释放显存后重启 dots.tts API')
    if health.get('model_loaded') is not True or health.get('status') != 'ok':
        return ServiceAvailability(False, 'dots.tts 模型尚未加载完成')
    return ServiceAvailability(True, 'dots.tts 在线，模型已加载')


async def _gpt_status(client: httpx.AsyncClient, base_url: str) -> ServiceAvailability:
    response = await client.get(base_url + '/kinoko/status')
    if response.status_code not in (404, 405):
        response.raise_for_status()
        status = _json(response)
        if (type(status.get('protocol')) is int and status.get('protocol') == 1
                and status.get('atomic_model_selection') is True
                and status.get('resident_model_reuse') is True):
            return ServiceAvailability(True, 'GPT-SoVITS API 在线')
        return ServiceAvailability(False, '该地址未返回兼容的 GPT-SoVITS 服务标识')

    response = await client.get(base_url + '/openapi.json')
    if response.status_code not in (404, 405):
        response.raise_for_status()
        if _has_routes(_json(response), {
            '/tts': 'post', '/set_gpt_weights': 'get', '/set_sovits_weights': 'get',
        }):
            return ServiceAvailability(True, 'GPT-SoVITS API 在线')

    # Legacy connections are supported by GPTSovitsService's Gradio client.
    # A generic Gradio app is insufficient: all three operations are required.
    response = await client.get(base_url + '/config')
    response.raise_for_status()
    dependencies = _json(response).get('dependencies')
    names = {
        item['api_name'].lstrip('/') for item in dependencies
        if isinstance(item, dict) and isinstance(item.get('api_name'), str)
    } if isinstance(dependencies, list) else set()
    if {'get_tts_wav', 'change_gpt_weights', 'change_sovits_weights'} <= names:
        return ServiceAvailability(True, 'GPT-SoVITS 网页接口在线')
    return ServiceAvailability(False, '该地址不是兼容的 GPT-SoVITS 服务')


async def _dobao_status(client: httpx.AsyncClient, base_url: str) -> ServiceAvailability:
    response = await client.get(base_url + '/health')
    response.raise_for_status()
    status = _json(response)
    if status.get('service') != 'dobao-local-api' or status.get('status') != 'running':
        return ServiceAvailability(False, '该地址不是兼容的 Doubao API')
    if status.get('upstream_paused'):
        return ServiceAvailability(False, 'Doubao 上游请求已暂停，请在 API 首页检查状态并手动恢复')
    if status.get('credential_configured') is not True:
        return ServiceAvailability(False, 'Doubao API 在线但尚未登录，请在 API 首页配置登录信息')
    detail = 'Doubao API 在线，登录信息已配置'
    if status.get('upstream_verified') is not True:
        detail += '；首次合成时确认音色是否可用'
    return ServiceAvailability(True, detail)


async def _probe(service_type: str, base_url: str) -> ServiceAvailability:
    try:
        # One deadline covers every fallback request and connection setup.
        async with asyncio.timeout(_CHECK_TIMEOUT):
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=_CHECK_TIMEOUT,
            ) as client:
                if service_type == 'dots_tts':
                    return await _dots_status(client, base_url)
                if service_type == 'dobao_tts':
                    return await _dobao_status(client, base_url)
                return await _gpt_status(client, base_url)
    except (TimeoutError, httpx.TimeoutException):
        return ServiceAvailability(False, '本地 TTS 服务检查超时，请确认服务已启动')
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            return ServiceAvailability(False, '本地 TTS 服务拒绝访问，请检查 API 配置')
        if exc.response.status_code in (404, 405):
            return ServiceAvailability(False, '未找到所选 TTS 的兼容接口')
        return ServiceAvailability(False, '本地 TTS 服务暂时不可用，请检查服务状态')
    except httpx.RequestError:
        return ServiceAvailability(False, '无法连接本地 TTS 服务，请确认服务已启动')
    except httpx.InvalidURL:
        return ServiceAvailability(False, 'API 地址格式无效，请填写 HTTP(S) 服务地址')
    except (ValueError, TypeError):
        return ServiceAvailability(False, '本地 TTS 服务返回的状态格式无效')


def invalidate_service_availability(service_type, api_url: str = '') -> None:
    """Discard a failed speech target's cached readiness without retrying it."""
    try:
        key = (str(service_type), _base_url(api_url))
    except (AttributeError, TypeError, ValueError):
        return
    _cache.pop(key, None)
    # A UI probe already in flight must not restore an obsolete positive result
    # after the playback queue has observed a failed synthesis request.
    for pending in list(_pending.values()):
        if key in pending:
            pending[key].cacheable = False


async def check_service_availability(
    service_type, api_url: str = '', *, force: bool = False,
) -> ServiceAvailability:
    """Check the selected service without loading models or making speech.

    Successful local probes are cached for 10 seconds; failed probes for 2.
    ``force`` bypasses a completed cached result. Concurrent checks for the same
    service/address share one bounded probe, including concurrent UI refreshes.
    """
    service_type = str(service_type)
    if service_type in ('fish_audio', 'seed_tts'):
        return ServiceAvailability(True, '云端 API，直接使用已保存的配置')
    if service_type not in ('dots_tts', 'gpt_sovits', 'dobao_tts'):
        return ServiceAvailability(False, '该 TTS 服务不支持用户角色映射')
    try:
        base_url = _base_url(api_url)
    except (AttributeError, TypeError, ValueError):
        return ServiceAvailability(False, 'API 地址格式无效，请填写 HTTP(S) 服务地址')
    key = (service_type, base_url)
    cached = _cache.get(key)
    if not force and cached and time.monotonic() < cached[0]:
        return cached[1]
    pending = _pending.setdefault(asyncio.get_running_loop(), {})
    probe = pending.get(key)
    if probe is None:
        probe = _PendingProbe()

        async def run_probe():
            try:
                result = await _probe(service_type, base_url)
                if probe.cacheable:
                    if len(_cache) >= _MAX_CACHE_ENTRIES and key not in _cache:
                        del _cache[next(iter(_cache))]
                    ttl = _POSITIVE_TTL if result.available else _NEGATIVE_TTL
                    _cache[key] = (time.monotonic() + ttl, result)
                return result
            finally:
                if pending.get(key) is probe:
                    pending.pop(key, None)

        probe.task = asyncio.create_task(run_probe())
        pending[key] = probe
    # Closing the UI checker must not cancel a check also awaited by the queue.
    probe.waiters += 1
    try:
        return await asyncio.shield(probe.task)
    finally:
        probe.waiters -= 1
        if probe.waiters == 0 and not probe.task.done():
            probe.cacheable = False
            if pending.get(key) is probe:
                pending.pop(key, None)
            probe.task.cancel()
