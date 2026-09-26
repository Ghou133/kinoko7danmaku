"""Offline Doubao voice choices shared by settings, audition, and user bindings."""

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QSizePolicy
from qfluentwidgets import ComboBox, SettingCard, qconfig
from qfluentwidgets import FluentIcon as FIF

from core.dobao_voices import get_dobao_voices, normalize_dobao_voice
from core.qconfig import cfg


def populate_dobao_voice_combo(combo, selected):
    """Display aliases canonically without rewriting the selected stored value."""
    try:
        selected_id = normalize_dobao_voice(selected)
    except ValueError:
        selected_id = None
    with QSignalBlocker(combo):
        combo.clear()
        for voice in get_dobao_voices():
            combo.addItem(voice['name'], userData=voice['id'])
        if selected_id is not None and combo.findData(selected_id) < 0:
            combo.addItem(f'{selected_id}（已保存，目录未收录）', userData=selected_id)
        combo.setCurrentIndex(combo.findData(selected_id) if selected_id is not None else -1)


class DoBaoVoiceCard(SettingCard):
    def __init__(self, parent=None):
        super().__init__(FIF.MUSIC, '音色', '完整音色列表；无需启动 API 即可选择', parent)
        self.configItem = cfg.dobaoVoice
        self.comboBox = ComboBox(self)
        self.comboBox.setPlaceholderText('请选择音色')
        self.comboBox.setMaxVisibleItems(12)
        self.comboBox.setMinimumWidth(240)
        self.comboBox.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.hBoxLayout.addWidget(self.comboBox)
        self.hBoxLayout.addSpacing(16)
        self.sync(self.configItem.value)
        self.comboBox.currentIndexChanged.connect(self.change)
        self.configItem.valueChanged.connect(self.sync)

    def sync(self, value):
        populate_dobao_voice_combo(self.comboBox, value)

    def change(self, index):
        if index >= 0:
            qconfig.set(self.configItem, self.comboBox.itemData(index))
