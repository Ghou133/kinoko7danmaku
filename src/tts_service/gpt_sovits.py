import asyncio
import pathlib
import time
import weakref
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import urlparse

from typing import Any

import aiohttp
import httpx

from loguru import logger

from core.qconfig import cfg
from core.const import GPT_SOVITS_TEXT_SPLIT_METHODS
from core.sovits_models import scan_models
from core.stream_audio import WavStreamDecoder

from .base import TTSService


class GradioClient:
    """
    Minimal Gradio client to talk to GPT-SoVITS WebUI following the behavior
    referenced in gpt-sovits-tts/tts.py and tts_client.py.
    """

    def __init__(self, base_url: str, ssl_verify: bool = False, timeout: int = 300) -> None:
        self.base_url = base_url if base_url.endswith('/') else (base_url + '/')
        self.ssl_verify = ssl_verify
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._fn_map: dict[str, int] = {}

    async def ensure(self) -> None:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=self.ssl_verify)
            self._session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
            await self._load_config()

    async def _load_config(self) -> None:
        assert self._session is not None
        url = self.base_url + 'config'
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            cfg = await resp.json()
        deps = cfg.get('dependencies') or []
        # Build api_name -> fn_index map
        for i, dep in enumerate(deps):
            api_name = (dep or {}).get('api_name')
            if api_name:
                self._fn_map[str(api_name).strip().lstrip('/')] = int((dep or {}).get('id', i))

    async def close(self) -> None:
        if self._session is not None:
            s = self._session
            self._session = None
            try:
                await s.close()
            except Exception:
                pass

    async def _upload_file(self, file_path: str) -> str:
        assert self._session is not None
        url = self.base_url + 'upload'
        data = aiohttp.FormData()
        file_content = await asyncio.to_thread(pathlib.Path(file_path).read_bytes)
        data.add_field(
            'files',
            file_content,
            filename=file_path.split('/')[-1],
            content_type='application/octet-stream',
        )
        async with self._session.post(url, data=data) as resp:
            resp.raise_for_status()
            j = await resp.json()
            # returns list of uploaded paths
            return j[0]

    async def _process_inputs(self, args: list[Any]) -> list[Any]:
        processed: list[Any] = []
        for a in args:
            if isinstance(a, dict) and a.get('meta', {}).get('_type') == 'gradio.FileData':
                p = a.get('path')
                if p and not (str(p).startswith('http://') or str(p).startswith('https://')):
                    # local path -> upload
                    uploaded = await self._upload_file(p)
                    processed.append({
                        'path': uploaded,
                        'orig_name': a.get('orig_name') or (str(p).split('/')[-1]),
                        'meta': {'_type': 'gradio.FileData'},
                    })
                else:
                    processed.append(a)
            else:
                processed.append(a)
        return processed

    async def predict(self, api_name: str, *args: Any) -> Any:
        await self.ensure()
        assert self._session is not None
        fn = self._fn_map.get(api_name.strip().lstrip('/'))
        if fn is None:
            raise RuntimeError(f"API '{api_name}' not found in gradio config")
        url = self.base_url + 'api/predict/'
        data = {
            'data': await self._process_inputs(list(args)),
            'fn_index': fn,
            'session_hash': str(int(time.time() * 1000)),
        }
        async with self._session.post(url, json=data, timeout=30) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f'Gradio predict failed: {resp.status} {text[:200]}')
            j = await resp.json()
            if j.get('error'):
                raise RuntimeError(f'Gradio API error: {j.get("error")}')
            return j.get('data')


