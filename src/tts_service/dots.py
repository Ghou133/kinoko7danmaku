"""Adapter for the resident dots.tts service shipped in E:/AITTS/serve_api.py."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx

from core.qconfig import cfg
from core.audio_effects import transform_pcm, transform_wav
from core.dots_prompts import local_reference
from core.stream_audio import WavStreamDecoder

from .base import TTSService


_OOM_HEALTH_TIMEOUT = 0.75
_OOM_MESSAGE = 'dots.tts 显存不足（CUDA out of memory），请释放显存后重启 dots.tts API。'


def _is_oom_error(document) -> bool:
    if not isinstance(document, dict):
        return False
    detail = document.get('detail', document)
    return isinstance(detail, dict) and detail.get('code') == 'cuda_out_of_memory'


def _response_is_oom(response: httpx.Response) -> bool:
    try:
        return _is_oom_error(response.json())
    except (ValueError, TypeError):
        return False


async def _health_reports_oom(tts_url: str) -> bool:
    """Explain a broken stream without retrying or changing the server state."""
    try:
        # A single short deadline also covers connecting and reading the body.
        async with asyncio.timeout(_OOM_HEALTH_TIMEOUT):
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=_OOM_HEALTH_TIMEOUT,
            ) as client:
                response = await client.get(tts_url[:-4] + '/health')
                if response.status_code not in (200, 503):
                    return False
                health = response.json()
                return (isinstance(health, dict) and health.get('status') == 'error'
                        and _is_oom_error(health.get('last_error')))
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError):
        # The diagnostic request must never replace the original stream error.
        return False


class DotsTTSService(TTSService):
    def __init__(self) -> None:
        # A queued utterance owns its settings, including the resolved local
        # reference text. Later UI/preset edits only affect newly queued speech.
        self._settings = SimpleNamespace(**{
            name: deepcopy(getattr(cfg, name).value)
            for name in dir(cfg)
            if name.startswith('dots') and hasattr(getattr(cfg, name), 'value')
        })
        self._reference = local_reference(self._settings.dotsVoice.strip())
        super().__init__(self._settings.dotsApiUrl)

    @property
    def streaming_enabled(self) -> bool:
        return self._settings.dotsStreaming

    def _request(self, text: str) -> tuple[str, dict]:
        settings = self._settings
        url = settings.dotsApiUrl.strip().rstrip('/')
        if not url:
            raise ValueError('请设置 dots.tts API 地址')
        if url.endswith('/tts/stream'):
            url = url[:-7]
        if not url.endswith('/tts'):
            url += '/tts'
        payload = {'text': text, 'normalize_text': settings.dotsNormalizeText}
        for key, value in (
            ('voice', settings.dotsVoice),
            ('prompt_text', settings.dotsPromptText),
            ('language', settings.dotsLanguage),
        ):
            if value.strip():
                payload[key] = value.strip()
        if settings.dotsNumSteps:
            payload['num_steps'] = settings.dotsNumSteps
        reference = self._reference
        if reference is not None:
            payload['voice'] = reference[0]
            if reference[1] and not settings.dotsPromptText.strip():
                payload['prompt_text'] = reference[1]
        return url, payload

    async def stream_speech(self, text: str):
        chunks = transform_pcm(
            self._stream_raw(text),
            self._settings.dotsSpeed,
            self._settings.dotsVolume,
            self._settings.dotsFfmpegPath,
        )
        try:
            async for chunk in chunks:
                yield chunk
        finally:
            await chunks.aclose()

    async def _stream_raw(self, text: str):
        url, payload = self._request(text)
        decoder = WavStreamDecoder()
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=self._settings.dotsTimeout) as client:
                async with client.stream('POST', url + '/stream', json=payload) as response:
                    if response.is_error:
                        await response.aread()
                        response.raise_for_status()
                    # Do not set chunk_size: it would delay yielding short first packets.
                    async for data in response.aiter_bytes():
                        for chunk in decoder.feed(data):
                            yield chunk
                    decoder.finish()
        except httpx.HTTPStatusError as exc:
            if _response_is_oom(exc.response):
                raise ValueError(_OOM_MESSAGE) from None
            raise ValueError(f'dots.tts 流式接口返回 {exc.response.status_code}，请检查服务日志') from None
        except httpx.RemoteProtocolError as exc:
            if await _health_reports_oom(url):
                audio_state = '已收到部分音频，本条播放已中止。' if decoder.frames else '未收到可播放音频。'
                raise ValueError(_OOM_MESSAGE + audio_state) from None
            raise ValueError('dots.tts 流式连接被服务端中断，音频未完整接收，请检查服务日志') from exc
        except httpx.RequestError as exc:
            raise ValueError(f'dots.tts 流式请求失败: {url}/stream ({exc})') from exc

    async def text_to_speech(self, text: str, **kwargs: object) -> bytes:
        speed, volume, ffmpeg_path = self._settings.dotsSpeed, self._settings.dotsVolume, self._settings.dotsFfmpegPath
        url, payload = self._request(text)
        try:
            # Local requests must not be sent through environment proxies.
            async with httpx.AsyncClient(trust_env=False, timeout=self._settings.dotsTimeout) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if _response_is_oom(exc.response):
                raise ValueError(_OOM_MESSAGE) from None
            raise ValueError(f'dots.tts 返回 {exc.response.status_code}，请检查服务日志') from None
        except httpx.RequestError as exc:
            raise ValueError(f'dots.tts 请求失败，请确认服务已启动且地址正确: {url} ({exc})') from exc
        audio = response.content
        if len(audio) < 44 or audio[:4] != b'RIFF' or audio[8:12] != b'WAVE':
            raise ValueError('dots.tts 未返回有效 WAV 音频，请检查 API 地址及服务日志')
        return await transform_wav(audio, speed, volume, ffmpeg_path)
