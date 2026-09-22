"""Audition text with editable, service-specific TTS controls."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from loguru import logger
from qasync import asyncSlot
from qfluentwidgets import (
    BodyLabel, CardWidget, InfoBar, InfoBarPosition, LineEdit,
    PrimaryPushButton, ScrollArea, TextEdit, TitleLabel,
)
from qfluentwidgets import FluentIcon as FIF

from core.const import SUPPORTED_SERVICES
from core.qconfig import cfg
from core.speech import speak_template
from core.user_voices import user_voice_binding
from gui.components.tts_quick_settings import TTSQuickSettings


class AudioTestInterface(ScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.scrollWidget = QWidget()
        self.scrollWidget.setObjectName('scrollWidget')
        layout = QVBoxLayout(self.scrollWidget)
        layout.setContentsMargins(36, 24, 36, 24)
        layout.setSpacing(18)
        layout.setAlignment(Qt.AlignTop)
        self.title_label = TitleLabel('音频测试', self.scrollWidget)
        layout.addWidget(self.title_label)

        self.test_card = CardWidget(self.scrollWidget)
        self.test_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        card_layout = QVBoxLayout(self.test_card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(12)
        self.user_name_edit = LineEdit(self.test_card)
        self.user_name_edit.setPlaceholderText('可选：原始用户名（测试角色映射、发音字典和弹幕模板）')
        card_layout.addWidget(self.user_name_edit)
        self.text_edit = TextEdit(self.test_card)
        self.text_edit.setPlaceholderText('请输入试听文本，修改下方参数后点击试听…')
        self.text_edit.setFixedHeight(130)
        card_layout.addWidget(self.text_edit)
        self.route_hint = BodyLabel(self.test_card)
        self.route_hint.setWordWrap(True)
        card_layout.addWidget(self.route_hint)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.test_button = PrimaryPushButton(FIF.PLAY, '试听', self.test_card)
        buttons.addWidget(self.test_button)
        card_layout.addLayout(buttons)
        layout.addWidget(self.test_card)

        self.quick_settings = TTSQuickSettings(self.scrollWidget)
        layout.addWidget(self.quick_settings)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.test_button.clicked.connect(self._on_test_button_clicked)
        self.user_name_edit.textChanged.connect(self._refresh_route_hint)
        for item in (cfg.activeTTS, cfg.gptSovitsUserModels,
                     cfg.gptSovitsUserModelsEnabled, cfg.fishAudioVoices):
            item.valueChanged.connect(self._refresh_route_hint)
        self._refresh_route_hint()

    def _refresh_route_hint(self, *_):
        username = self.user_name_edit.text().strip()
        model = user_voice_binding(username)
        if model is not None:
            service_type = model['service']
            service = SUPPORTED_SERVICES[service_type].description
            label = model.get('label') or '该用户绑定的角色'
            if service_type == 'fish_audio':
                label = cfg.fishAudioVoices.value.get(model.get('reference_id'), label)
            elif service_type == 'dots_tts':
                label = '服务当前固定模型'
            availability = ('本地服务未就绪时临时使用默认服务。'
                            if service_type in ('gpt_sovits', 'dots_tts') else '')
            self.route_hint.setText(
                f'本次按用户名映射使用 {service} · {label}。{availability}'
                '清空用户名即可试听下方选择的服务和模型。'
            )
        else:
            service = SUPPORTED_SERVICES[cfg.activeTTS.value].description
            self.route_hint.setText(f'本次使用 {service} 和下方所选模型；修改参数后再次点击试听。')

    @asyncSlot()
    async def _on_test_button_clicked(self) -> None:
        text = self.text_edit.toPlainText()
        if not text.strip():
            return
        try:
            self.test_button.setEnabled(False)
            user_name = self.user_name_edit.text().strip()
            template = cfg.danmakuOnText.value if user_name else '{message}'
            notice = await speak_template(template, user_name=user_name, message=text)
            if notice:
                self.route_hint.setText(f'本次试听：{notice}')
                InfoBar.warning(
                    title='已临时使用默认服务', content=notice, orient=Qt.Horizontal,
                    isClosable=True, position=InfoBarPosition.TOP, duration=6000, parent=self,
                )
        except Exception as exc:
            logger.exception(f'音频测试失败: {exc}')
            InfoBar.error(
                title='测试失败', content=str(exc), orient=Qt.Horizontal,
                isClosable=True, position=InfoBarPosition.TOP, duration=3000, parent=self,
            )
        finally:
            self.test_button.setEnabled(True)