class GPTSovitsService(TTSService):
    """GPTSovits TTS适配器"""

    _locks = weakref.WeakKeyDictionary()

    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        # Each queued utterance owns a complete configuration snapshot. A later
        # UI/model change must not change its weights, reference or API target.
        values = {
            name: deepcopy(getattr(cfg, name).value)
            for name in dir(cfg)
            if name.startswith('gptSovits') and hasattr(getattr(cfg, name), 'value')
        }
        if overrides:
            unknown = overrides.keys() - values.keys()
            if unknown:
                raise ValueError(f'未知 GPT-SoVITS 配置项：{", ".join(sorted(unknown))}')
            values.update(deepcopy(overrides))
        self._settings = SimpleNamespace(**values)
        self.client = GradioClient(self._settings.gptSovitsApiUrl)
        self._resident_mode = False

    @asynccontextmanager
    async def _rest_session(self):
        url = self._settings.gptSovitsApiUrl.strip().rstrip('/')
        if url.endswith('/tts'):
            url = url[:-4]
        gpt, sovits = self._settings.gptSovitsGptModel, self._settings.gptSovitsSovitsModel
        if self._settings.gptSovitsFolder:
            pairs, _ = await asyncio.to_thread(scan_models, self._settings.gptSovitsFolder)
            if not any(pathlib.Path(p.gpt) == pathlib.Path(gpt) and pathlib.Path(p.sovits) == pathlib.Path(sovits) for p in pairs):
                raise ValueError('请在 GPT-SoVITS 设置中选择一组成对角色模型')
        if not gpt or not sovits:
            raise ValueError('请先选择 GPT-SoVITS 角色模型')
        lock = self._locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
        try:
            async with lock, httpx.AsyncClient(trust_env=False, timeout=300) as client:
                started = time.perf_counter()
                # Negotiate on every request: a restarted/replaced server must
                # never inherit a client's guess about its loaded model.
                status = await client.get(url + '/kinoko/status')
                if status.status_code in (404, 405):
                    self._resident_mode = False
                else:
                    status.raise_for_status()
                    capabilities = status.json()
                    self._resident_mode = (capabilities.get('protocol') == 1
                                           and capabilities.get('atomic_model_selection') is True
                                           and capabilities.get('resident_model_reuse') is True)
                if not self._resident_mode:
                    for endpoint, path in (('set_sovits_weights', sovits), ('set_gpt_weights', gpt)):
                        response = await client.get(f'{url}/{endpoint}', params={'weights_path': path})
                        response.raise_for_status()
                logger.info('GPT-SoVITS 准备请求 {:.3f}s，模型常驻接口={}',
                            time.perf_counter() - started, self._resident_mode)
                yield client, url + '/kinoko' if self._resident_mode else url
        except httpx.HTTPStatusError as exc:
            raise ValueError(f'GPT-SoVITS API 返回 {exc.response.status_code}: {exc.response.text[:500]}') from exc
        except httpx.TimeoutException as exc:
            raise ValueError(f'GPT-SoVITS 请求超时：{url}；模型加载或合成未在等待时间内完成，请检查服务日志。{exc}') from exc
        except httpx.ConnectError as exc:
            raise ValueError(f'无法连接 GPT-SoVITS API：{url}；请点击“启动 / 检查 API”并等待模型加载完成。{exc}') from exc
        except httpx.RemoteProtocolError as exc:
            raise ValueError(f'GPT-SoVITS 响应中途断开，音频可能不完整；请检查参考音频与服务端合成日志：{exc}') from exc
        except httpx.RequestError as exc:
            raise ValueError(f'GPT-SoVITS 请求或音频传输失败：{url}；请检查服务日志。{exc}') from exc

    async def _rest_speech(self, payload: dict) -> bytes:
        async with self._rest_session() as (client, url):
            response = await client.post(url + '/tts', json=self._request_body(payload))
            response.raise_for_status()
        audio = response.content
        if len(audio) < 44 or audio[:4] != b'RIFF' or audio[8:12] != b'WAVE':
            raise ValueError('GPT-SoVITS 未返回有效的 WAV 音频')
        return audio

    def _request_body(self, payload: dict) -> dict:
        if not self._resident_mode:
            return payload
        return {'gpt_weights_path': self._settings.gptSovitsGptModel,
                'sovits_weights_path': self._settings.gptSovitsSovitsModel, 'tts': payload}

    @property
    def streaming_enabled(self):
        return self._settings.gptSovitsStreaming and self._is_rest()

    def _is_rest(self):
        url = self._settings.gptSovitsApiUrl.rstrip('/')
        return bool(self._settings.gptSovitsFolder) or urlparse(url).port == 9880 or url.endswith('/tts')

    async def stream_speech(self, text: str):
        languages = {'auto': 'auto', 'Chinese': 'all_zh', 'English': 'en',
                     'Japanese': 'all_ja', 'Korean': 'all_ko', 'Cantonese': 'all_yue',
                     'Multilingual Mixed': 'auto'}
        if not self._settings.gptSovitsRefAudioPath:
            raise ValueError('请选择参考音频，并填写该音频对应的参考文本')
        split = self._settings.gptSovitsTextSplitMethod
        payload = {
            'text': text, 'text_lang': languages.get(self._settings.gptSovitsTextLang, self._settings.gptSovitsTextLang),
            'ref_audio_path': self._settings.gptSovitsRefAudioPath,
            'prompt_text': '' if self._settings.gptSovitsRefTextFree else self._settings.gptSovitsRefText,
            'prompt_lang': languages.get(self._settings.gptSovitsRefTextLang, self._settings.gptSovitsRefTextLang),
            'top_k': self._settings.gptSovitsTopK, 'top_p': self._settings.gptSovitsTopP,
            'temperature': self._settings.gptSovitsTemperature,
            'text_split_method': 'cut5' if split == '不切' else f'cut{GPT_SOVITS_TEXT_SPLIT_METHODS.index(split)}',
            'speed_factor': self._settings.gptSovitsSpeedFactor, 'sample_steps': self._settings.gptSovitsSampleSteps,
            'super_sampling': self._settings.gptSovitsSuperSampling,
            'fragment_interval': self._settings.gptSovitsPauseSeconds,
            'media_type': 'wav', 'streaming_mode': True, 'batch_size': 1, 'split_bucket': False,
        }
        # api_v2 emits a WAV header with data length 0 followed by raw PCM.
        decoder = WavStreamDecoder(zero_size_is_stream=True)
        started = time.perf_counter()
        first_pcm = True
        async with self._rest_session() as (client, url):
            async with client.stream('POST', url + '/tts', json=self._request_body(payload)) as response:
                if response.is_error:
                    await response.aread()
                    response.raise_for_status()
                try:
                    async for data in response.aiter_bytes():
                        for chunk in decoder.feed(data):
                            if first_pcm:
                                logger.info('GPT-SoVITS 首段音频 {:.3f}s，采样率 {} Hz',
                                            time.perf_counter() - started, chunk.sample_rate)
                                first_pcm = False
                            yield chunk
                    decoder.finish()
                    logger.info('GPT-SoVITS 音频流消费完成 {:.3f}s（含下游播放等待）', time.perf_counter() - started)
                except ValueError as exc:
                    detail = str(exc).removeprefix('dots.tts ')
                    raise ValueError(f'GPT-SoVITS 音频流异常：{detail}') from exc

    async def close(self) -> None:
        await self.client.close()

    async def init(self) -> None:
        await self.client.ensure()
        result = await self.client.predict(
            '/change_sovits_weights',
            self._settings.gptSovitsSovitsModel,
            self._settings.gptSovitsTextLang,
            self._settings.gptSovitsTextLang,
        )
        logger.info(f'Changed SoVITS weights: {result}')
        result = await self.client.predict('/change_gpt_weights', self._settings.gptSovitsGptModel)
        logger.info(f'Changed GPT weights: {result}')

    async def text_to_speech(
        self,
        text: str,
        text_lang: str | None = None,
        ref_audio_path: str | None = None,
        ref_text: str | None = None,
        ref_text_lang: str | None = None,
        top_k: int | None = None,
        top_p: float | None = None,
        temperature: float | None = None,
        text_split_method: str | None = None,
        speed_factor: float | None = None,
        ref_text_free: bool | None = None,
        sample_steps: int | None = None,
        super_sampling: bool | None = None,
        pause_seconds: float | None = None,
    ) -> bytes:
        """
        使用GPTSovits API将文本转换为语音

        Args:
            text: 要转换的文本
            text_lang: 文本语言，为 None 时使用此条请求的配置快照
            ref_audio_path: 参考音频路径，为 None 时使用此条请求的配置快照
            ref_text: 参考文本，为 None 时使用此条请求的配置快照
            ref_text_lang: 参考文本语言，为 None 时使用此条请求的配置快照
            top_k: Top K采样参数，为 None 时使用此条请求的配置快照
            top_p: Top P采样参数，为 None 时使用此条请求的配置快照
            temperature: 采样温度，为 None 时使用此条请求的配置快照
            text_split_method: 文本切分方式，为 None 时使用此条请求的配置快照
            speed_factor: 语速调整，为 None 时使用此条请求的配置快照
            ref_text_free: 无参考文本模式，为 None 时使用此条请求的配置快照
            sample_steps: 采样步数，为 None 时使用此条请求的配置快照
            super_sampling: 超采样，为 None 时使用此条请求的配置快照
            pause_seconds: 句间停顿秒数，为 None 时使用此条请求的配置快照

        Returns:
            bytes: 音频数据（WAV格式）
        """
        # 使用创建服务时保存的配置，避免排队期间切换角色串音。
        if text_lang is None:
            text_lang = self._settings.gptSovitsTextLang
        if ref_audio_path is None:
            ref_audio_path = self._settings.gptSovitsRefAudioPath
        if ref_text is None:
            ref_text = self._settings.gptSovitsRefText
        if ref_text_lang is None:
            ref_text_lang = self._settings.gptSovitsRefTextLang
        if top_k is None:
            top_k = self._settings.gptSovitsTopK
        if top_p is None:
            top_p = self._settings.gptSovitsTopP
        if temperature is None:
            temperature = self._settings.gptSovitsTemperature
        if text_split_method is None:
            text_split_method = self._settings.gptSovitsTextSplitMethod
        if speed_factor is None:
            speed_factor = self._settings.gptSovitsSpeedFactor
        if ref_text_free is None:
            ref_text_free = self._settings.gptSovitsRefTextFree
        if sample_steps is None:
            sample_steps = self._settings.gptSovitsSampleSteps
        if super_sampling is None:
            super_sampling = self._settings.gptSovitsSuperSampling
        if pause_seconds is None:
            pause_seconds = self._settings.gptSovitsPauseSeconds

        # Preserve explicitly configured old WebUI connections. Folder-based
        # installations use the stable api_v2 REST interface.
        if self._is_rest():
            languages = {'auto': 'auto', 'Chinese': 'all_zh', 'English': 'en',
                         'Japanese': 'all_ja', 'Korean': 'all_ko', 'Cantonese': 'all_yue',
                         'Multilingual Mixed': 'auto'}
            if not ref_audio_path:
                raise ValueError('请选择参考音频，并填写该音频对应的参考文本')
            payload = {
                'text': text, 'text_lang': languages.get(text_lang, text_lang),
                'ref_audio_path': ref_audio_path, 'prompt_text': '' if ref_text_free else ref_text,
                'prompt_lang': languages.get(ref_text_lang, ref_text_lang),
                'top_k': top_k, 'top_p': top_p, 'temperature': temperature,
                'text_split_method': f'cut{GPT_SOVITS_TEXT_SPLIT_METHODS.index(text_split_method)}',
                'speed_factor': speed_factor, 'sample_steps': sample_steps,
                'super_sampling': super_sampling, 'fragment_interval': pause_seconds,
                'media_type': 'wav', 'streaming_mode': False,
            }
            return await self._rest_speech(payload)

        await self.init()
        ref_audio_dict = {
            'path': ref_audio_path,
            'orig_name': ref_audio_path.split('/')[-1],
            'meta': {'_type': 'gradio.FileData'},
        }
        is_freeze = False  # 是否冻结模型
        inp_refs = None  # 输入的参考音频
        data = await self.client.predict(
            '/get_tts_wav',
            ref_audio_dict,
            ref_text,
            ref_text_lang,
            text,
            text_lang,
            text_split_method,
            top_k,
            top_p,
            temperature,
            ref_text_free,
            speed_factor,
            is_freeze,
            inp_refs,
            sample_steps,
            super_sampling,
            pause_seconds,
        )

        # 读取返回的音频文件
        audio_path = data[0].get('url')
        async with httpx.AsyncClient() as client:
            response = await client.get(audio_path)
            response.raise_for_status()
            return response.content
