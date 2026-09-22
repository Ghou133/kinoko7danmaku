import io
import json
import wave
from copy import deepcopy

import httpx
import pytest


@pytest.fixture
def snapshot_config(config):
    from core.sovits_references import reference_memory

    names = [name for name in dir(config) if name.startswith('gptSovits')]
    saved = {name: deepcopy(getattr(config, name).value) for name in names}
    previous_switching = reference_memory.switching
    reference_memory.switching = True
    values = {
        'gptSovitsFolder': '',
        'gptSovitsApiUrl': 'http://127.0.0.1:9880',
        'gptSovitsGptModel': 'A.ckpt',
        'gptSovitsSovitsModel': 'A.pth',
        'gptSovitsRefAudioPath': 'A.wav',
        'gptSovitsRefText': '角色 A 台词',
        'gptSovitsTextLang': 'Chinese',
        'gptSovitsRefTextLang': 'Japanese',
        'gptSovitsRefTextFree': False,
        'gptSovitsTopK': 5,
        'gptSovitsTopP': 0.8,
        'gptSovitsTemperature': 0.9,
        'gptSovitsTextSplitMethod': '不切',
        'gptSovitsSpeedFactor': 1.2,
        'gptSovitsSampleSteps': 32,
        'gptSovitsSuperSampling': True,
        'gptSovitsPauseSeconds': 0.2,
        'gptSovitsReferences': {'A': {'text': '角色 A 台词'}},
    }
    for name, value in values.items():
        getattr(config, name).value = value
    yield config
    for name, value in saved.items():
        getattr(config, name).value = value
    reference_memory.switching = previous_switching


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b'\x01\x00' * 12)
    return output.getvalue()


