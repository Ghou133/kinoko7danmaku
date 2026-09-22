import asyncio

from copy import deepcopy
from types import SimpleNamespace

import pytest
import pytest_asyncio

from core.stream_audio import PCMChunk


VOICE_A = 'a' * 32
VOICE_B = 'b' * 32
SERVICES = ('dots_tts', 'gpt_sovits', 'fish_audio')


@pytest.fixture
def routes(config):
    names = [name for name in dir(config) if name.startswith(('gptSovits', 'fishAudio'))]
    saved = {name: deepcopy(getattr(config, name).value) for name in names}
    config.gptSovitsUserModelsEnabled.value = True
    config.gptSovitsUserModels.value = {}
    config.gptSovitsReferences.value = {}
    config.gptSovitsRouteFromDots.value = False
    config.fishAudioReferenceId.value = VOICE_B
    config.fishAudioApiKey.value = 'test-only-key'
    config.aliasDict.value = {}
    config.messageAliasDict.value = {}
    config.audioClipDict.value = {}
    yield config
    from core.sovits_references import reference_memory

    reference_memory.switching = True
    try:
        for name, value in saved.items():
            getattr(config, name).value = value
    finally:
        reference_memory.switching = False


@pytest.mark.parametrize('default', SERVICES)
@pytest.mark.parametrize('target', SERVICES)
def test_every_visible_default_can_route_to_every_visible_service(routes, default, target):
    from core.user_voices import service_for_user, user_model_overrides, user_voice_binding

    routes.activeTTS.value = default
    original = {'service': target, 'gpt': 'A.ckpt', 'sovits': 'A.pth', 'reference_id': VOICE_A}
    routes.gptSovitsUserModels.value = {'Alice': original}
    service = service_for_user('Alice')
    expected = dict(zip(SERVICES, ('DotsTTSService', 'GPTSovitsService', 'FishAudioService')))
    assert type(service).__name__ == expected[target]
    assert routes.activeTTS.value == default
    assert routes.gptSovitsUserModels.value['Alice'] == original
    assert (user_model_overrides('Alice') is not None) == (target == 'gpt_sovits')
    assert user_voice_binding('alice') is None
    if target == 'fish_audio':
        assert service._request('你好')[1]['reference_id'] == VOICE_A
        assert routes.fishAudioReferenceId.value == VOICE_B


def test_binding_is_a_copy_and_legacy_entry_is_preserved(routes):
    from core.user_voices import user_voice_binding

    original = {'gpt': 'A.ckpt', 'sovits': 'A.pth', 'label': '角色 A'}
    routes.gptSovitsUserModels.value = {'Alice': original}
    binding = user_voice_binding('Alice')
    assert binding['service'] == 'gpt_sovits'
    binding['label'] = 'changed'
    assert routes.gptSovitsUserModels.value['Alice'] == original


@pytest.mark.parametrize('master,user', [(False, False), (False, True), (True, False)])
@pytest.mark.parametrize('target', SERVICES)
def test_disabled_bindings_use_default_without_deleting_entry(routes, master, user, target):
    from core.user_voices import service_for_user, user_voice_binding

    routes.activeTTS.value = 'fish_audio'
    routes.gptSovitsUserModelsEnabled.value = master
    binding = {'service': target, 'enabled': user, 'reference_id': VOICE_A}
    routes.gptSovitsUserModels.value = {'Alice': binding}
    assert user_voice_binding('Alice') is None
    assert service_for_user('Alice')._request('你好')[1]['reference_id'] == VOICE_B
    assert routes.gptSovitsUserModels.value['Alice'] == binding


@pytest.mark.parametrize('binding', [None, '', {}, {'service': 'unknown'}, {'service': 'edge'}])
def test_unknown_or_malformed_binding_does_not_select_hidden_service(routes, binding):
    from core.user_voices import user_voice_binding

    routes.gptSovitsUserModels.value = {'Alice': binding}
    assert user_voice_binding('Alice') is None


