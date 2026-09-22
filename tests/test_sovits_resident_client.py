import json

import httpx
import pytest

from tests.test_sovits_snapshot import snapshot_config, wav_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_atomic_request_and_server_replacement(snapshot_config, monkeypatch, streaming):
    from tts_service.gpt_sovits import GPTSovitsService
    resident = True
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == '/kinoko/status':
            return (httpx.Response(200, json={'protocol': 1, 'atomic_model_selection': True,
                                             'resident_model_reuse': True})
                    if resident else httpx.Response(404))
        if request.url.path == '/kinoko/tts':
            payload = json.loads(request.content)
            assert payload['gpt_weights_path'] == 'A.ckpt'
            assert payload['sovits_weights_path'] == 'A.pth'
            assert payload['tts']['ref_audio_path'] == 'A.wav'
            assert payload['tts']['streaming_mode'] is streaming
            return httpx.Response(200, content=wav_bytes())
        if request.url.path == '/tts':
            return httpx.Response(200, content=wav_bytes())
        return httpx.Response(200, json={'message': 'success'})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    service = GPTSovitsService()

    async def speak():
        if streaming:
            return [chunk async for chunk in service.stream_speech('测试')]
        return await service.text_to_speech('测试')

    assert await speak()
    assert [r.url.path for r in calls] == ['/kinoko/status', '/kinoko/tts']
    resident = False
    calls.clear()
    assert await speak()
    assert [r.url.path for r in calls] == ['/kinoko/status', '/set_sovits_weights', '/set_gpt_weights', '/tts']


@pytest.mark.asyncio
async def test_status_error_does_not_switch_models_or_retry(snapshot_config, monkeypatch):
    from tts_service.gpt_sovits import GPTSovitsService
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(503, text='server busy')

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    with pytest.raises(ValueError, match='503'):
        await GPTSovitsService().text_to_speech('测试')
    assert calls == ['/kinoko/status']
