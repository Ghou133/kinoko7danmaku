"""Fish streaming through the GUI event loop and paced, silent audio output."""

import asyncio
import time
import traceback

import httpx
import pytest

from qasync import QEventLoop

from tests.test_fish_audio import fish_config, mock_client
from tts_service.fish_audio import FishAudioService


def test_qasync_streaming_starts_before_response_finishes_and_keeps_all_pcm(
    app, fish_config, monkeypatch,
):
    from core.player import StreamPlayer
    from core.speech import speak_template
    import core.player as player_module
    import core.user_voices as voices_module

    packets = [b'\x01\x00' * 2205, b'\x02\x00' * 2205, b'\x03\x00' * 2205]
    state = {'network_done': False, 'closed': False, 'output_closed': False, 'first_early': False}
    played = bytearray()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for packet in packets:
                yield packet
                await asyncio.sleep(0.01)
            state['network_done'] = True

        async def aclose(self):
            state['closed'] = True

    class Device:
        def write(self, data):
            if not played:
                state['first_early'] = not state['network_done']
            time.sleep(len(data) / (44100 * 2))
            played.extend(data)

        def stop_stream(self):
            pass

        def close(self):
            state['output_closed'] = True

    mock_client(monkeypatch, lambda _: httpx.Response(200, stream=Stream()))
    player = StreamPlayer()
    fish_config.fishAudioStreaming.value = True
    fish_config.aliasDict.value = {}
    fish_config.messageAliasDict.value = {}
    fish_config.audioClipDict.value = {}
    monkeypatch.setattr(player_module, 'audio_player', player)
    monkeypatch.setattr(voices_module, 'service_for_user', lambda _: FishAudioService())
    monkeypatch.setattr(player.p, 'open', lambda **kwargs: Device())

    async def speak():
        player.start_worker()
        try:
            await speak_template('{message}', user_name='', message='测试')
        finally:
            await player.stop_worker()

    try:
        with QEventLoop(app) as loop:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(speak())
    finally:
        asyncio.set_event_loop(None)
        player.close()
    assert bytes(played) == b''.join(packets)
    assert all(state.values())


@pytest.mark.asyncio
async def test_network_failure_after_first_audio_releases_output_and_does_not_retry(fish_config, monkeypatch):
    from core.player import StreamPlayer

    state = {'requests': 0, 'response_closed': False, 'output_closed': False}
    played = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'\x01\x00' * 30
            raise httpx.ReadError('server reflected test-only-secret')

        async def aclose(self):
            state['response_closed'] = True

    def handle(_):
        state['requests'] += 1
        return httpx.Response(200, stream=Stream())

    class Device:
        def write(self, data):
            played.append(data)

        def stop_stream(self):
            pass

        def close(self):
            state['output_closed'] = True

    mock_client(monkeypatch, handle)
    player = StreamPlayer()
    monkeypatch.setattr(player.p, 'open', lambda **kwargs: Device())
    try:
        with pytest.raises(ValueError, match='Fish Audio') as error:
            await player.play_pcm_stream(FishAudioService().stream_speech('测试'))
    finally:
        player.close()
    assert 'test-only-secret' not in str(error.value)
    assert 'ReadError' in str(error.value)
    assert '已收到部分音频' in str(error.value)
    assert played == [b'\x01\x00' * 30]
    assert state == {'requests': 1, 'response_closed': True, 'output_closed': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [
    httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
    httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError,
])
async def test_network_error_category_and_stage_are_safe(fish_config, monkeypatch, error_type):
    def handle(request):
        raise error_type('proxy reflected test-only-secret', request=request)

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError) as error:
        await FishAudioService().text_to_speech('测试')
    assert error_type.__name__ in str(error.value)
    assert '尚未收到音频' in str(error.value)
    assert 'test-only-secret' not in ''.join(traceback.format_exception(error.value))