def test_fish_override_is_validated_and_every_setting_is_snapshotted(routes):
    from tts_service import FishAudioService

    routes.fishAudioSpeed.value = 1.2
    service = FishAudioService(reference_id=VOICE_A.upper())
    routes.fishAudioSpeed.value = 1.7
    routes.fishAudioApiKey.value = 'changed-key'
    routes.fishAudioReferenceId.value = VOICE_B
    headers, payload = service._request('你好')
    assert headers['Authorization'] == 'Bearer test-only-key'
    assert payload['reference_id'] == VOICE_A
    assert payload['prosody']['speed'] == 1.2
    with pytest.raises(ValueError, match='32 位音色 ID'):
        FishAudioService(reference_id='https://unrelated.invalid/voice')


class Adapter:
    def __init__(self, name, events, *, streaming=False, fail=False, url='http://127.0.0.1:9880'):
        self.name = name
        self.events = events
        self.streaming_enabled = streaming
        self.fail = fail
        self.api_url = url
        self._settings = SimpleNamespace(gptSovitsApiUrl=url, dotsApiUrl=url)

    async def text_to_speech(self, text):
        self.events.append((self.name, 'whole', text))
        if self.fail:
            raise ValueError('synthesis failed')
        return b'fake audio'

    async def stream_speech(self, text):
        self.events.append((self.name, 'stream', text))
        yield PCMChunk(b'\0\0', 24000, 1)
        if self.fail:
            raise ValueError('stream interrupted')

    async def close(self):
        self.events.append((self.name, 'close'))


@pytest_asyncio.fixture
async def playback(routes, monkeypatch):
    from core.player import audio_player

    async def consume(chunks):
        try:
            async for _ in chunks:
                pass
        finally:
            await chunks.aclose()

    monkeypatch.setattr(audio_player, 'play_pcm_stream', consume)
    monkeypatch.setattr(audio_player, 'play_bytes', lambda _: None)
    audio_player.start_worker()
    try:
        yield audio_player
    finally:
        await audio_player.stop_worker()


def install_route(monkeypatch, routes, target_type, default_type, target, fallback, check):
    import tts_service
    from core import tts_availability, user_voices

    routes.activeTTS.value = default_type
    routes.gptSovitsUserModels.value = {'Alice': {'service': target_type}}
    monkeypatch.setattr(user_voices, 'service_for_user', lambda _: target)
    monkeypatch.setattr(tts_service, 'get_tts_service', lambda: fallback)
    monkeypatch.setattr(tts_availability, 'check_service_availability', check)


@pytest.mark.asyncio
@pytest.mark.parametrize('target_type,default_type,available,expected,checks', [
    ('gpt_sovits', 'fish_audio', {'gpt_sovits': False}, 'fallback', ['gpt_sovits']),
    ('dots_tts', 'fish_audio', {'dots_tts': False}, 'fallback', ['dots_tts']),
    ('gpt_sovits', 'dots_tts', {'gpt_sovits': False, 'dots_tts': True}, 'fallback', ['gpt_sovits', 'dots_tts']),
    ('dots_tts', 'gpt_sovits', {'dots_tts': False, 'gpt_sovits': True}, 'fallback', ['dots_tts', 'gpt_sovits']),
    ('gpt_sovits', 'fish_audio', {'gpt_sovits': True}, 'target', ['gpt_sovits']),
    ('fish_audio', 'dots_tts', {}, 'target', []),
])
async def test_local_availability_fallback_and_actual_adapter_streaming(
    routes, monkeypatch, playback, target_type, default_type, available, expected, checks
):
    from core.speech import speak_template
    from core.tts_availability import ServiceAvailability

    events, calls = [], []
    target = Adapter('target', events, streaming=False)
    fallback = Adapter('fallback', events, streaming=True)

    async def check(service, url):
        calls.append(service)
        return ServiceAvailability(available[service], 'test status')

    install_route(monkeypatch, routes, target_type, default_type, target, fallback, check)
    notice = await speak_template('{message}', user_name='Alice', message='你好')
    assert calls == checks
    assert events[0] == (expected, 'stream' if expected == 'fallback' else 'whole', '你好')
    assert events[1:] == [('target', 'close'), ('fallback', 'close')]
    assert (notice is not None) == (expected == 'fallback')
    assert routes.activeTTS.value == default_type


