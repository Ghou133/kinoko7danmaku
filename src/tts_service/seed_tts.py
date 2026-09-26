"""Volcengine Seed-TTS 2.0 over the V3 HTTP chunked API."""

import base64
import binascii
import io
import json
import uuid
import wave

from copy import deepcopy
from types import SimpleNamespace

import httpx

from core.qconfig import cfg
from core.stream_audio import PCMChunk

from .base import TTSService


SEED_TTS_URL = 'https://openspeech.bytedance.com/api/v3/tts/unidirectional'
SAMPLE_RATE = 24000
_MAX_INCOMPLETE_FRAME = 64 * 1024 * 1024


class SeedTTSService(TTSService):
    """Synthesize one utterance per request, optionally playing PCM as it arrives.

    Settings are captured when the service is constructed so a queued message
    keeps the voice and credentials it was assigned. No automatic retries are
    made: retrying after partial audio can speak twice and incur another charge.
    """

    def __init__(self, *, speaker: str | None = None) -> None:
        names = (
            'seedTtsApiKey', 'seedTtsVoice', 'seedTtsSpeed',
            'seedTtsStreaming', 'seedTtsTimeout',
        )
        self._settings = SimpleNamespace(**{
            name: deepcopy(getattr(cfg, name).value) for name in names
        })
        if speaker is not None:
            if not isinstance(speaker, str):
                raise ValueError('豆包语音音色 ID 格式无效')
            # An empty user binding means "use the global voice".
            self._settings.seedTtsVoice = speaker.strip() or self._settings.seedTtsVoice
        super().__init__(SEED_TTS_URL)

    @property
    def streaming_enabled(self) -> bool:
        return self._settings.seedTtsStreaming

    def _request(self, text: str) -> tuple[dict[str, str], dict[str, object]]:
        settings = self._settings
        key = settings.seedTtsApiKey.strip()
        if not key:
            raise ValueError('请在 TTS 设置中填写火山引擎 API Key')
        if any(not 33 <= ord(char) <= 126 for char in key):
            raise ValueError('火山引擎 API Key 格式无效，请重新粘贴')
        speaker = settings.seedTtsVoice.strip()
        if not speaker:
            raise ValueError('请在豆包语音设置中填写音色 ID')
        if not text.strip():
            raise ValueError('豆包语音朗读文本不能为空')
        headers = {
            'X-Api-Key': key,
            'X-Api-Resource-Id': 'seed-tts-2.0',
            'X-Api-Request-Id': str(uuid.uuid4()),
            'Content-Type': 'application/json',
        }
        payload = {
            'req_params': {
                'text': text,
                'speaker': speaker,
                'audio_params': {
                    'format': 'pcm',
                    'sample_rate': SAMPLE_RATE,
                    'speech_rate': settings.seedTtsSpeed,
                },
            },
        }
        return headers, payload

    @staticmethod
    def _status_error(status: int) -> ValueError:
        # Response bodies and server messages can reflect a credential; never
        # include them in UI errors or logs.
        reasons = {
            401: 'API Key 无效或已过期',
            402: '账户余额不足或服务未开通',
            403: '当前 API Key 无权使用此资源或音色',
            404: '接口或音色不存在',
            429: '请求过于频繁，请稍后重试',
            503: '服务暂时不可用，请稍后重试',
        }
        reason = reasons.get(status, '请求失败，请检查账户和音色设置')
        return ValueError(f'豆包语音返回 {status}：{reason}')

    @staticmethod
    def _network_error(error: httpx.RequestError, received: bool) -> ValueError:
        categories = (
            (httpx.ConnectTimeout, '连接超时'),
            (httpx.ReadTimeout, '等待音频超时'),
            (httpx.WriteTimeout, '发送请求超时'),
            (httpx.PoolTimeout, '等待连接超时'),
            (httpx.ConnectError, '无法连接服务'),
            (httpx.RemoteProtocolError, '服务端提前中断传输'),
            (httpx.ReadError, '接收音频时连接中断'),
            (httpx.WriteError, '发送请求时连接中断'),
        )
        reason = next((label for kind, label in categories if isinstance(error, kind)), '网络请求失败')
        stage = '已收到部分音频' if received else '尚未收到音频'
        return ValueError(f'豆包语音{reason}（{stage}），请检查网络或稍后重试')

    async def stream_speech(self, text: str):
        headers, payload = self._request(text)
        decoder = json.JSONDecoder()
        buffered = ''
        pending = bytearray()
        received = False
        finished = False
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.seedTtsTimeout, follow_redirects=False,
            ) as client:
                async with client.stream('POST', self.api_url, headers=headers, json=payload) as response:
                    if not response.is_success:
                        raise self._status_error(response.status_code)

                    # HTTP chunk boundaries can bisect a JSON object (or hold
                    # several objects). raw_decode accepts either NDJSON or
                    # adjacent JSON objects without depending on packet size.
                    async for fragment in response.aiter_text():
                        buffered += fragment
                        while buffered.strip():
                            buffered = buffered.lstrip()
                            try:
                                frame, end = decoder.raw_decode(buffered)
                            except json.JSONDecodeError:
                                break  # The next transport fragment may finish this object.
                            buffered = buffered[end:]
                            if not isinstance(frame, dict) or type(frame.get('code')) is not int:
                                raise ValueError('豆包语音返回了无效的响应')
                            code = frame['code']
                            if code == 20000000:
                                finished = True
                                break
                            if code != 0:
                                raise ValueError(f'豆包语音合成失败（错误码 {code}）')
                            encoded = frame.get('data')
                            if encoded is None or encoded == '':
                                continue  # Subtitle or usage frame, with no audio.
                            if not isinstance(encoded, str):
                                raise ValueError('豆包语音返回了无效的音频数据')
                            try:
                                pending.extend(base64.b64decode(encoded, validate=True))
                            except (ValueError, binascii.Error):
                                raise ValueError('豆包语音返回了无效的音频数据') from None
                            count = len(pending) - len(pending) % 2
                            if count:
                                received = True
                                pcm = bytes(pending[:count])
                                del pending[:count]
                                yield PCMChunk(pcm, SAMPLE_RATE, 1)
                        if finished:
                            break
                        if len(buffered) > _MAX_INCOMPLETE_FRAME:
                            raise ValueError('豆包语音响应过大或格式无效')

                    if not finished and buffered.strip():
                        raise ValueError('豆包语音音频流不完整，请重试本条文本')
                    if pending:
                        raise ValueError('豆包语音 PCM 音频不完整，请重试本条文本')
                    if not received:
                        raise ValueError('豆包语音未返回音频，请检查音色 ID 和文本')
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
