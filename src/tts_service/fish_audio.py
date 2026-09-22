"""Hosted Fish Audio with immediate PCM playback and per-utterance settings."""

import io
import wave

from copy import deepcopy
from types import SimpleNamespace

import httpx

from core.qconfig import cfg
from core.stream_audio import PCMChunk

from .base import TTSService


FISH_AUDIO_TTS_URL = 'https://api.fish.audio/v1/tts'
SAMPLE_RATE = 44100


class FishAudioService(TTSService):
    """One complete text request, with audio received as the service generates it.

    Raw PCM avoids waiting for a complete WAV/MP3 before playback. HTTP packet
    boundaries are unrelated to PCM frames, so incomplete samples are retained
    until the next packet. No automatic retries can duplicate partially spoken
    text or issue an extra paid synthesis request.
    """

    def __init__(self, *, reference_id: str | None = None) -> None:
        self._settings = SimpleNamespace(**{
            name: deepcopy(getattr(cfg, name).value)
            for name in dir(cfg)
            if name.startswith('fishAudio') and hasattr(getattr(cfg, name), 'value')
        })
        if reference_id is not None:
            from core.fish_voices import normalize_voice_id

            self._settings.fishAudioReferenceId = normalize_voice_id(reference_id)
        super().__init__(FISH_AUDIO_TTS_URL)

    @property
    def streaming_enabled(self) -> bool:
        return self._settings.fishAudioStreaming

    def _request(self, text: str) -> tuple[dict, dict]:
        settings = self._settings
        key = settings.fishAudioApiKey.strip()
        if not key:
            raise ValueError('请在 TTS 设置中填写 Fish Audio API 密钥')
        if any(not 33 <= ord(char) <= 126 for char in key):
            raise ValueError('Fish Audio API 密钥格式无效，请重新粘贴')
        voice_id = settings.fishAudioReferenceId.strip()
        if not voice_id:
            raise ValueError('请在 Fish Audio 设置中填写音色 ID（reference_id）')
        if not text.strip():
            raise ValueError('Fish Audio 朗读文本不能为空')
        headers = {
            'Authorization': f'Bearer {key}',
            'model': settings.fishAudioModel,
            'Content-Type': 'application/json',
        }
        payload = {
            'text': text,
            'reference_id': voice_id,
            'format': 'pcm',
            'sample_rate': SAMPLE_RATE,
            'temperature': settings.fishAudioTemperature,
            'top_p': settings.fishAudioTopP,
            'prosody': {
                'speed': settings.fishAudioSpeed,
                'volume': settings.fishAudioVolume,
                'normalize_loudness': True,
            },
            'latency': settings.fishAudioLatency,
            'normalize': True,
        }
        return headers, payload

    @staticmethod
    def _status_error(status: int) -> ValueError:
        # Do not expose response bodies: proxies or a remote error can reflect
        # Authorization headers. Fixed messages also keep keys out of logs.
        reasons = {
            401: 'API 密钥无效或已过期',
            402: '余额不足或当前模型未获授权',
            403: '当前密钥没有使用此音色或模型的权限',
            404: '音色不存在，请检查音色 ID',
            422: '请求参数无效，请检查音色 ID 和合成设置',
            429: '请求过于频繁，请稍后重试',
            503: '服务暂时不可用，请稍后重试',
        }
        reason = reasons.get(status, '服务未能完成请求，请检查账户和合成设置')
        return ValueError(f'Fish Audio 返回 {status}：{reason}')

    @staticmethod
    def _network_error(error: httpx.RequestError, received: bool) -> ValueError:
        # Only use our fixed categories, never the exception's message, request,
        # or response. Some proxies reflect credentials in transport errors.
        categories = (
            (httpx.ConnectTimeout, '连接服务超时', 'ConnectTimeout'),
            (httpx.ReadTimeout, '等待音频超时', 'ReadTimeout'),
            (httpx.WriteTimeout, '发送请求超时', 'WriteTimeout'),
            (httpx.PoolTimeout, '等待连接超时', 'PoolTimeout'),
            (httpx.ConnectError, '无法连接服务', 'ConnectError'),
            (httpx.RemoteProtocolError, '服务端提前中断音频传输', 'RemoteProtocolError'),
            (httpx.ReadError, '接收音频时连接中断', 'ReadError'),
            (httpx.WriteError, '发送请求时连接中断', 'WriteError'),
        )
        reason, code = '网络请求失败', 'RequestError'
        for error_type, description, category in categories:
            if isinstance(error, error_type):
                reason, code = description, category
                break
        stage = '已收到部分音频' if received else '尚未收到音频'
        return ValueError(f'Fish Audio {reason}（{code}，{stage}），请检查网络或稍后重试')

    async def stream_speech(self, text: str):
        headers, payload = self._request(text)
        pending = bytearray()
        received = False
        try:
            # Keep redirects disabled so a server response cannot send the key
            # to another endpoint. HTTPS certificate verification stays enabled.
            async with httpx.AsyncClient(timeout=self._settings.fishAudioTimeout, follow_redirects=False) as client:
                async with client.stream('POST', self.api_url, headers=headers, json=payload) as response:
                    if not response.is_success:
                        raise self._status_error(response.status_code)
                    media_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                    if media_type not in ('', 'audio/pcm', 'audio/x-pcm', 'application/pcm', 'application/octet-stream'):
                        raise ValueError('Fish Audio 未返回 PCM 音频，请稍后重试')
                    # No fixed chunk_size: yield the first available complete
                    # sample rather than holding audio for a network-sized block.
                    async for data in response.aiter_bytes():
                        pending.extend(data)
                        count = len(pending) - len(pending) % 2
                        if count:
                            received = True
                            pcm = bytes(pending[:count])
                            del pending[:count]
                            yield PCMChunk(pcm, SAMPLE_RATE, 1)
                    if pending:
                        raise ValueError('Fish Audio 音频流不完整，请重试本条文本')
                    if not received:
                        raise ValueError('Fish Audio 未返回音频，请检查音色 ID 和文本')
        except httpx.RequestError as error:
            raise self._network_error(error, received) from None

    async def text_to_speech(self, text: str, **kwargs: object) -> bytes:
        output = io.BytesIO()
        stream = self.stream_speech(text)
        try:
            with wave.open(output, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                async for chunk in stream:
                    wav.writeframesraw(chunk.data)
        finally:
            await stream.aclose()
        return output.getvalue()
