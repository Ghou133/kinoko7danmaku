"""Editable audition controls sharing the regular TTS configuration."""

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    ComboBoxSettingCard, SettingCard, SettingCardGroup, SpinBox,
    SwitchSettingCard, TogglePushButton, qconfig,
)
from qfluentwidgets import FluentIcon as FIF

from core.qconfig import cfg
from models.service import ServiceType
from gui.components.float_range_setting_card import FloatRangeSettingCard
from gui.components.str_setting_card import StrSettingCard
from gui.components.sovits_cards import SovitsModelCard, SovitsReferenceCard
from gui.components.tts_service_card import TTSServiceCard
from gui.components.fish_audio_cards import fish_audio_group
from gui.components.dobao_voice_card import DoBaoVoiceCard


class StepSettingCard(SettingCard):
    def __init__(self, item, title, content, minimum, maximum, parent=None):
        super().__init__(FIF.SPEED_OFF, title, content, parent)
        self.configItem = item
        self.spinBox = SpinBox(self)
        self.spinBox.setRange(minimum, maximum)
        self.spinBox.setFixedWidth(140)
        self.spinBox.setKeyboardTracking(False)
        self.hBoxLayout.addWidget(self.spinBox)
        self.hBoxLayout.addSpacing(16)
        self.sync(item.value)
        self.spinBox.valueChanged.connect(self.change)
        item.valueChanged.connect(self.sync)

    def change(self, value):
        qconfig.set(self.configItem, value)

    def sync(self, value):
        with QSignalBlocker(self.spinBox):
            self.spinBox.setValue(value)