@pytest.mark.asyncio
@pytest.mark.parametrize('default_type', ['gpt_sovits', 'dots_tts'])
async def test_unavailable_default_fails_promptly_and_queue_continues(routes, monkeypatch, playback, default_type):
    from core.speech import speak_template
    from core.tts_availability import ServiceAvailability

    events, calls = [], []
    target, fallback = Adapter('target', events), Adapter('fallback', events)

    async def check(service, url):
        calls.append(service)
        return ServiceAvailability(False, '未启动')

    install_route(monkeypatch, routes, 'gpt_sovits', default_type, target, fallback, check)
    with pytest.raises(ValueError, match='默认.*本条无法播放'):
        await asyncio.wait_for(speak_template('{message}', user_name='Alice', message='第一条'), 1)
    assert calls == (['gpt_sovits'] if default_type == 'gpt_sovits' else ['gpt_sovits', 'dots_tts'])
    assert events == [('target', 'close'), ('fallback', 'close')]
    routes.gptSovitsUserModels.value = {}
    await speak_template('{message}', user_name='Alice', message='第二条')
    assert ('target', 'whole', '第二条') in events


@pytest.mark.asyncio
async def test_partial_stream_failure_is_never_retried_on_default(routes, monkeypatch, playback):
    from core import tts_availability
    from core.speech import speak_template
    from core.tts_availability import ServiceAvailability

    events, invalidated = [], []
    target = Adapter('target', events, streaming=True, fail=True)
    fallback = Adapter('fallback', events)

    async def check(service, url):
        return ServiceAvailability(True, 'ready')

    install_route(monkeypatch, routes, 'gpt_sovits', 'fish_audio', target, fallback, check)
    monkeypatch.setattr(
        tts_availability, 'invalidate_service_availability',
        lambda service, url: invalidated.append((service, url)),
    )
    with pytest.raises(ValueError, match='stream interrupted'):
        await speak_template('{message}', user_name='Alice', message='你好')
    assert events == [('target', 'stream', '你好'), ('target', 'close'), ('fallback', 'close')]
    assert invalidated == [('gpt_sovits', target.api_url)]


@pytest.mark.asyncio
async def test_cloud_failure_does_not_probe_or_retry_local_default(routes, monkeypatch, playback):
    from core.speech import speak_template

    events = []
    target = Adapter('target', events, fail=True)
    fallback = Adapter('fallback', events)

    async def check(*args):
        pytest.fail('Cloud routes never probe local services')

    install_route(monkeypatch, routes, 'fish_audio', 'dots_tts', target, fallback, check)
    with pytest.raises(ValueError, match='synthesis failed'):
        await speak_template('{message}', user_name='Alice', message='你好')
    assert events == [('target', 'whole', '你好'), ('target', 'close'), ('fallback', 'close')]


@pytest.mark.asyncio
async def test_fallback_failure_keeps_route_reason(routes, monkeypatch, playback):
    from core.speech import speak_template
    from core.tts_availability import ServiceAvailability

    events = []
    target, fallback = Adapter('target', events), Adapter('fallback', events, fail=True)

    async def check(service, url):
        return ServiceAvailability(False, '未启动')

    install_route(monkeypatch, routes, 'gpt_sovits', 'fish_audio', target, fallback, check)
    with pytest.raises(ValueError, match='GPT-SoVITS.*未启动.*默认 Fish Audio.*synthesis failed'):
        await speak_template('{message}', user_name='Alice', message='你好')


