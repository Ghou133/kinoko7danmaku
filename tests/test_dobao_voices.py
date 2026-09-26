"""Bundled voice coverage, old configuration compatibility, and portable lookup."""

from pathlib import Path

import pytest

from core import dobao_voices
from core.dobao_voices import (
    CLASSIC_DOBAO_VOICE,
    DEFAULT_DOBAO_VOICE,
    detect_dobao_folder,
    dobao_voice_name,
    get_dobao_voices,
    normalize_dobao_voice,
)


def test_complete_offline_catalog_contains_all_30_distinct_voices():
    voices = get_dobao_voices()
    by_name = {row['name']: row['id'] for row in voices}
    assert len(voices) == len(set(row['id'] for row in voices)) == 30
    assert by_name['温柔桃子（升级版）'] == DEFAULT_DOBAO_VOICE
    assert by_name['温柔桃子（经典版）'] == CLASSIC_DOBAO_VOICE
    assert by_name['磁性俊宇（升级版）'] == 'zh_male_cixingjunyu_uranus_bigtts'
    assert by_name['腹黑霸总'] == 'ICL_c021bc19bf92'
    assert by_name['青涩沐阳'] == 'ICL_afedffe4586c'
    voices[0]['id'] = 'modified-by-caller'
    assert get_dobao_voices()[0]['id'] == DEFAULT_DOBAO_VOICE


@pytest.mark.parametrize('value,expected', [
    ('taozi', DEFAULT_DOBAO_VOICE),
    ('  taozi-classic  ', CLASSIC_DOBAO_VOICE),
    ('104', DEFAULT_DOBAO_VOICE),
    ('温柔桃子（经典版）', CLASSIC_DOBAO_VOICE),
    ('future_unknown_voice', 'future_unknown_voice'),
    ('  ICL_c021bc19bf92  ', 'ICL_c021bc19bf92'),
])
def test_normalization_retains_legacy_aliases_and_future_ids(value, expected):
    assert normalize_dobao_voice(value) == expected


@pytest.mark.parametrize('value', ['', '  ', None, 123, 'x' * 201, 'voice\x00id', 'voice\nid'])
def test_empty_invalid_or_unbounded_voice_ids_are_rejected(value):
    with pytest.raises(ValueError, match='豆包音色'):
        normalize_dobao_voice(value)


def test_display_names_do_not_replace_unknown_saved_ids():
    assert dobao_voice_name('taozi-classic') == '温柔桃子（经典版）'
    assert dobao_voice_name('ICL_c021bc19bf92') == '腹黑霸总'
    assert dobao_voice_name('future_unknown_voice') == 'future_unknown_voice'
    assert dobao_voice_name('') == '未选择音色'


def test_config_loading_does_not_reset_new_voices_or_legacy_aliases(config):
    previous = config.dobaoVoice.value
    try:
        for value in ['future_unknown_voice', 'ICL_c021bc19bf92', 'taozi', 'taozi-classic']:
            config.dobaoVoice.deserializeFrom(value)
            assert config.dobaoVoice.value == value
            assert config.dobaoVoice.serialize() == value
        assert hasattr(config, 'dobaoFolder')
    finally:
        config.dobaoVoice.value = previous


def test_portable_source_and_executable_folder_detection(tmp_path, monkeypatch):
    install = tmp_path / 'DoBao-TTS-Win'
    (install / 'runtime').mkdir(parents=True)
    (install / 'local-api').mkdir()
    (install / 'runtime/node.exe').touch()
    (install / 'local-api/server.mjs').touch()
    source = tmp_path / 'danmaku' / 'src' / 'core' / 'dobao_voices.py'
    monkeypatch.setattr(dobao_voices, '__file__', str(source))
    monkeypatch.setattr(dobao_voices.sys, 'frozen', False, raising=False)
    assert Path(detect_dobao_folder()) == install
    monkeypatch.setattr(dobao_voices.sys, 'frozen', True)
    monkeypatch.setattr(dobao_voices.sys, 'executable', str(tmp_path / 'danmaku' / 'outputs' / 'Danmaku.exe'))
    assert Path(detect_dobao_folder()) == install
    (install / 'runtime/node.exe').unlink()
    assert detect_dobao_folder() == ''


def test_generic_service_description_keeps_hidden_seed_backend():
    from core.const import SUPPORTED_SERVICES, VISIBLE_TTS_SERVICES
    from models.service import ServiceType

    assert SUPPORTED_SERVICES[ServiceType.DOBAO].description == 'Doubao'
    assert ServiceType.DOBAO in VISIBLE_TTS_SERVICES
    assert ServiceType.SEED_TTS in SUPPORTED_SERVICES
    assert ServiceType.SEED_TTS not in VISIBLE_TTS_SERVICES
