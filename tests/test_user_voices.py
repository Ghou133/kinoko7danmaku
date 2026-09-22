from copy import deepcopy

import pytest


@pytest.fixture
def voice_config(config):
    names = [name for name in dir(config) if name.startswith('gptSovits')]
    saved = {name: deepcopy(getattr(config, name).value) for name in names}
    config.gptSovitsUserModels.value = {}
    config.gptSovitsReferences.value = {}
    config.gptSovitsRouteFromDots.value = False
    config.gptSovitsUserModelsEnabled.value = True
    yield config
    from core.sovits_references import reference_memory
    reference_memory.switching = True
    for name, value in saved.items():
        getattr(config, name).value = value
    reference_memory.switching = False


def test_user_role_is_exact_original_name_and_independent_of_default_engine(voice_config):
    from core.sovits_references import pair_key
    from core.user_voices import user_model_overrides
    cfg = voice_config
    cfg.activeTTS.value = 'gpt_sovits'
    cfg.gptSovitsGptModel.value = 'default.ckpt'
    cfg.gptSovitsSovitsModel.value = 'default.pth'
    cfg.gptSovitsRefAudioPath.value = 'default.wav'
    cfg.aliasDict.value = {'Alice': '爱丽丝'}
    cfg.gptSovitsUserModels.value = {'Alice': {'gpt': 'A.ckpt', 'sovits': 'A.pth'}}
    cfg.gptSovitsReferences.value = {pair_key('A.ckpt', 'A.pth'): {
        'audio': 'A.wav', 'text': '角色 A', 'language': 'Chinese', 'text_free': False,
    }}
    settings = user_model_overrides('Alice')
    assert settings['gptSovitsRefAudioPath'] == 'A.wav'
    assert settings['gptSovitsRefText'] == '角色 A'
    assert user_model_overrides('爱丽丝') is None
    assert user_model_overrides('alice') is None
    assert cfg.gptSovitsGptModel.value == 'default.ckpt'
    assert cfg.gptSovitsRefAudioPath.value == 'default.wav'
    cfg.activeTTS.value = 'dots_tts'
    assert user_model_overrides('Alice')['gptSovitsGptModel'] == 'A.ckpt'


@pytest.mark.parametrize('active,enabled,expected', [
    ('dots_tts', False, 'GPTSovitsService'),
    ('dots_tts', True, 'GPTSovitsService'),
    ('gpt_sovits', False, 'GPTSovitsService'),
    ('gpt_sovits', True, 'GPTSovitsService'),
    ('edge', True, 'GPTSovitsService'),
    ('minimax', True, 'GPTSovitsService'),
])
def test_legacy_gpt_mapping_routes_from_any_default_without_old_dots_gate(voice_config, active, enabled, expected):
    from core.user_voices import service_for_user, user_model_overrides
    cfg = voice_config
    cfg.activeTTS.value = active
    cfg.gptSovitsRouteFromDots.value = enabled
    cfg.gptSovitsUserModels.value = {'Alice': {'gpt': 'A.ckpt', 'sovits': 'A.pth'}}
    service = service_for_user('Alice')
    assert type(service).__name__ == expected
    assert cfg.activeTTS.value == active
    if expected == 'GPTSovitsService':
        assert service._settings.gptSovitsGptModel == 'A.ckpt'
    else:
        assert user_model_overrides('Alice') is None


def test_unmapped_dots_user_remains_dots(voice_config):
    from core.user_voices import service_for_user
    cfg = voice_config
    cfg.activeTTS.value = 'dots_tts'
    cfg.gptSovitsRouteFromDots.value = True
    cfg.gptSovitsUserModels.value = {'Alice': {'gpt': 'A.ckpt', 'sovits': 'A.pth'}}
    for username in ('', 'alice', 'unmapped'):
        assert type(service_for_user(username)).__name__ == 'DotsTTSService'
    assert cfg.activeTTS.value == 'dots_tts'


@pytest.mark.parametrize('active', ['gpt_sovits', 'dots_tts'])
@pytest.mark.parametrize('master_enabled,user_enabled', [
    (False, False), (False, True), (True, False), (True, True),
])
def test_master_and_individual_switches_both_required(voice_config, active, master_enabled, user_enabled):
    from core.user_voices import service_for_user, user_model_overrides

    cfg = voice_config
    cfg.activeTTS.value = active
    cfg.gptSovitsGptModel.value = 'default.ckpt'
    cfg.gptSovitsSovitsModel.value = 'default.pth'
    cfg.gptSovitsRouteFromDots.value = True
    cfg.gptSovitsUserModelsEnabled.value = master_enabled
    binding = {'gpt': 'A.ckpt', 'sovits': 'A.pth', 'enabled': user_enabled}
    cfg.gptSovitsUserModels.value = {'Alice': binding}

    overrides = user_model_overrides('Alice')
    service = service_for_user('Alice')
    if master_enabled and user_enabled:
        assert overrides['gptSovitsGptModel'] == 'A.ckpt'
        assert type(service).__name__ == 'GPTSovitsService'
        assert service._settings.gptSovitsGptModel == 'A.ckpt'
    else:
        assert overrides is None
        if active == 'dots_tts':
            assert type(service).__name__ == 'DotsTTSService'
        else:
            assert type(service).__name__ == 'GPTSovitsService'
            assert service._settings.gptSovitsGptModel == 'default.ckpt'
    assert cfg.gptSovitsUserModels.value['Alice'] == binding
    assert cfg.activeTTS.value == active


def test_legacy_binding_missing_enabled_remains_active_without_mutation(voice_config):
    from core.user_voices import user_model_overrides

    cfg = voice_config
    cfg.activeTTS.value = 'gpt_sovits'
    old_mapping = {'Alice': {'gpt': 'A.ckpt', 'sovits': 'A.pth'}}
    cfg.gptSovitsUserModels.value = deepcopy(old_mapping)
    assert user_model_overrides('Alice')['gptSovitsGptModel'] == 'A.ckpt'
    assert cfg.gptSovitsUserModels.value == old_mapping


def test_fish_audio_default_can_route_users_to_gpt(voice_config):
    from core.user_voices import user_model_overrides

    cfg = voice_config
    cfg.activeTTS.value = 'fish_audio'
    cfg.gptSovitsRouteFromDots.value = True
    cfg.gptSovitsUserModels.value = {'Alice': {'gpt': 'A.ckpt', 'sovits': 'A.pth', 'enabled': True}}
    assert user_model_overrides('Alice')['gptSovitsGptModel'] == 'A.ckpt'


def test_missing_role_reference_does_not_inherit_default(voice_config):
    from core.user_voices import user_model_overrides
    cfg = voice_config
    cfg.activeTTS.value = 'gpt_sovits'
    cfg.gptSovitsGptModel.value = 'default.ckpt'
    cfg.gptSovitsSovitsModel.value = 'default.pth'
    cfg.gptSovitsRefAudioPath.value = 'default.wav'
    cfg.gptSovitsUserModels.value = {'Bob': {'gpt': 'B.ckpt', 'sovits': 'B.pth'}}
    assert user_model_overrides('Bob')['gptSovitsRefAudioPath'] == ''