class TTSQuickSettings(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(12)
        self.serviceCard = TTSServiceCard(self)
        self.layout_.addWidget(self.serviceCard)
        self.groups, self.advanced, self.cards = {}, {}, {}
        self._build_gpt()
        self._build_dots()
        self._build_fish_audio()
        self._build_dobao()
        for group in self.groups.values():
            group.setFixedHeight(group.height())
            self.layout_.addWidget(group)
        self.moreButton = TogglePushButton('更多参数', self)
        self.layout_.addWidget(self.moreButton, 0, Qt.AlignLeft)
        for group in self.advanced.values():
            group.setFixedHeight(group.height())
            self.layout_.addWidget(group)
        self.moreButton.toggled.connect(self.show_service)
        cfg.activeTTS.valueChanged.connect(self.show_service)
        self.show_service()

    def group(self, service, title, advanced=False):
        group = SettingCardGroup(title, self)
        (self.advanced if advanced else self.groups)[service] = group
        return group

    def add(self, group, name, card):
        group.addSettingCard(card)
        if name:
            self.cards[name] = card
        return card

    def slider(self, group, name, title, content=None, step=0.1, decimals=1):
        return self.add(group, name, FloatRangeSettingCard(
            getattr(cfg, name), FIF.SPEED_OFF, title, content,
            step=step, decimals=decimals, parent=group,
        ))

    def switch(self, group, name, title, content=None):
        return self.add(group, name, SwitchSettingCard(
            FIF.PLAY, title, content, configItem=getattr(cfg, name), parent=group,
        ))

    def text(self, group, name, title, content=None):
        return self.add(group, name, StrSettingCard(
            getattr(cfg, name), FIF.EDIT, title, content, parent=group,
        ))

    def combo(self, group, name, title, content=None):
        item = getattr(cfg, name)
        return self.add(group, name, ComboBoxSettingCard(
            item, FIF.MUSIC, title, content, texts=list(item.options), parent=group,
        ))

    def steps(self, group, name, title, content, minimum=1, maximum=64):
        return self.add(group, name, StepSettingCard(
            getattr(cfg, name), title, content, minimum, maximum, group,
        ))

    def _build_gpt(self):
        group = self.group(ServiceType.GPT_SOVITS, 'GPT-SoVITS 试听设置')
        self.modelCard = self.add(group, 'gptModel', SovitsModelCard(group))
        self.modelCard.setTitle('切换角色模型')
        self.slider(group, 'gptSovitsSpeedFactor', '语速', '1.0 为原速')
        self.steps(group, 'gptSovitsSampleSteps', '推理步数', 'V3 / V4 使用；步数增加不保证音质更好。', maximum=128)
        self.switch(group, 'gptSovitsStreaming', '流式播放', '生成一段就播放一段')
        extra = self.group(ServiceType.GPT_SOVITS, 'GPT-SoVITS 更多参数', True)
        self.add(extra, 'gptSovitsRefAudioPath', SovitsReferenceCard(
            configItem=cfg.gptSovitsRefAudioPath, icon=FIF.MUSIC,
            title='参考音频', content='按角色记忆，切换模型时自动恢复', parent=extra,
        ))
        self.text(extra, 'gptSovitsRefText', '参考文本')
        self.combo(extra, 'gptSovitsRefTextLang', '参考音频语言')
        self.combo(extra, 'gptSovitsTextLang', '生成语言')
        self.steps(extra, 'gptSovitsTopK', 'Top K', '采样候选数量，数值更大不代表音质更好。', maximum=100)
        self.slider(extra, 'gptSovitsTopP', 'Top P', step=0.05, decimals=2)
        self.slider(extra, 'gptSovitsTemperature', '采样温度')
        self.combo(extra, 'gptSovitsTextSplitMethod', '文本切分')
        self.slider(extra, 'gptSovitsPauseSeconds', '句间停顿（秒）')
        self.switch(extra, 'gptSovitsRefTextFree', '无参考文本模式')
        self.switch(extra, 'gptSovitsSuperSampling', '超采样', '本地整合包仅 V3 生效，V4 不使用此项。')

    def _build_dots(self):
        group = self.group(ServiceType.DOTS, 'dots.tts 试听设置')
        self.add(group, None, SettingCard(
            FIF.MUSIC, '固定模型', '沿用 dots 服务启动时加载的模型；此处不重新加载模型。', group,
        ))
        self.slider(group, 'dotsSpeed', '语速', '1.0 为原速，变速不变调')
        self.steps(group, 'dotsNumSteps', '推理步数', '0 = 沿用服务设置；可设置 1–64。', minimum=0)
        self.switch(group, 'dotsStreaming', '流式播放', '生成一段就播放一段')
        extra = self.group(ServiceType.DOTS, 'dots.tts 更多参数', True)
        self.slider(extra, 'dotsVolume', '音量')
        self.text(extra, 'dotsVoice', '参考音色', '沿用 TTS 设置中的参考音频规则')
        self.text(extra, 'dotsPromptText', '参考文本', '留空时沿用音色对应的参考文本')
        self.text(extra, 'dotsLanguage', '语言', '留空沿用服务设置；例如 zh、en')
        self.switch(extra, 'dotsNormalizeText', '数字与符号读法规整')

    def _build_fish_audio(self):
        for advanced in (False, True):
            group, cards = fish_audio_group(self, advanced=advanced)
            (self.advanced if advanced else self.groups)[ServiceType.FISH_AUDIO] = group
            self.cards.update(cards)

    def _build_dobao(self):
        group = self.group(ServiceType.DOBAO, 'Doubao 试听设置')
        self.add(group, 'dobaoVoice', DoBaoVoiceCard(group))
        self.slider(group, 'dobaoSpeed', '语速', '0.5–2.0 倍；1.0 为原速')

    def show_service(self, *_):
        active = cfg.activeTTS.value
        for service, group in self.groups.items():
            group.setVisible(service == active)
        self.moreButton.setVisible(active in self.advanced)
        self.moreButton.setText('收起更多参数' if self.moreButton.isChecked() else '更多参数')
        for service, group in self.advanced.items():
            group.setVisible(service == active and self.moreButton.isChecked())
        self.layout_.invalidate()
        self.updateGeometry()