def mock_client(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, 'AsyncClient',
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_queued_service_keeps_weights_payload_api_and_streaming(snapshot_config, monkeypatch, streaming):
    from tts_service.gpt_sovits import GPTSovitsService

    cfg = snapshot_config
    cfg.gptSovitsStreaming.value = streaming
    service = GPTSovitsService()
    # Simulate a later UI/service/model change before this entry is played.
    changes = {
        'gptSovitsFolder': 'not-installed-after-queue',
        'gptSovitsApiUrl': 'http://other-server:9872',
        'gptSovitsStreaming': not streaming,
        'gptSovitsGptModel': 'B.ckpt',
        'gptSovitsSovitsModel': 'B.pth',
        'gptSovitsRefAudioPath': 'B.wav',
        'gptSovitsRefText': '角色 B 台词',
        'gptSovitsTextLang': 'English',
        'gptSovitsRefTextLang': 'Chinese',
        'gptSovitsRefTextFree': True,
        'gptSovitsTopK': 15,
        'gptSovitsTopP': 1.0,
        'gptSovitsTemperature': 1.0,
        'gptSovitsTextSplitMethod': '按中文句号。切',
        'gptSovitsSpeedFactor': 1.0,
        'gptSovitsSampleSteps': 8,
        'gptSovitsSuperSampling': False,
        'gptSovitsPauseSeconds': 0.3,
    }
    for name, value in changes.items():
        getattr(cfg, name).value = value
    requests = []
    audio = wav_bytes()

    def handle(request):
        if request.url.path == '/kinoko/status':
            return httpx.Response(404)
        requests.append(request)
        return httpx.Response(200, content=audio if request.url.path == '/tts' else b'{}')

    mock_client(monkeypatch, handle)
    assert service.streaming_enabled is streaming
    if streaming:
        chunks = [chunk async for chunk in service.stream_speech('你好。')]
        assert sum(len(chunk.data) for chunk in chunks) == 24
    else:
        assert await service.text_to_speech('你好。') == audio
    assert [request.url.path for request in requests] == ['/set_sovits_weights', '/set_gpt_weights', '/tts']
    assert all(request.url.host == '127.0.0.1' and request.url.port == 9880 for request in requests)
    assert requests[0].url.params['weights_path'] == 'A.pth'
    assert requests[1].url.params['weights_path'] == 'A.ckpt'
    payload = json.loads(requests[-1].content)
    expected = {
        'text': '你好。', 'text_lang': 'all_zh',
        'ref_audio_path': 'A.wav', 'prompt_text': '角色 A 台词', 'prompt_lang': 'all_ja',
        'top_k': 5, 'top_p': 0.8, 'temperature': 0.9,
        'text_split_method': 'cut5' if streaming else 'cut0',
        'speed_factor': 1.2, 'sample_steps': 32, 'super_sampling': True,
        'fragment_interval': 0.2, 'media_type': 'wav', 'streaming_mode': streaming,
    }
    if streaming:
        expected.update(batch_size=1, split_bucket=False)
    assert payload == expected
    await service.close()


@pytest.mark.asyncio
async def test_overrides_are_request_local_and_deepcopied(snapshot_config, monkeypatch):
    from tts_service.gpt_sovits import GPTSovitsService

    cfg = snapshot_config
    saved = {name: deepcopy(getattr(cfg, name).value) for name in dir(cfg) if name.startswith('gptSovits')}
    overrides = {
        'gptSovitsGptModel': 'B.ckpt', 'gptSovitsSovitsModel': 'B.pth',
        'gptSovitsRefAudioPath': 'B.wav', 'gptSovitsRefText': '角色 B 台词',
        'gptSovitsRefTextLang': 'English',
        'gptSovitsReferences': {'B': {'text': '角色 B 台词'}},
    }
    service = GPTSovitsService(overrides=overrides)
    assert {name: getattr(cfg, name).value for name in saved} == saved
    overrides['gptSovitsGptModel'] = 'C.ckpt'
    overrides['gptSovitsReferences']['B']['text'] = 'changed'
    cfg.gptSovitsReferences.value['A']['text'] = 'global change'
    assert service._settings.gptSovitsReferences == {'B': {'text': '角色 B 台词'}}
    default_service = GPTSovitsService()
    cfg.gptSovitsReferences.value['A']['text'] = 'changed again'
    assert default_service._settings.gptSovitsReferences['A']['text'] == 'global change'
    requests = []

    def handle(request):
        if request.url.path == '/kinoko/status':
            return httpx.Response(404)
        requests.append(request)
        return httpx.Response(200, content=wav_bytes() if request.url.path == '/tts' else b'{}')

    mock_client(monkeypatch, handle)
    await service.text_to_speech('你好')
    assert requests[0].url.params['weights_path'] == 'B.pth'
    assert requests[1].url.params['weights_path'] == 'B.ckpt'
    payload = json.loads(requests[-1].content)
    assert payload['ref_audio_path'] == 'B.wav'
    assert payload['prompt_text'] == '角色 B 台词'
    assert payload['prompt_lang'] == 'en'
    await service.close()
    await default_service.close()


def test_unknown_override_fails_explicitly(snapshot_config):
    from tts_service.gpt_sovits import GPTSovitsService

    with pytest.raises(ValueError, match='gptSovitsModelTypo'):
        GPTSovitsService(overrides={'gptSovitsModelTypo': 'B.pth'})


@pytest.mark.asyncio
@pytest.mark.parametrize(('error_type', 'message'), [
    (httpx.ConnectError, '无法连接'),
    (httpx.ReadTimeout, '请求超时'),
    (httpx.ReadError, '音频传输失败'),
])
async def test_transport_errors_are_classified_without_retry(snapshot_config, monkeypatch, error_type, message):
    from tts_service.gpt_sovits import GPTSovitsService

    requests = []

    def handle(request):
        if request.url.path == '/kinoko/status':
            return httpx.Response(404)
        requests.append(request)
        raise error_type('test transport failure', request=request)

    mock_client(monkeypatch, handle)
    service = GPTSovitsService()
    with pytest.raises(ValueError, match=message) as exc:
        await service.text_to_speech('你好')
    if error_type is not httpx.ConnectError:
        assert '无法连接' not in str(exc.value)
    assert len(requests) == 1
    await service.close()


@pytest.mark.asyncio
async def test_partial_stream_reports_interruption_without_retry(snapshot_config, monkeypatch):
    from tts_service.gpt_sovits import GPTSovitsService

    requests, closed, chunks = [], [], []

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield wav_bytes()
            raise httpx.RemoteProtocolError('incomplete chunked read')

        async def aclose(self):
            closed.append(True)

    def handle(request):
        if request.url.path == '/kinoko/status':
            return httpx.Response(404)
        requests.append(request)
        if request.url.path == '/tts':
            return httpx.Response(200, stream=BrokenStream())
        return httpx.Response(200, content=b'{}')

    mock_client(monkeypatch, handle)
    service = GPTSovitsService()
    with pytest.raises(ValueError, match='响应中途断开') as exc:
        async for chunk in service.stream_speech('你好'):
            chunks.append(chunk)
    assert '无法连接' not in str(exc.value)
    assert 'incomplete chunked read' in str(exc.value)
    assert chunks and closed
    assert len(requests) == 3
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [b'not a wave response', wav_bytes()[:-2]])
async def test_invalid_or_truncated_wav_identifies_correct_service(snapshot_config, monkeypatch, body):
    from tts_service.gpt_sovits import GPTSovitsService

    def handle(request):
        if request.url.path == '/kinoko/status':
            return httpx.Response(404)
        return httpx.Response(200, content=body if request.url.path == '/tts' else b'{}')

    mock_client(monkeypatch, handle)
    service = GPTSovitsService()
    with pytest.raises(ValueError, match='GPT-SoVITS 音频流异常') as exc:
        async for _ in service.stream_speech('你好'):
            pass
    assert 'dots.tts' not in str(exc.value)
    assert '无法连接' not in str(exc.value)
    await service.close()
