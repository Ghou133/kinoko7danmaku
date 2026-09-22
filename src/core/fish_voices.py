"""Saved Fish Audio voice IDs and optional, read-only name lookup."""

import re

from urllib.parse import urlsplit

import httpx


_VOICE_ID = re.compile(r'[0-9a-fA-F]{32}')
_VOICE_PATH = re.compile(
    r'/(?:[a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?/)?(?:app/)?m/([0-9a-fA-F]{32})/?'
)
_INVALID_ID = '请输入 32 位音色 ID，或 fish.audio 的音色页面链接'


def normalize_voice_id(value: str) -> str:
    """Accept a voice ID or official page URL, without resolving any URL."""
    if not isinstance(value, str):
        raise ValueError(_INVALID_ID)
    value = value.strip()
    if _VOICE_ID.fullmatch(value):
        return value.lower()
    try:
        url = urlsplit(value)
        valid_origin = (
            url.scheme == 'https'
            and url.hostname == 'fish.audio'
            and url.port is None
            and url.username is None
            and url.password is None
        )
    except ValueError:
        valid_origin = False
    if valid_origin:
        match = _VOICE_PATH.fullmatch(url.path)
        if match:
            return match.group(1).lower()
    raise ValueError(_INVALID_ID)


async def fetch_voice_name(value: str, api_key: str) -> str:
    """Fetch metadata only; never synthesize audio or follow supplied URLs.

    Errors contain fixed local messages. Raising outside the exception handler
    also keeps request headers and reflected server bodies out of the exception
    context, not only out of the displayed traceback.
    """
    voice_id = normalize_voice_id(value)
    key = api_key.strip() if isinstance(api_key, str) else ''
    if not key:
        raise ValueError('请先在 TTS 设置中填写 Fish Audio API 密钥，也可以手动填写音色名称')
    if any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError('Fish Audio API 密钥格式无效，请重新粘贴')

    error = None
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(
                f'https://api.fish.audio/model/{voice_id}',
                headers={'Authorization': f'Bearer {key}'},
            )
        if not response.is_success:
            reasons = {
                401: 'API 密钥无效或已过期',
                403: '当前密钥无权查看此音色',
                404: '音色不存在，请检查音色 ID',
                429: '请求过于频繁，请稍后重试',
            }
            reason = reasons.get(response.status_code, '暂时无法获取音色名称，请稍后重试或手动填写')
            error = f'Fish Audio 返回 {response.status_code}：{reason}'
        else:
            metadata = response.json()
            title = metadata.get('title') if isinstance(metadata, dict) else None
            if isinstance(title, str) and title.strip():
                return title.strip()
            error = 'Fish Audio 未提供音色名称，请手动填写'
    except httpx.TimeoutException:
        error = '获取音色名称超时，请稍后重试或手动填写'
    except httpx.RequestError:
        error = '无法连接 Fish Audio，请检查网络或手动填写音色名称'
    except Exception:
        # Includes malformed JSON and local transport errors. Never include the
        # original exception, which may contain a credential or response body.
        error = '无法读取 Fish Audio 音色信息，请稍后重试或手动填写名称'
    raise ValueError(error)