@pytest.mark.asyncio
async def test_probe_waits_inside_fifo_and_both_candidates_are_snapshots(routes, monkeypatch, playback):
    import tts_service
    from core import tts_availability, user_voices
    from core.speech import speak_template
    from core.tts_availability import ServiceAvailability

    events, calls = [], []
    release = asyncio.Event()
    blocking = asyncio.Event()

    async def preceding():
        blocking.set()
        await release.wait()

    target = Adapter('target-old', events, url='http://127.0.0.1:19880')
    fallback = Adapter('fallback-old', events, streaming=True)
    captured = {'target': target, 'fallback': fallback}
    routes.activeTTS.value = 'fish_audio'
    routes.gptSovitsUserModels.value = {'Alice': {'service': 'gpt_sovits'}}
    monkeypatch.setattr(user_voices, 'service_for_user', lambda _: captured['target'])
    monkeypatch.setattr(tts_service, 'get_tts_service', lambda: captured['fallback'])

    async def check(service, url):
        calls.append((service, url))
        await asyncio.sleep(0)
        return ServiceAvailability(False, '未启动')

    monkeypatch.setattr(tts_availability, 'check_service_availability', check)
    first = asyncio.create_task(playback.play_job_async(preceding))
    await blocking.wait()
    a = asyncio.create_task(speak_template('{message}', user_name='Alice', message='第一条'))
    await asyncio.sleep(0)
    assert not calls  # No preflight await can reorder this utterance.
    captured['target'] = Adapter('target-new', events)
    captured['fallback'] = Adapter('fallback-new', events)
    routes.activeTTS.value = 'dots_tts'
    routes.gptSovitsUserModels.value = {'Alice': {'service': 'fish_audio'}}
    b = asyncio.create_task(speak_template('{message}', user_name='Alice', message='第二条'))
    await asyncio.sleep(0)
    release.set()
    try:
        _, notice, second_notice = await asyncio.wait_for(asyncio.gather(first, a, b), 2)
    finally:
        release.set()
    assert '默认 Fish Audio' in notice
    assert second_notice is None
    assert calls == [('gpt_sovits', 'http://127.0.0.1:19880')]
    assert [event for event in events if len(event) == 3] == [
        ('fallback-old', 'stream', '第一条'), ('target-new', 'whole', '第二条')
    ]


@pytest.mark.asyncio
async def test_fallback_mixed_clips_remain_in_one_slot(routes, monkeypatch, playback, tmp_path):
    from core.speech import speak_template
    from core.tts_availability import ServiceAvailability
    from tests.test_speech import wav_bytes

    events, calls = [], []
    clip = tmp_path / 'clip.wav'
    clip.write_bytes(wav_bytes())
    routes.audioClipDict.value = {'咕': str(clip)}
    target, fallback = Adapter('target', events), Adapter('fallback', events, streaming=True)

    async def check(service, url):
        calls.append(service)
        return ServiceAvailability(False, '未启动')

    install_route(monkeypatch, routes, 'gpt_sovits', 'fish_audio', target, fallback, check)
    monkeypatch.setattr(playback, 'play_bytes', lambda _: events.append(('clip',)))

    async def second():
        events.append(('next',))

    await asyncio.gather(
        speak_template('{message}', user_name='Alice', message='前咕后'),
        playback.play_job_async(second),
    )
    assert calls == ['gpt_sovits']
    assert events == [
        ('fallback', 'stream', '前'), ('clip',), ('fallback', 'stream', '后'),
        ('target', 'close'), ('fallback', 'close'), ('next',),
    ]


@pytest.mark.asyncio
async def test_clip_only_mapping_needs_no_service_probe(routes, monkeypatch, playback, tmp_path):
    from core.speech import speak_template
    from tests.test_speech import wav_bytes

    events = []
    clip = tmp_path / 'clip.wav'
    clip.write_bytes(wav_bytes())
    routes.audioClipDict.value = {'咕': str(clip)}
    target, fallback = Adapter('target', events), Adapter('fallback', events)

    async def check(*args):
        pytest.fail('Clip-only message needs no TTS service')

    install_route(monkeypatch, routes, 'gpt_sovits', 'fish_audio', target, fallback, check)
    monkeypatch.setattr(playback, 'play_bytes', lambda _: events.append(('clip',)))
    assert await speak_template('{message}', user_name='Alice', message='咕') is None
    assert events == [('clip',), ('target', 'close'), ('fallback', 'close')]
