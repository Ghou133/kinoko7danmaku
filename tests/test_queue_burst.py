"""Exercise the real speech queue with synthetic TTS and a silent output device."""

import asyncio
import io
import struct
import threading
import wave

import pytest

from core.stream_audio import PCMChunk


def _wav_bytes():
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(struct.pack('<h', 1000) * 240)
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming, failed_index', [(False, None), (True, None), (False, 11), (True, 11)])
async def test_concurrent_24_utterance_burst_is_fifo_and_atomic(config, monkeypatch, tmp_path, streaming, failed_index):
    import tts_service
    from core.player import audio_player
    from core.speech import speak_template

    clip = tmp_path / 'clip.wav'
    clip.write_bytes(_wav_bytes())
    config.aliasDict.value = {}
    config.messageAliasDict.value = {}
    config.audioClipDict.value = {'[clip]': str(clip)}
    config.dotsStreaming.value = streaming
    events = []
    services = []
    state = {'owner': None, 'open_devices': 0, 'max_open_devices': 0}

    class Device:
        def __init__(self):
            assert state['owner'] is not None, 'Output must belong to an active utterance'
            self.owner = state['owner']
            state['open_devices'] += 1
            state['max_open_devices'] = max(state['max_open_devices'], state['open_devices'])
            events.append((self.owner, 'output-open'))

        def write(self, data):
            assert state['owner'] == self.owner
            assert data
            events.append((self.owner, 'output-write'))

        def stop_stream(self):
            events.append((self.owner, 'output-stop'))

        def close(self):
            events.append((self.owner, 'output-close'))
            state['open_devices'] -= 1

    class Service:
        streaming_enabled = streaming

        def __init__(self):
            self.index = len(services)
            self.started = False
            services.append(self)

        def begin(self, text):
            if not self.started:
                assert state['owner'] is None, 'Two utterances entered synthesis simultaneously'
                self.started = True
                state['owner'] = self.index
            assert state['owner'] == self.index
            events.append((self.index, 'tts', text))

        async def text_to_speech(self, text):
            self.begin(text)
            await asyncio.sleep(0)
            if self.index == failed_index and text.startswith('后'):
                raise RuntimeError('synthetic synthesis failure')
            return _wav_bytes()

        async def stream_speech(self, text):
            self.begin(text)
            try:
                for packet in range(2):
                    await asyncio.sleep(0)
                    if self.index == failed_index and text.startswith('后') and packet == 1:
                        raise RuntimeError('synthetic synthesis failure')
                    yield PCMChunk(b'\x01\x00' * 16, 24000, 1)
            finally:
                events.append((self.index, 'generator-close'))

        async def close(self):
            assert state['owner'] == self.index
            assert state['open_devices'] == 0, 'Next utterance must wait for all output to close'
            events.append((self.index, 'service-close'))
            state['owner'] = None

    monkeypatch.setattr(tts_service, 'get_tts_service', Service)
    monkeypatch.setattr(audio_player.p, 'open', lambda **kwargs: Device())
    audio_player.start_worker()
    try:
        # There is no await between task creation: this is one burst, not a serial test.
        tasks = [
            asyncio.create_task(
                speak_template('{user_name}:{message}', user_name=f'u{i:02}', message=f'前{i:02}[clip]后{i:02}')
            )
            for i in range(24)
        ]
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        await asyncio.wait_for(audio_player.audio_queue.join(), 1)
    finally:
        await audio_player.stop_worker()

    for index, result in enumerate(results):
        if index == failed_index:
            assert isinstance(result, RuntimeError)
            assert str(result) == 'synthetic synthesis failure'
        else:
            assert result is None

    assert len(services) == 24
    assert state == {'owner': None, 'open_devices': 0, 'max_open_devices': 1}
    # Every observed synthesis, file clip, PCM write, and cleanup stays inside its
    # own utterance's contiguous interval, including after a mid-stream failure.
    assert [event[0] for event in events] == sorted(event[0] for event in events)
    assert [event[0] for event in events if event[1] == 'service-close'] == list(range(24))
    assert [(event[0], event[2]) for event in events if event[1] == 'tts'] == [
        (i, text) for i in range(24) for text in (f'u{i:02}:前{i:02}', f'后{i:02}')
    ]
    # Streaming opens prefix / literal clip / suffix separately. Buffered TTS
    # opens one combined utterance, and does not play partially generated errors.
    expected_opens = [
        i for i in range(24) for _ in range(3 if streaming else (0 if i == failed_index else 1))
    ]
    assert [event[0] for event in events if event[1] == 'output-open'] == expected_opens
    assert [event[0] for event in events if event[1] == 'output-close'] == expected_opens


@pytest.mark.asyncio
async def test_stop_during_stream_write_joins_output_and_releases_generator(config, monkeypatch):
    import tts_service
    from core.player import audio_player
    from core.speech import speak_template

    config.aliasDict.value = {}
    config.messageAliasDict.value = {}
    config.audioClipDict.value = {}
    events = []
    loop = asyncio.get_running_loop()
    writing = asyncio.Event()
    release_write = threading.Event()

    class Device:
        def write(self, data):
            assert data
            events.append('write-start')
            loop.call_soon_threadsafe(writing.set)
            assert release_write.wait(5), 'Test did not release the fake device write'
            events.append('write-end')

        def stop_stream(self):
            events.append('output-stop')

        def close(self):
            events.append('output-close')

    class Service:
        streaming_enabled = True

        async def stream_speech(self, text):
            assert text == 'first', 'Pending utterances must not start after stop'
            try:
                yield PCMChunk(b'\x01\x00', 24000, 1)
                await asyncio.Event().wait()
            finally:
                events.append('generator-close')

        async def close(self):
            events.append('service-close')

    monkeypatch.setattr(tts_service, 'get_tts_service', Service)
    monkeypatch.setattr(audio_player.p, 'open', lambda **kwargs: Device())
    audio_player.start_worker()
    active = asyncio.create_task(speak_template('{message}', message='first'))
    pending = []
    try:
        await asyncio.wait_for(writing.wait(), 2)
        pending = [asyncio.create_task(speak_template('{message}', message=f'pending{i}')) for i in range(24)]
        await asyncio.sleep(0)
        stopping = asyncio.create_task(audio_player.stop_worker())
        await asyncio.sleep(0.02)
        assert not stopping.done(), 'Stopping must wait for the device write before closing its resources'
        assert events == ['write-start']
        release_write.set()
        await asyncio.wait_for(stopping, 2)
        results = await asyncio.wait_for(asyncio.gather(active, *pending, return_exceptions=True), 2)
    finally:
        release_write.set()
        await audio_player.stop_worker()
        await asyncio.gather(active, *pending, return_exceptions=True)

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert events == ['write-start', 'write-end', 'generator-close', 'output-stop', 'output-close', 'service-close']
    assert audio_player.audio_queue is None

    # A stopped/cancelled stream must not poison subsequent player sessions.
    audio_player.start_worker()
    try:
        async def next_job():
            events.append('restarted')

        await asyncio.wait_for(audio_player.play_job_async(next_job), 1)
        assert events[-1] == 'restarted'
    finally:
        await audio_player.stop_worker()
