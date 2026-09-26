"""Start the optional Doubao bridge only after an explicit UI action."""

import asyncio
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .dobao_voices import detect_dobao_folder
from .tts_availability import invalidate_service_availability


def local_api_url(value: str) -> tuple[str, int]:
    """The bridge listens on IPv4 loopback; never launch for a remote URL."""
    parts = urlsplit(value.strip())
    if (parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', 'localhost')
            or parts.username is not None or parts.password is not None
            or parts.path not in ('', '/') or parts.query or parts.fragment):
        raise ValueError('请填写本机 API 地址，例如 http://127.0.0.1:9882')
    port = parts.port or 80
    if not 1024 <= port <= 65535:
        raise ValueError('API 端口需在 1024 到 65535 之间，默认使用 9882')
    return f'http://127.0.0.1:{port}', port


def installation_folder(value: str = '') -> Path:
    value = value.strip() or detect_dobao_folder()
    if not value:
        raise ValueError('请先选择 DoBao-TTS-Win 安装文件夹')
    root = Path(value).expanduser().resolve()
    if not (root / 'local-api' / 'server.mjs').is_file():
        raise ValueError('此文件夹缺少 local-api/server.mjs，请选择已适配的 DoBao-TTS-Win 文件夹')
    if not (root / 'runtime' / 'node.exe').is_file():
        raise ValueError('此文件夹缺少 runtime/node.exe，请检查 Doubao 安装文件夹')
    return root


def _port_is_free(port: int) -> bool:
    # Windows may report a refused localhost connection as ConnectTimeout.
    # An exclusive bind confirms absence without mistaking a slow HTTP server
    # for a stopped one. No listening socket or background thread is created.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class ApiState:
    ready: bool
    detail: str
    started: bool = False


def describe_health(data: dict, *, started: bool = False) -> ApiState:
    if (not isinstance(data, dict) or data.get('service') != 'dobao-local-api'
            or data.get('status') != 'running'):
        raise ValueError('该端口正在被其他服务使用，请检查 API 地址')
    prefix = 'API 已启动' if started else 'API 已运行'
    if data.get('upstream_paused'):
        return ApiState(False, prefix + '；上游请求已暂停，请在登录 / 状态页面检查并手动恢复', started)
    if data.get('credential_configured') is not True:
        return ApiState(False, prefix + '；尚未登录，请点击“登录 / 状态”保存登录信息', started)
    detail = prefix + '，登录信息已配置'
    if data.get('upstream_verified') is True:
        detail += '，最近合成成功'
    else:
        detail += '；可在音频测试中试听'
    return ApiState(True, detail, started)


class DoBaoApiManager:
    def __init__(self):
        self.process: subprocess.Popen | None = None

    @staticmethod
    async def _health(client: httpx.AsyncClient, url: str, *, started=False) -> ApiState:
        response = await client.get(url + '/health')
        if response.status_code in (401, 403):
            raise ValueError('API 拒绝访问，请检查该端口上的服务及访问配置')
        response.raise_for_status()
        return describe_health(response.json(), started=started)

    async def start_or_check(self, api_url: str, folder: str = '') -> ApiState:
        url, port = local_api_url(api_url)
        invalidate_service_availability('dobao_tts', api_url)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=1.0) as client:
            try:
                # Checking an existing service requires no installation folder.
                return await self._health(client, url)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                pass
            except httpx.TimeoutException as exc:
                raise ValueError('API 检查超时，请稍后再检查；未重复启动服务') from exc

            if self.process is not None and self.process.poll() is None:
                return ApiState(False, '之前启动的 API 仍在运行，请稍后检查；更改端口后需先停止原 API')
            if not _port_is_free(port):
                raise ValueError('该端口已被占用，但 API 未响应；请稍后检查或更换 API 地址')
            root = installation_folder(folder)
            logs = root / 'local-api' / 'logs'
            logs.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env['DOBAO_API_PORT'] = str(port)
            with (logs / 'kinoko-api.log').open('ab') as log:
                self.process = subprocess.Popen(
                    [str(root / 'runtime' / 'node.exe'), str(root / 'local-api' / 'server.mjs')],
                    cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )

            # Keep stop_api.bat usable, without requiring it for normal startup.
            # Only the standard port owns the standalone launcher's PID file.
            if port == 9882:
                private = root / 'local-api' / 'private'
                private.mkdir(parents=True, exist_ok=True)
                (private / 'server.pid').write_text(str(self.process.pid), encoding='ascii')

            deadline = asyncio.get_running_loop().time() + 6.0
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.2)
                if self.process.poll() is not None:
                    raise ValueError('API 未能启动，请查看安装目录 local-api/logs/kinoko-api.log')
                try:
                    state = await self._health(client, url, started=True)
                    invalidate_service_availability('dobao_tts', api_url)
                    return state
                except (httpx.ConnectError, httpx.TimeoutException):
                    continue
            return ApiState(False, '已发出启动请求，暂未就绪；请稍后检查或查看 local-api/logs/kinoko-api.log', True)
