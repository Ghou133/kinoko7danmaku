"""Dedicated TTS settings page sharing the existing configuration cards."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout
from qfluentwidgets import ScrollArea, TitleLabel

from core.qconfig import cfg
from models.service import ServiceType


class TTSSettingsInterface(QWidget):
    def __init__(self, settings):
        super().__init__(settings)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 30, 36, 20)
        layout.setSpacing(16)
        layout.addWidget(TitleLabel('TTS 设置', self))
        # The service selector stays visible while its settings scroll.
        layout.addWidget(settings.ttsGroup)
        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet('QScrollArea { border: none; background: transparent; }')
        self.body = QWidget()
        self.body.setObjectName('ttsSettingsBody')
        self.body.setStyleSheet('#ttsSettingsBody { background: transparent; }')
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setAlignment(Qt.AlignTop)
        self.groups = {
            ServiceType.DOTS: settings.dotsGroup,
            ServiceType.GPT_SOVITS: settings.gptSovitsGroup,
            ServiceType.FISH_AUDIO: settings.fishAudioGroup,
        }
        for group in (settings.minimaxGroup, settings.fishSpeechGroup, settings.piperGroup, settings.edgeGroup):
            group.hide()
        for group in self.groups.values():
            body_layout.addWidget(group)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
        cfg.activeTTS.valueChanged.connect(self.show_service)
        self.show_service()

    def show_service(self, *_):
        for service, group in self.groups.items():
            group.setVisible(service == cfg.activeTTS.value)
        self.body.layout().invalidate()
        self.body.adjustSize()
        self.scroll.verticalScrollBar().setValue(0)
