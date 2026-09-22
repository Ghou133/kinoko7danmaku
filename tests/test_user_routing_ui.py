"""Mapping switches remain persistent while services are selected per user."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def routing_editor(config, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from gui.view import user_dictionary

    names = ('gptSovitsRouteFromDots', 'gptSovitsUserModels', 'gptSovitsUserModelsEnabled')
    saved = {name: deepcopy(getattr(config, name).value) for name in names}
    config.activeTTS.value = 'dots_tts'
    config.gptSovitsRouteFromDots.value = False
    config.gptSovitsUserModels.value = {}
    config.gptSovitsUserModelsEnabled.value = True
    pair = SimpleNamespace(gpt='role.ckpt', sovits='role.pth', label='测试角色')
    monkeypatch.setattr(user_dictionary, 'scan_models', lambda folder: ([pair], []))
    monkeypatch.setattr(user_dictionary, 'model_reference', lambda gpt, sovits: {
        'audio': 'reference.wav', 'text': '参考文本', 'text_free': False,
    })
    editor = user_dictionary.UserModelsWidget()
    yield config, editor
    editor.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    for name, value in saved.items():
        getattr(config, name).value = value


def test_legacy_dots_switch_is_removed_without_changing_default(routing_editor):
    cfg, editor = routing_editor
    assert not hasattr(editor, 'routeFromDotsCard')
    assert cfg.gptSovitsRouteFromDots.value is False
    assert cfg.activeTTS.value == 'dots_tts'
    assert '临时使用默认服务' in editor.hint.text()
    assert '原始用户名精确匹配' in editor.hint.text()


@pytest.mark.parametrize('route_enabled', [False, True])
def test_dots_can_prepare_and_edit_gpt_roles_even_with_routing_off(routing_editor, route_enabled):
    from qfluentwidgets import BodyLabel

    cfg, editor = routing_editor
    cfg.gptSovitsRouteFromDots.value = route_enabled
    assert editor.form.isEnabled()
    editor.username.setText('Alice')
    editor.save()
    assert cfg.gptSovitsUserModels.value['Alice']['gpt'] == 'role.ckpt'
    assert cfg.gptSovitsRouteFromDots.value is route_enabled
    assert cfg.activeTTS.value == 'dots_tts'
    row = editor.rows.itemAt(0).widget()
    assert any('Alice → GPT-SoVITS' in label.text() for label in row.findChildren(BodyLabel))
    editor.username.clear()
    editor.edit('Alice')
    assert editor.username.text() == 'Alice'
    assert editor.models.currentIndex() == 0
    editor.remove('Alice')
    assert cfg.gptSovitsUserModels.value == {}


def test_engine_changes_keep_all_bindings_editable_and_hint_consistent(routing_editor):
    cfg, editor = routing_editor
    cfg.gptSovitsRouteFromDots.value = True
    cfg.activeTTS.value = 'gpt_sovits'
    assert editor.form.isEnabled()
    assert '当前默认服务：GPT-SoVITS' in editor.hint.text()
    for engine in ('edge', 'minimax', 'piper', 'fish_speech', 'fish_audio'):
        cfg.activeTTS.value = engine
        assert editor.form.isEnabled()
        assert '未绑定或独立开关关闭' in editor.hint.text()
        assert cfg.gptSovitsRouteFromDots.value is True
    cfg.activeTTS.value = 'dots_tts'
    assert editor.form.isEnabled()
    assert '当前默认服务：dots' in editor.hint.text()


def test_master_switch_persists_and_keeps_bindings_editable(routing_editor):
    from core.const import DATA_DIR
    from core.user_voices import user_model_overrides

    cfg, editor = routing_editor
    cfg.gptSovitsRouteFromDots.value = True
    editor.username.setText('Alice')
    editor.save()
    mapping = deepcopy(cfg.gptSovitsUserModels.value)
    assert editor.modelsEnabledCard.isEnabled()
    assert user_model_overrides('Alice') is not None
    editor.modelsEnabledCard.switchButton.setChecked(False)
    assert cfg.gptSovitsUserModelsEnabled.value is False
    assert user_model_overrides('Alice') is None
    assert '总开关已关闭' in editor.hint.text()
    assert editor.form.isEnabled()
    assert editor.userSwitches['Alice'].isEnabled()
    editor.edit('Alice')
    editor.save()
    assert cfg.gptSovitsUserModels.value == mapping
    persisted = json.loads((DATA_DIR / 'config.json').read_text('utf-8'))
    assert persisted['GptSovitsService'][cfg.gptSovitsUserModelsEnabled.name] is False
    editor.modelsEnabledCard.switchButton.setChecked(True)
    assert user_model_overrides('Alice') is not None
    cfg.activeTTS.value = 'edge'
    assert editor.modelsEnabledCard.isEnabled()


def test_per_user_switch_persists_across_edit_and_master_toggle(routing_editor):
    from core.const import DATA_DIR
    from core.user_voices import user_model_overrides

    cfg, editor = routing_editor
    cfg.gptSovitsRouteFromDots.value = True
    for name in ('Alice', 'Bob'):
        editor.username.setText(name)
        editor.save()
    editor.userSwitches['Alice'].setChecked(False)
    assert user_model_overrides('Alice') is None
    assert user_model_overrides('Bob') is not None
    before = deepcopy(cfg.gptSovitsUserModels.value['Alice'])
    editor.edit('Alice')
    editor.save()
    assert cfg.gptSovitsUserModels.value['Alice'] == before
    assert not editor.userSwitches['Alice'].isChecked()
    editor.modelsEnabledCard.switchButton.setChecked(False)
    editor.modelsEnabledCard.switchButton.setChecked(True)
    assert user_model_overrides('Alice') is None
    persisted = json.loads((DATA_DIR / 'config.json').read_text('utf-8'))
    assert persisted['GptSovitsService']['UsernameModels']['Alice']['enabled'] is False
    assert persisted['GptSovitsService']['UsernameModels']['Bob']['enabled'] is True
    editor.userSwitches['Alice'].setChecked(True)
    assert user_model_overrides('Alice') is not None


def test_legacy_ui_mapping_defaults_on_and_adds_enabled_only_when_saved(routing_editor):
    cfg, editor = routing_editor
    cfg.gptSovitsUserModels.value = {
        'Alice': {'gpt': 'role.ckpt', 'sovits': 'role.pth', 'label': '旧角色'},
    }
    editor.reload_rows()
    assert editor.userSwitches['Alice'].isChecked()
    assert 'enabled' not in cfg.gptSovitsUserModels.value['Alice']
    editor.edit('Alice')
    editor.save()
    assert cfg.gptSovitsUserModels.value['Alice']['enabled'] is True
