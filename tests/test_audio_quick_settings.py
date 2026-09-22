"""Quick audition controls must edit the real request settings without API calls."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def preview(config, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtGui import QFontDatabase
    from qfluentwidgets import ConfigItem
    from core.sovits_references import reference_memory
    from gui.components import sovits_cards
    from gui.view.audio_test import AudioTestInterface

    # Windows offscreen Qt does not discover system fonts automatically.
    for font in ('segoeui.ttf', 'msyh.ttc', 'msyhbd.ttc'):
        path = Path('C:/Windows/Fonts') / font
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))

    saved = {name: deepcopy(getattr(config, name).value) for name in dir(config)
             if isinstance(getattr(config, name), ConfigItem)}
    monkeypatch.setattr(sovits_cards, 'scan_models', lambda _: ([
        SimpleNamespace(gpt='a.ckpt', sovits='a.pth', label='角色 A · V4'),
        SimpleNamespace(gpt='b.ckpt', sovits='b.pth', label='角色 B · V4'),
    ], []))
    config.activeTTS.value = 'dots_tts'
    config.dotsVoice.value = ''
    config.dotsNumSteps.value = 10
    config.dotsSpeed.value = 1.0
    before = {name: deepcopy(getattr(config, name).value) for name in saved}
    page = AudioTestInterface()
    assert all(getattr(config, name).value == value for name, value in before.items())
    page.resize(1100, 950)
    page.show()
    QCoreApplication.processEvents()
    yield config, page
    page.close()
    page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    reference_memory.switching = True
    try:
        for name, value in saved.items():
            getattr(config, name).value = value
    finally:
        reference_memory.switching = False


def test_only_current_service_controls_and_no_connection_fields(preview):
    from qfluentwidgets import SettingCard

    cfg, page = preview
    panel = page.quick_settings
    assert set(panel.groups) == {'dots_tts', 'gpt_sovits', 'fish_audio'}
    assert [panel.serviceCard.comboBox.itemData(i) for i in range(panel.serviceCard.comboBox.count())] == [
        'dots_tts', 'gpt_sovits', 'fish_audio',
    ]
    for service in panel.groups:
        panel.serviceCard.comboBox.setCurrentIndex(panel.serviceCard.comboBox.findData(service))
        assert cfg.activeTTS.value == service
        assert [key for key, group in panel.groups.items() if not group.isHidden()] == [service]
        assert all(group.isHidden() for group in panel.advanced.values())
    titles = [card.titleLabel.text() for card in panel.findChildren(SettingCard)]
    assert 'API 地址' not in titles
    assert 'API Key' not in titles
    cfg.activeTTS.value = 'gpt_sovits'
    panel.moreButton.setChecked(True)
    assert not panel.advanced['gpt_sovits'].isHidden()
    cfg.activeTTS.value = 'dots_tts'
    assert panel.advanced['gpt_sovits'].isHidden()
    assert not panel.advanced['dots_tts'].isHidden()
    panel.moreButton.setChecked(False)
    assert panel.advanced['dots_tts'].isHidden()


def test_controls_sync_to_regular_settings_and_persist(preview):
    import json
    from PySide6.QtCore import QCoreApplication, QEvent
    from core.const import DATA_DIR
    from gui.view.settings import SettingsInterface
    from qfluentwidgets import RangeSettingCard
    from gui.components.float_range_setting_card import FloatRangeSettingCard

    cfg, page = preview
    settings = SettingsInterface()
    try:
        from PySide6.QtWidgets import QLineEdit
        assert settings.fishAudioCards['fishAudioApiKey'].lineEdit.echoMode() == QLineEdit.Password
        assert set(settings.tts_interface.groups) == {'dots_tts', 'gpt_sovits', 'fish_audio'}
        panel = page.quick_settings
        panel.cards['dotsNumSteps'].spinBox.setValue(64)
        panel.cards['dotsSpeed'].slider.setValue(13)
        old_steps = next(card for card in settings.dotsGroup.findChildren(RangeSettingCard)
                         if card.configItem is cfg.dotsNumSteps)
        old_speed = next(card for card in settings.dotsGroup.findChildren(FloatRangeSettingCard)
                         if card.configItem is cfg.dotsSpeed)
        assert old_steps.slider.value() == 64
        assert old_speed.slider.value() == 13
        old_steps.slider.setValue(32)
        old_speed.slider.setValue(11)
        assert panel.cards['dotsNumSteps'].spinBox.value() == 32
        assert panel.cards['dotsSpeed'].valueLabel.text() == '1.1'
        persisted = json.loads((DATA_DIR / 'config.json').read_text('utf-8'))
        assert persisted['DotsTTSService']['NumSteps'] == 32
        assert persisted['DotsTTSService']['Speed'] == 1.1
    finally:
        settings.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.mark.asyncio
async def test_audition_uses_changed_settings_and_explains_user_override(preview, monkeypatch):
    from gui.view import audio_test
    from core.user_voices import service_for_user
    from tts_service import dots

    cfg, page = preview
    monkeypatch.setattr(dots, 'local_reference', lambda _: None)
    calls = []

    async def capture(template, **fields):
        service = service_for_user(fields['user_name'])
        calls.append((template, fields, service))

    monkeypatch.setattr(audio_test, 'speak_template', capture)
    page.text_edit.setPlainText('你好，欢迎来到直播间。')
    page.quick_settings.cards['dotsNumSteps'].spinBox.setValue(64)
    page.quick_settings.cards['dotsSpeed'].slider.setValue(12)
    await page._on_test_button_clicked()
    assert calls[-1][2]._request('test')[1]['num_steps'] == 64
    assert calls[-1][2]._settings.dotsSpeed == 1.2
    page.quick_settings.cards['dotsNumSteps'].spinBox.setValue(32)
    await page._on_test_button_clicked()
    assert calls[-1][2]._request('test')[1]['num_steps'] == 32
    assert calls[0][2]._request('test')[1]['num_steps'] == 64
    cfg.gptSovitsRouteFromDots.value = True
    cfg.gptSovitsUserModels.value = {'Alice': {'gpt': 'a.ckpt', 'sovits': 'a.pth', 'label': '角色 A'}}
    page.user_name_edit.setText('Alice')
    assert 'GPT-SoVITS · 角色 A' in page.route_hint.text()
    assert '清空用户名' in page.route_hint.text()
    await page._on_test_button_clicked()
    assert calls[-1][2]._settings.gptSovitsGptModel == 'a.ckpt'
    assert cfg.activeTTS.value == 'dots_tts'
    cfg.gptSovitsUserModelsEnabled.value = False
    assert '本次使用 dots.tts' in page.route_hint.text()
    cfg.gptSovitsUserModelsEnabled.value = True
    page.user_name_edit.clear()
    assert '本次使用 dots.tts' in page.route_hint.text()
    assert page.test_button.isEnabled()


def test_layout_and_capture(preview):
    from PySide6.QtCore import QCoreApplication

    cfg, page = preview
    output = Path(__file__).resolve().parents[1] / 'build' / 'audio-controls-check'
    output.mkdir(parents=True, exist_ok=True)
    for service in ('dots_tts', 'fish_audio', 'gpt_sovits'):
        cfg.activeTTS.value = service
        QCoreApplication.processEvents()
        panel = page.quick_settings
        group = panel.groups[service]
        assert group.y() >= panel.serviceCard.geometry().bottom()
        assert panel.moreButton.y() >= group.geometry().bottom()
        assert page.scrollWidget.width() <= page.viewport().width()
        assert page.grab().save(str(output / f'{service}.png'))
    page.quick_settings.moreButton.setChecked(True)
    QCoreApplication.processEvents()
    page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
    QCoreApplication.processEvents()
    assert page.grab().save(str(output / 'gpt_advanced.png'))


@pytest.mark.parametrize('service,label', [
    ('gpt_sovits', '角色 A'), ('dots_tts', '服务当前固定模型'), ('fish_audio', '网站音色'),
])
def test_audition_hint_explains_cross_service_binding(preview, service, label):
    cfg, page = preview
    cfg.activeTTS.value = 'fish_audio'
    cfg.gptSovitsUserModelsEnabled.value = True
    voice = '4ed45d53ee9245d9abf20db5221b19b2'
    cfg.fishAudioVoices.value = {voice: '网站音色'}
    cfg.gptSovitsUserModels.value = {'Alice': {
        'service': service, 'enabled': True, 'label': '角色 A',
        'gpt': 'a.ckpt', 'sovits': 'a.pth', 'reference_id': voice,
    }}
    page.user_name_edit.setText('Alice')
    assert label in page.route_hint.text()
    assert ('本地服务未就绪' in page.route_hint.text()) == (service != 'fish_audio')
    cfg.gptSovitsUserModelsEnabled.value = False
    assert '本次使用 Fish Audio' in page.route_hint.text()


@pytest.mark.asyncio
async def test_audition_shows_actual_fallback_notice(preview, monkeypatch):
    from gui.view import audio_test

    _, page = preview
    notices = []
    notice = 'GPT-SoVITS 未就绪，本条临时使用默认服务 Fish Audio。'

    async def speak(*_, **__):
        return notice

    monkeypatch.setattr(audio_test, 'speak_template', speak)
    monkeypatch.setattr(audio_test.InfoBar, 'warning', lambda **kwargs: notices.append(kwargs['content']))
    page.text_edit.setPlainText('测试')
    await page._on_test_button_clicked()
    assert notices == [notice]
    assert notice in page.route_hint.text()
    assert page.test_button.isEnabled()


def test_main_navigation_hides_minimax_and_keeps_new_pages(preview, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QWidget
    from PySide6.QtTest import QTest
    from qfluentwidgets import FluentWindow
    from gui.view import main

    cfg, _ = preview
    monkeypatch.setattr(main, 'MainInterface', QWidget)
    window = main.MainWindow.__new__(main.MainWindow)
    FluentWindow.__init__(window)
    try:
        window._setup_interfaces()
        window._set_qss()
        assert not hasattr(window, 'minimax_interface')
        assert window.navigationInterface.widget('ttsSettingsInterface') is not None
        assert window.navigationInterface.widget('userDictionaryInterface') is not None
        window.resize(1200, 900)
        window.show()
        cfg.activeTTS.value = 'fish_audio'
        window.switchTo(window.audio_test_interface)
        QTest.qWait(300)
        output = Path(__file__).resolve().parents[1] / 'build' / 'audio-controls-check'
        assert window.grab().save(str(output / 'main-fish-audio.png'))
        cfg.activeTTS.value = 'dots_tts'
        cfg.gptSovitsUserModels.value = {'测试用户': {
            'gpt': 'a.ckpt', 'sovits': 'a.pth', 'label': '角色 A', 'enabled': True,
        }}
        window.user_dictionary_interface.models.reload_rows()
        window.switchTo(window.user_dictionary_interface)
        QTest.qWait(300)
        assert window.grab().save(str(output / 'main-user-switches.png'))
    finally:
        window.hide()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
