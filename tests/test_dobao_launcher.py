"""On-demand startup, identity checking and login-state reporting."""

import subprocess
import socket
from types import SimpleNamespace

import httpx
import pytest

from core import dobao_launcher as launcher


@pytest.fixture(autouse=True)
def isolated_port_probe(monkeypatch):
    monkeypatch.setattr(launcher, '_port_is_free', lambda port: True)


def health(**extra):
    return {'service': 'dobao-local-api', 'status': 'running',
            'credential_configured': True, 'upstream_verified': True, **extra}


def client(monkeypatch, handler):
    original = httpx.AsyncClient
    options = []

    def create(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', create)
    return options


@pytest.mark.parametrize('value', [
    'https://127.0.0.1:9882', 'http://example.com:9882', 'http://127.0.0.1:9882/tts',
    'http://user:pass@127.0.0.1:9882', 'http://127.0.0.1:9882?x=1',
    'http://127.0.0.1:9882#x', 'http://127.0.0.1:80', 'http://127.0.0.1:65536',
    'http://[::1]:9882', 'file:///tmp/file',
])
def test_remote_or_ambiguous_url_never_launches(value):
    with pytest.raises(ValueError):
        launcher.local_api_url(value)


def test_localhost_matches_ipv4_listener():
    assert launcher.local_api_url(' http://localhost:9982/ ') == ('http://127.0.0.1:9982', 9982)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,ready,detail', [
    (health(), True, '最近合成成功'),
    (health(credential_configured=False), False, '尚未登录'),
    (health(upstream_paused={'code': 'UPSTREAM_BLOCKED'}), False, '暂停'),
    (health(upstream_verified=False), True, '试听'),
])
async def test_running_api_only_checked_not_launched_or_resumed(monkeypatch, status, ready, detail):
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=status)

    options = client(monkeypatch, respond)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('existing API must not be launched'))
    result = await launcher.DoBaoApiManager().start_or_check('http://127.0.0.1:9882', 'missing installation')
    assert result.ready is ready and not result.started
    assert detail in result.detail
    assert requests == [('GET', '/health')]
    assert options == [{'trust_env': False, 'follow_redirects': False, 'timeout': 1.0}]


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [
    httpx.Response(200, json={'status': 'ok'}), httpx.Response(200, json=[]),
    httpx.Response(401), httpx.Response(404), httpx.Response(302, headers={'Location': 'https://example.org'}),
])
async def test_occupied_or_unidentified_service_never_launches(monkeypatch, response):
    client(monkeypatch, lambda request: response)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('occupied port'))
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        await launcher.DoBaoApiManager().start_or_check('http://127.0.0.1:9882')


@pytest.mark.asyncio
async def test_timeout_does_not_spawn_duplicate(monkeypatch):
    def respond(request):
        raise httpx.ReadTimeout('slow', request=request)

    client(monkeypatch, respond)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('slow existing API'))
    with pytest.raises(ValueError, match='未重复启动'):
        await launcher.DoBaoApiManager().start_or_check('http://127.0.0.1:9882')


@pytest.mark.asyncio
@pytest.mark.parametrize('port', [9882, 19982])
@pytest.mark.parametrize('unavailable_error', [httpx.ConnectError, httpx.ConnectTimeout])
async def test_launch_hidden_with_selected_port_then_check(monkeypatch, tmp_path, port, unavailable_error):
    (tmp_path / 'runtime').mkdir()
    (tmp_path / 'runtime/node.exe').touch()
    (tmp_path / 'local-api').mkdir()
    (tmp_path / 'local-api/server.mjs').touch()
    pidfile = tmp_path / 'local-api/private/server.pid'
    pidfile.parent.mkdir()
    pidfile.write_text('42')
    calls = []
    process = SimpleNamespace(pid=2345, poll=lambda: None)

    def respond(request):
        assert request.method == 'GET' and request.url.path == '/health'
        if not calls:
            raise unavailable_error('refused', request=request)
        return httpx.Response(200, json=health())

    def spawn(args, **kwargs):
        calls.append((args, kwargs))
        return process

    client(monkeypatch, respond)
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    manager = launcher.DoBaoApiManager()
    assert manager.process is None and not calls
    state = await manager.start_or_check(f'http://127.0.0.1:{port}', str(tmp_path))
    assert state.ready and state.started and len(calls) == 1
    args, opts = calls[0]
    assert args == [str(tmp_path / 'runtime/node.exe'), str(tmp_path / 'local-api/server.mjs')]
    assert opts['env']['DOBAO_API_PORT'] == str(port)
    assert opts['cwd'] == tmp_path and opts['creationflags'] == subprocess.CREATE_NO_WINDOW
    assert opts['stderr'] == subprocess.STDOUT
    assert pidfile.read_text() == ('2345' if port == 9882 else '42')
    again = await manager.start_or_check(f'http://127.0.0.1:{port}', str(tmp_path))
    assert again.ready and not again.started and len(calls) == 1


@pytest.mark.asyncio
async def test_connection_timeout_with_occupied_port_never_launches(monkeypatch):
    def respond(request):
        raise httpx.ConnectTimeout('slow connection', request=request)

    client(monkeypatch, respond)
    monkeypatch.setattr(launcher, '_port_is_free', lambda port: False)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('occupied port'))
    with pytest.raises(ValueError, match='已被占用'):
        await launcher.DoBaoApiManager().start_or_check('http://127.0.0.1:9882')


@pytest.mark.asyncio
async def test_unavailable_api_and_missing_installation_reports_action(monkeypatch, tmp_path):
    def respond(request):
        raise httpx.ConnectError('refused', request=request)

    client(monkeypatch, respond)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('incomplete installation'))
    with pytest.raises(ValueError, match='server.mjs'):
        await launcher.DoBaoApiManager().start_or_check('http://127.0.0.1:9882', str(tmp_path))


def test_constructing_api_card_never_connects_or_launches(app, monkeypatch):
    from gui.components.dobao_cards import DoBaoApiCard

    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **k: pytest.fail('initialization must not connect'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('initialization must not launch'))
    card = DoBaoApiCard()
    assert card.button.text() == '启动 / 检查 API'
    assert card.loginButton.text() == '登录 / 状态'
    assert card.manager.process is None
    card.close()
