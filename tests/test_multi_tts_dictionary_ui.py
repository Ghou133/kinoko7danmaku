"""Per-user service forms, local status checks and narrow-window layout."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


VOICE = '4ed45d53ee9245d9abf20db5221b19b2'


@pytest.fixture
def editor(config, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from gui.view import user_dictionary

    names = [name for name in dir(config) if name.startswith('gptSovits')]
    names += ['fishAudioVoices', 'fishAudioReferenceId']
    saved = {name: deepcopy(getattr(config, name).value) for name in names}
    config.activeTTS.value = 'fish_audio'
    config.gptSovitsUserModels.value = {}
    config.gptSovitsUserModelsEnabled.value = True
    config.gptSovitsRouteFromDots.value = False
    config.fishAudioVoices.value = {VOICE: '莫提斯'}
    config.fishAudioReferenceId.value = ''
    pair = SimpleNamespace(gpt='role.ckpt', sovits='role.pth', label='测试角色')
    monkeypatch.setattr(user_dictionary, 'scan_models', lambda _: ([pair], []))
    monkeypatch.setattr(user_dictionary, 'model_reference', lambda *_: {
        'audio': 'reference.wav', 'text': '参考文本', 'text_free': False,
    })

    async def forbidden(*_, **__):
        pytest.fail('Saving/editing bindings must not probe or synthesize')

    monkeypatch.setattr(user_dictionary, 'check_service_availability', forbidden)
    widget = user_dictionary.UserModelsWidget()
    yield config, widget
    from shiboken6 import isValid
    if isValid(widget):
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    from core.sovits_references import reference_memory
    reference_memory.switching = True
    for name, value in saved.items():
        getattr(config, name).value = value
    reference_memory.switching = False


def select_service(widget, kind):
    widget.service.setCurrentIndex(widget.service.findData(kind))


def test_visible_services_and_conditional_fields_allow_offline_preconfiguration(editor):
    cfg, widget = editor
    assert {widget.service.itemData(i) for i in range(widget.service.count())} == {
        'gpt_sovits', 'dots_tts', 'fish_audio',
    }
    assert widget.form.isEnabled()
    for service in ('dots_tts', 'fish_audio', 'gpt_sovits'):
        select_service(widget, service)
        assert widget.models.isHidden() is (service != 'gpt_sovits')
        assert widget.fishVoices.isHidden() is (service != 'fish_audio')
        assert widget.dotsModel.isHidden() is (service != 'dots_tts')
        widget.username.setText(service)
        if service == 'fish_audio':
            widget.fishVoices.setCurrentIndex(widget.fishVoices.findData(VOICE))
        widget.save()
        assert cfg.gptSovitsUserModels.value[service]['service'] == service
    assert cfg.activeTTS.value == 'fish_audio'
    assert cfg.fishAudioReferenceId.value == ''
    assert cfg.gptSovitsRouteFromDots.value is False
    assert cfg.gptSovitsUserModels.value['dots_tts'] == {
        'service': 'dots_tts', 'enabled': True, 'label': '当前固定模型',
    }


def test_service_change_removes_obsolete_fields_and_preserves_user_switch(editor):
    from core.const import DATA_DIR

    cfg, widget = editor
    widget.username.setText(' Alice ')
    widget.save()
    assert ' Alice ' in cfg.gptSovitsUserModels.value
    assert 'Alice' not in cfg.gptSovitsUserModels.value
    widget.userSwitches[' Alice '].setChecked(False)
    select_service(widget, 'fish_audio')
    widget.fishVoices.setCurrentIndex(widget.fishVoices.findData(VOICE))
    widget.save()
    expected = {'service': 'fish_audio', 'enabled': False, 'reference_id': VOICE, 'label': '莫提斯'}
    assert cfg.gptSovitsUserModels.value[' Alice '] == expected
    widget.username.clear()
    widget.edit(' Alice ')
    assert widget.username.text() == ' Alice '
    assert widget.service.currentData() == 'fish_audio'
    assert widget.fishVoices.currentData() == VOICE
    assert not widget.userSwitches[' Alice '].isChecked()
    stored = json.loads((DATA_DIR / 'config.json').read_text('utf-8'))
    assert stored['GptSovitsService']['UsernameModels'][' Alice '] == expected
    select_service(widget, 'dots_tts')
    widget.save()
    assert cfg.gptSovitsUserModels.value[' Alice '] == {
        'service': 'dots_tts', 'enabled': False, 'label': '当前固定模型',
    }


def test_fish_binding_requires_saved_voice_and_refreshes_named_list(editor):
    from qfluentwidgets import BodyLabel

    cfg, widget = editor
    select_service(widget, 'fish_audio')
    widget.username.setText('Alice')
    widget.save()
    assert cfg.gptSovitsUserModels.value == {}
    assert '请选择已保存' in widget.status.text()
    widget.fishVoices.setCurrentIndex(widget.fishVoices.findData(VOICE))
    widget.save()
    cfg.fishAudioVoices.value = {VOICE: '修改后的名字'}
    assert widget.fishVoices.currentData() == VOICE
    assert '修改后的名字' in widget.fishVoices.currentText()
    assert any('修改后的名字' in label.text()
               for label in widget.rows.itemAt(0).widget().findChildren(BodyLabel))
    cfg.fishAudioVoices.value = {}
    widget.edit('Alice')
    assert widget.fishVoices.currentData() == ''
    assert '不在已保存' in widget.status.text()
    previous = deepcopy(cfg.gptSovitsUserModels.value)
    assert previous['Alice']['reference_id'] == VOICE
    assert any('绑定保留' in label.text()
               for label in widget.rows.itemAt(0).widget().findChildren(BodyLabel))
    widget.save()
    assert cfg.gptSovitsUserModels.value == previous


def test_gpt_reference_validation_and_legacy_mapping_edit(editor, monkeypatch):
    from gui.view import user_dictionary

    cfg, widget = editor
    cfg.gptSovitsUserModels.value = {'Alice': {
        'gpt': 'role.ckpt', 'sovits': 'role.pth', 'label': '旧角色', 'enabled': False,
    }}
    widget.edit('Alice')
    assert widget.service.currentData() == 'gpt_sovits'
    assert widget.models.currentIndex() == 0
    monkeypatch.setattr(user_dictionary, 'model_reference', lambda *_: {'audio': 'ref.wav'})
    widget.save()
    assert 'service' not in cfg.gptSovitsUserModels.value['Alice']
    assert '参考音频与对应文本' in widget.status.text()
    monkeypatch.setattr(user_dictionary, 'model_reference', lambda *_: {'audio': 'ref.wav', 'text_free': True})
    widget.save()
    assert cfg.gptSovitsUserModels.value['Alice']['service'] == 'gpt_sovits'
    assert cfg.gptSovitsUserModels.value['Alice']['enabled'] is False
    widget.remove('Alice')
    assert cfg.gptSovitsUserModels.value == {}


@pytest.mark.asyncio
async def test_manual_check_is_concurrent_cloud_free_and_does_not_change_selected_service(editor, monkeypatch):
    from gui.view import user_dictionary

    cfg, widget = editor
    calls = []
    both_started, release = asyncio.Event(), asyncio.Event()

    async def probe(kind, url, *, force=False):
        calls.append((kind, url, force))
        if len(calls) == 2:
            both_started.set()
        await release.wait()
        return SimpleNamespace(available=kind == 'dots_tts', detail='测试状态')

    monkeypatch.setattr(user_dictionary, 'check_service_availability', probe)
    task = widget.check_services()
    await asyncio.wait_for(both_started.wait(), 1)
    assert not widget.checkButton.isEnabled()
    select_service(widget, 'fish_audio')
    release.set()
    await task
    assert {kind for kind, _, _ in calls} == {'dots_tts', 'gpt_sovits'}
    assert all(force for _, _, force in calls)
    assert widget.service.currentData() == 'fish_audio'
    assert cfg.activeTTS.value == 'fish_audio'
    assert 'dots：可用' in widget.availability.text()
    assert 'GPT-SoVITS：不可用' in widget.availability.text()
    assert '无需本地启动' in widget.availability.text()
    assert widget.checkButton.isEnabled()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['url', 'close', 'delete'])
async def test_late_local_status_cannot_override_changed_endpoint_or_closed_widget(editor, monkeypatch, change):
    from PySide6.QtCore import QCoreApplication, QEvent
    from gui.view import user_dictionary

    cfg, widget = editor
    started, release = asyncio.Event(), asyncio.Event()

    async def probe(*_, **__):
        started.set()
        await release.wait()
        return SimpleNamespace(available=True, detail='旧结果')

    monkeypatch.setattr(user_dictionary, 'check_service_availability', probe)
    task = widget.check_services()
    await started.wait()
    before = widget.availability.text()
    if change == 'url':
        cfg.dotsApiUrl.value = 'http://127.0.0.1:12345'
    elif change == 'close':
        widget.close()
    else:
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    release.set()
    await task
    if change == 'url':
        assert '地址已修改，请重新检测' in widget.availability.text()
    elif change == 'close':
        assert widget.availability.text() == before


def test_dictionary_page_integrated_persistence_and_narrow_layout(editor):
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtTest import QTest
    from core.const import DATA_DIR
    from gui.view.settings import SettingsInterface

    cfg, _ = editor
    for font in ('segoeui.ttf', 'msyh.ttc'):
        font_path = Path('C:/Windows/Fonts') / font
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))
    settings = SettingsInterface()
    page = settings.user_dictionary_interface
    page.setParent(None)
    page.resize(800, 920)
    widget = page.models
    widget.username.setText('测试用户')
    select_service(widget, 'fish_audio')
    widget.fishVoices.setCurrentIndex(widget.fishVoices.findData(VOICE))
    widget.save()
    assert cfg.gptSovitsUserModels.value['测试用户']['reference_id'] == VOICE
    assert settings.aliasDictCard.parentWidget() is not settings.biliGroup
    persisted = json.loads((DATA_DIR / 'config.json').read_text('utf-8'))
    assert persisted['GptSovitsService']['UsernameModels']['测试用户']['service'] == 'fish_audio'
    page.show()
    QTest.qWait(100)
    QCoreApplication.processEvents()
    assert widget.form.width() <= page.width() - 72
    assert widget.username.geometry().right() < widget.service.geometry().left()
    assert widget.fishVoices.geometry().right() < widget.bind.geometry().left()
    assert page.scroll.horizontalScrollBar().maximum() == 0
    output = Path(__file__).resolve().parents[1] / 'build' / 'multi-tts-check'
    output.mkdir(parents=True, exist_ok=True)
    assert page.grab().save(str(output / 'dictionary-fish.png'))
    select_service(widget, 'dots_tts')
    QCoreApplication.processEvents()
    assert page.grab().save(str(output / 'dictionary-dots.png'))
    select_service(widget, 'gpt_sovits')
    widget.models.setItemText(0, '一个很长的角色模型名称用于验证窄窗口不会横向溢出' * 4)
    widget.models.setCurrentIndex(-1)
    widget.models.setCurrentIndex(0)
    QCoreApplication.processEvents()
    assert widget.form.width() <= page.width() - 72
    assert page.scroll.horizontalScrollBar().maximum() == 0
    assert page.grab().save(str(output / 'dictionary-gpt.png'))
    widget.remove('测试用户')
    assert cfg.gptSovitsUserModels.value == {}
    page.deleteLater()
    settings.deleteLater()
