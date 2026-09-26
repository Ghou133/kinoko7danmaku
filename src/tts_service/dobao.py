"""Complete WAV synthesis through the local DoBao API; credentials stay there."""

import asyncio
import io
import math
import wave
import weakref

from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import httpx

from core.dobao_voices import normalize_dobao_voice
from core.qconfig import cfg

from .base import TTSService


# Small margin over the API's 3-second receipt interval avoids network jitter
# making two requests arrive just under its limit.
MIN_REQUEST_INTERVAL = 3.2


@dataclass
class _RequestGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_start: float = 0.0


# Adapters are snapshots created for individual queued messages. Share one gate
# on their running loop so a second adapter cannot bypass spacing or serialization.
_gates = weakref.WeakKeyDictionary()


def _endpoint(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
        if (parts.scheme.lower() not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment):
            raise ValueError()
        port = parts.port
        host = parts.hostname.lower()
        if ':' in host:
            host = f'[{host}]'
        if port is not None:
            host += f':{port}'
        path = parts.path.rstrip('/')
        if not path.endswith('/tts'):
            path += '/tts'
        return urlunsplit((parts.scheme.lower(), host, path, '', ''))
    except (ValueError, AttributeError):
        raise ValueError('DoBao API 地址无效，请填写不含账号、密码和查询参数的 HTTP(S) 地址') from None


def _retry_after(response: httpx.Response) -> int | None:
    try:
        seconds = float(response.headers.get('Retry-After', ''))
        if math.isfinite(seconds) and 0 <= seconds <= 3600:
            return max(1, math.ceil(seconds))
    except ValueError:
        pass
    return None


def _status_error(response: httpx.Response, retry_after: int | None) -> ValueError:
    # Upstream bodies may reflect credentials. Only use known codes and our own
    # messages, never error.message, URLs, or raw response text in UI errors.
    try:
        document = response.json()
        error = document.get('error') if isinstance(document, dict) else None
        code = error.get('code') if isinstance(error, dict) else None
    except ValueError:
        code = None
    reasons = {
        'LOGIN_REQUIRED': '请打开 DoBao API 首页登录或配置自己的 Cookie',
        'UPSTREAM_ERROR': '豆包拒绝了合成，请检查登录状态或稍后手动重试',
        'UPSTREAM_HTTP': '豆包连接被拒绝，请检查登录状态',
        'UPSTREAM_CONNECT': '无法连接豆包，请检查本地 API 的网络状态',
        'UPSTREAM_CONNECTION': '豆包连接中断，请稍后手动重试',
        'UPSTREAM_SEND': '发送语音请求失败，请稍后手动重试',
        'UPSTREAM_PROTOCOL': '豆包返回格式异常，请检查本地 API',
        'UPSTREAM_AUTH': '豆包登录已失效，请在本地 API 首页重新登录后手动恢复',
        'UPSTREAM_BLOCKED': '豆包限制了语音请求，API 已暂停；请等待限制解除后在 API 首页手动恢复',
        'UPSTREAM_RATE_LIMITED': '豆包请求频率受限，API 已暂停；请等待限制解除后在 API 首页手动恢复',
        'UPSTREAM_PAUSED': 'API 已暂停上游请求，请在 API 首页检查登录状态并等待限制解除后手动恢复',
        'INVALID_VOICE': '所选豆包音色不可用，请检查音色 ID 或更新本地 API 的音色目录',
        'INVALID_TEXT': '朗读文本不符合 API 要求，请缩短文本后重试',
        'TEXT_TOO_LONG': '文本过长，每条最多 2000 字符',
        'IDLE_TIMEOUT': '豆包语音响应超时，本条未自动重试',
        'TOTAL_TIMEOUT': '豆包语音合成超时，本条未自动重试',
    }
    if isinstance(code, str) and code in reasons:
        reason = reasons[code]
    elif response.status_code == 429:
        suffix = f'；建议等待 {retry_after} 秒' if retry_after else ''
        reason = f'服务忙或请求过快，本条未自动重试{suffix}'
    elif response.status_code in (401, 403):
        reason = '本地 API 拒绝访问，请检查服务访问设置'
    else:
        reason = '请求失败，请检查本地 API 状态后手动重试'
    return ValueError(f'DoBao 返回 {response.status_code}：{reason}')


def _validate_wav(data: bytes) -> bytes:
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 24000, 'NONE'):
                raise ValueError()
            frames = audio.getnframes()
            if frames <= 0 or len(audio.readframes(frames)) != frames * 2:
                raise ValueError()
    except (wave.Error, EOFError, ValueError):
        raise ValueError('DoBao 未返回完整的 24 kHz 单声道 WAV 音频') from None
    return data


class DoBaoTTSService(TTSService):
    """Snapshot settings for one message and synthesize without automatic retry."""

    def __init__(self, *, voice: str | None = None) -> None:
        self._settings = SimpleNamespace(**{
            name: deepcopy(getattr(cfg, name).value)
            for name in ('dobaoApiUrl', 'dobaoVoice', 'dobaoSpeed', 'dobaoTimeout')
        })
        if voice is not None and voice != '':
            self._settings.dobaoVoice = voice
        super().__init__(self._settings.dobaoApiUrl)

    async def text_to_speech(self, text: str, **kwargs: object) -> bytes:
        endpoint = _endpoint(self.api_url)
        if not isinstance(text, str) or not text.strip():
            raise ValueError('DoBao 朗读文本不能为空')
        voice = normalize_dobao_voice(self._settings.dobaoVoice)
        speed = self._settings.dobaoSpeed
        if type(speed) not in (int, float) or not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
            raise ValueError('DoBao 语速必须在 0.5–2.0 倍之间')
        payload = {'text': text, 'voice': voice, 'speed': speed, 'format': 'wav'}
        loop = asyncio.get_running_loop()
        gate = _gates.setdefault(loop, _RequestGate())
        async with gate.lock:
            delay = gate.next_start - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient(
                    timeout=self._settings.dobaoTimeout, trust_env=False, follow_redirects=False,
                ) as client:
                    # Start spacing after client setup, immediately before sending.
                    # A cancelled waiter reserves nothing; a request already sent
                    # still consumes its interval because the server may have it.
                    gate.next_start = loop.time() + MIN_REQUEST_INTERVAL
                    response = await client.post(endpoint, json=payload)
                    if response.status_code != 200:
                        retry_after = _retry_after(response)
                        if response.status_code == 429 and retry_after is not None:
                            gate.next_start = max(gate.next_start, loop.time() + retry_after)
                        raise _status_error(response, retry_after)
                    return _validate_wav(response.content)
            except httpx.TimeoutException:
                raise ValueError('DoBao 语音请求超时，本条未自动重试') from None
            except httpx.InvalidURL:
                raise ValueError('DoBao API 地址无效，请检查 TTS 设置') from None
            except httpx.RequestError:
                raise ValueError('无法连接或接收 DoBao 语音，请确认本地 API 已启动；本条未自动重试') from None
