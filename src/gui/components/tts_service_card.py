"""Only expose the user's enabled service choices; retain legacy configuration."""

from PySide6.QtCore import QSignalBlocker
from qfluentwidgets import ComboBox, SettingCard, qconfig
from qfluentwidgets import FluentIcon as FIF

from core.const import SUPPORTED_SERVICES, VISIBLE_TTS_SERVICES
from core.qconfig import cfg


class TTSServiceCard(SettingCard):
    def __init__(self, parent=None):
        super().__init__(FIF.MICROPHONE, 'TTS 服务',
                         '与 TTS 设置同步保存；调整用于下一次试听和后续弹幕。', parent)
        self.comboBox = ComboBox(self)
        for service in VISIBLE_TTS_SERVICES:
            self.comboBox.addItem(SUPPORTED_SERVICES[service].description, userData=service)
        self.hBoxLayout.addWidget(self.comboBox)
        self.hBoxLayout.addSpacing(16)
        self.sync(cfg.activeTTS.value)
        self.comboBox.currentIndexChanged.connect(self.change)
        cfg.activeTTS.valueChanged.connect(self.sync)

    def change(self, index):
        if index >= 0:
            qconfig.set(cfg.activeTTS, self.comboBox.itemData(index))

    def sync(self, value):
        with QSignalBlocker(self.comboBox):
            index = self.comboBox.findData(value)
            if index < 0:
                self.comboBox.setPlaceholderText('旧服务已隐藏，请选择默认服务')
                self.setContent('当前保存的服务已隐藏；配置仍保留，请选择新的默认服务。')
            else:
                self.setContent('与 TTS 设置同步保存；调整用于下一次试听和后续弹幕。')
            self.comboBox.setCurrentIndex(index)
