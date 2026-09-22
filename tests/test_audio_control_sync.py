"""Quick controls and the full TTS page share config without replaying edits."""

from copy import deepcopy
from types import SimpleNamespace

import pytest


@pytest.fixture
def cards(config):
    from PySide6.QtCore import QCoreApplication, QEvent

    widgets = []
    yield widgets
    for widget in widgets:
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_two_model_selectors_preserve_each_models_reference(config, cards, monkeypatch):
    from qfluentwidgets import FluentIcon as FIF, qconfig
    from core.sovits_references import pair_key, reference_memory
    from gui.components import sovits_cards
    from gui.components.str_setting_card import StrSettingCard

    names = [name for name in dir(config) if name.startswith('gptSovits')]
    saved = {name: deepcopy(getattr(config, name).value) for name in names}
    pairs = [SimpleNamespace(gpt=f'{name}.ckpt', sovits=f'{name}.pth', label=name)
             for name in ('A', 'B')]
    monkeypatch.setattr(sovits_cards, 'scan_models', lambda _: (pairs, []))
    calls = []
    real_select = reference_memory.select

    def select(gpt, sovits):
        calls.append((gpt, sovits))
        real_select(gpt, sovits)

    monkeypatch.setattr(reference_memory, 'select', select)
    try:
        reference_memory.switching = True
        config.gptSovitsGptModel.value = ''
        config.gptSovitsSovitsModel.value = ''
        config.gptSovitsRefAudioPath.value = ''
        config.gptSovitsRefText.value = ''
        config.gptSovitsReferences.value = {}
        reference_memory.switching = False
        first, second = sovits_cards.SovitsModelCard(), sovits_cards.SovitsModelCard()
        text_a = StrSettingCard(config.gptSovitsRefText, FIF.FONT, '参考文本')
        text_b = StrSettingCard(config.gptSovitsRefText, FIF.FONT, '参考文本')
        cards.extend((first, second, text_a, text_b))
        first.combo.setCurrentIndex(1)
        assert second.combo.currentIndex() == 1
        assert calls == [('A.ckpt', 'A.pth')]
        qconfig.set(config.gptSovitsRefAudioPath, 'A.wav')
        text_a.lineEdit.setText('角色 A 的文本')
        assert text_b.lineEdit.text() == '角色 A 的文本'

        second.combo.setCurrentIndex(2)
        assert first.combo.currentIndex() == 2
        assert config.gptSovitsRefAudioPath.value == ''
        assert text_a.lineEdit.text() == text_b.lineEdit.text() == ''
        qconfig.set(config.gptSovitsRefAudioPath, 'B.wav')
        text_b.lineEdit.setText('角色 B 的文本')

        first.combo.setCurrentIndex(1)
        assert second.combo.currentIndex() == 1
        assert config.gptSovitsRefAudioPath.value == 'A.wav'
        assert text_a.lineEdit.text() == text_b.lineEdit.text() == '角色 A 的文本'
        assert config.gptSovitsReferences.value[pair_key('B.ckpt', 'B.pth')]['text'] == '角色 B 的文本'
        # A passive refresh must not save A's reference under B, or reselect.
        second.reload()
        assert second.combo.currentIndex() == 1
        assert calls == [('A.ckpt', 'A.pth'), ('B.ckpt', 'B.pth'), ('A.ckpt', 'A.pth')]
        second.combo.setCurrentIndex(2)
        assert config.gptSovitsRefAudioPath.value == 'B.wav'
        assert text_a.lineEdit.text() == text_b.lineEdit.text() == '角色 B 的文本'
    finally:
        reference_memory.switching = True
        for name, value in saved.items():
            getattr(config, name).value = value
        reference_memory.switching = False


def test_float_controls_sync_real_units_and_emit_only_user_edits(cards):
    from qfluentwidgets import FluentIcon as FIF, RangeConfigItem, RangeValidator, qconfig
    from gui.components.float_range_setting_card import FloatRangeSettingCard

    item = RangeConfigItem('SyncTest', 'Speed', 1.5, RangeValidator(0.5, 2.0))
    first = FloatRangeSettingCard(item, FIF.SPEED_HIGH, '语速')
    second = FloatRangeSettingCard(item, FIF.SPEED_HIGH, '语速')
    cards.extend((first, second))
    first_edits, second_edits = [], []
    first.valueChanged.connect(first_edits.append)
    second.valueChanged.connect(second_edits.append)

    qconfig.set(item, 1)
    assert item.value == 1
    assert first.slider.value() == second.slider.value() == 10
    assert first.valueLabel.text() == second.valueLabel.text() == '1.0'
    assert first_edits == second_edits == []
    first.slider.setValue(13)
    assert item.value == 1.3
    assert second.slider.value() == 13
    assert first_edits == [1.3]
    assert second_edits == []
    second.setValue(2)
    assert item.value == 2
    assert first.slider.value() == second.slider.value() == 20


def test_integer_controls_share_corrected_value_without_passive_edit_signal(cards):
    from qfluentwidgets import FluentIcon as FIF, RangeConfigItem, RangeValidator
    from gui.components.int_setting_card import IntSettingCard

    item = RangeConfigItem('SyncTest', 'Steps', 10, RangeValidator(1, 64))
    first, second = [IntSettingCard(item, FIF.SPEED_HIGH, '推理步数') for _ in range(2)]
    cards.extend((first, second))
    first_edits, second_edits = [], []
    first.valueChanged.connect(first_edits.append)
    second.valueChanged.connect(second_edits.append)
    first.lineEdit.setText('80')
    assert item.value == 64
    assert first.lineEdit.text() == second.lineEdit.text() == '64'
    assert first_edits == [64]
    assert second_edits == []
    second.lineEdit.setText('32')
    assert item.value == 32
    assert first.lineEdit.text() == second.lineEdit.text() == '32'
    assert first_edits == [64]
    assert second_edits == [32]
