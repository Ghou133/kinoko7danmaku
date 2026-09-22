"""Mixed-engine routing through the actual adapters and single playback queue."""

import asyncio
import json

import httpx
import pytest

from tests.test_sovits_snapshot import snapshot_config, wav_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize('dots_streaming,gpt_streaming,fail_index', [
    (True, True, None), (False, False, None),
    (False, True, 2), (True, False, 2),
])
async def test_mixed_queue_keeps_route_snapshot_order_and_recovers(
    snapshot_config, monkeypatch, dots_streaming, gpt_streaming, fail_index,
):
    from core.player import audio_player
    from core.speech import speak_template
    from core import tts_availability
    import tts_service.dots as dots_module

    cfg = snapshot_config
    cfg.activeTTS.value = 'dots_tts'
    # Legacy opt-in no longer gates username routing between visible services.
    cfg.gptSovitsRouteFromDots.value = False
    cfg.gptSovitsUserModelsEnabled.value = True
    cfg.gptSovitsUserModels.value = {'mapped': {'gpt': 'A.ckpt', 'sovits': 'A.pth'}}
    cfg.gptSovitsStreaming.value = gpt_streaming
    cfg.dotsStreaming.value = dots_streaming
    cfg.dotsApiUrl.value = 'http://127.0.0.1:9881'
    cfg.dotsVoice.value = 'fixed.wav'
    cfg.dotsSpeed.value = cfg.dotsVolume.value = 1.0
    cfg.aliasDict.value = {'mapped': 'pronunciation-alias'}
    cfg.messageAliasDict.value = {}
    cfg.audioClipDict.value = {}
    monkeypatch.setattr(dots_module, 'local_reference', lambda _: None)
    monkeypatch.setattr(tts_availability, '_cache', {})
    started, release = asyncio.Event(), asyncio.Event()
    calls, outputs = [], []
    state = {'open': 0, 'max_open': 0, 'http_open': 0, 'max_http': 0, 'index': None}

    class AudioStream(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False
            state['http_open'] += 1
            state['max_http'] = max(state['max_http'], state['http_open'])

        async def __aiter__(self):
            data = wav_bytes()
            yield data[:48]
            await asyncio.sleep(0)
            yield data[48:]

        async def aclose(self):
            if not self.closed:
                self.closed = True
                state['http_open'] -= 1

    async def handle(request):
        assert request.url.host == '127.0.0.1', 'Queued API target changed'
        if request.url.path == '/kinoko/status':
            assert request.url.port == 9880
            return httpx.Response(200, json={
                'protocol': 1, 'atomic_model_selection': True, 'resident_model_reuse': True,
            })
        body = json.loads(request.content)
        is_gpt = request.url.port == 9880
        payload = body['tts'] if is_gpt else body
        index = int(payload['text'])
        assert state['open'] == state['http_open'] == 0, 'Previous playback/response still open'
        state['index'] = index
        calls.append((index, request.url.port, request.url.path))
        if is_gpt:
            assert body['gpt_weights_path'] == 'A.ckpt'
            assert body['sovits_weights_path'] == 'A.pth'
            assert payload['ref_audio_path'] == 'A.wav'
            assert payload['streaming_mode'] is gpt_streaming
            assert request.url.path == '/kinoko/tts', 'Resident path must not reload via setters'
        else:
            assert payload['voice'] == 'fixed.wav'
            assert request.url.path == ('/tts/stream' if dots_streaming else '/tts')
        if index == 0:
            started.set()
            await release.wait()
        if index == fail_index:
            return httpx.Response(503, text='synthetic GPT failure')
        return httpx.Response(200, stream=AudioStream())

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original_client(
        transport=httpx.MockTransport(handle), **kw,
    ))

    class Device:
        def __init__(self):
            self.index = state['index']
            state['open'] += 1
            state['max_open'] = max(state['max_open'], state['open'])
            outputs.append(self.index)

        def write(self, data):
            assert data and state['index'] == self.index

        def stop_stream(self):
            pass

        def close(self):
            state['open'] -= 1

    monkeypatch.setattr(audio_player.p, 'open', lambda **kw: Device())
    audio_player.start_worker()
    tasks = [asyncio.create_task(speak_template(
        '{message}', user_name='mapped' if i % 2 == 0 else 'ordinary', message=str(i),
    )) for i in range(6)]
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert audio_player.audio_queue.qsize() == 5
        # All entries already exist. Later edits must not reroute them, change
        # their API/role, or turn their captured streaming mode on/off.
        assert cfg.activeTTS.value == 'dots_tts'
        cfg.activeTTS.value = 'edge'
        cfg.gptSovitsRouteFromDots.value = False
        cfg.gptSovitsUserModels.value = {}
        cfg.gptSovitsApiUrl.value = 'http://changed:9880'
        cfg.gptSovitsGptModel.value = 'changed.ckpt'
        cfg.gptSovitsStreaming.value = not gpt_streaming
        cfg.dotsApiUrl.value = 'http://changed:9881'
        cfg.dotsVoice.value = 'changed.wav'
        cfg.dotsStreaming.value = not dots_streaming
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
    finally:
        release.set()
        await audio_player.stop_worker()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert [(i, port) for i, port, _ in calls] == [(i, 9880 if i % 2 == 0 else 9881) for i in range(6)]
    assert outputs == [i for i in range(6) if i != fail_index]
    assert state['max_open'] == state['max_http'] == 1
    assert state['open'] == state['http_open'] == 0
    for index, result in enumerate(results):
        if index == fail_index:
            assert isinstance(result, ValueError) and '503' in str(result)
        else:
            assert result is None
