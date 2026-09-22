"""Hosted Fish Audio controls shared by settings and the audition page."""

from PySide6.QtWidgets import QLineEdit
from qfluentwidgets import ComboBoxSettingCard, SettingCardGroup, SwitchSettingCard
from qfluentwidgets import FluentIcon as FIF

from core.qconfig import cfg
from gui.components.float_range_setting_card import FloatRangeSettingCard
from gui.components.str_setting_card import StrSettingCard
from gui.components.fish_voice_card import FishVoiceCard


def fish_audio_group(parent=None, *, advanced=False, credentials=False):
    group = SettingCardGroup('fish.audio 更多参数' if advanced else 'fish.audio 设置', parent)
    cards = {}

    def add(name, card):
        cards[name] = card
        group.addSettingCard(card)
        return card

    def combo(name, title, content=None, texts=None):
        item = getattr(cfg, name)
        return add(name, ComboBoxSettingCard(
            item, FIF.MUSIC, title, content, texts=texts or list(item.options), parent=group,
        ))

    def slider(name, title, content=None, step=0.1, decimals=1):
        return add(name, FloatRangeSettingCard(
            getattr(cfg, name), FIF.SPEED_OFF, title, content,
            step=step, decimals=decimals, parent=group,
        ))

    if credentials:
        key = add('fishAudioApiKey', StrSettingCard(
            cfg.fishAudioApiKey, FIF.LINK, 'API 密钥', '仅用于连接 fish.audio 官方服务', parent=group,
        ))
        key.lineEdit.setEchoMode(QLineEdit.Password)
    if not advanced:
        combo('fishAudioModel', '生成模型', '选择 fish.audio 的推理模型版本')
        add('fishAudioReferenceId', FishVoiceCard(group))
        slider('fishAudioSpeed', '语速', '1.0 为原速')
        add('fishAudioStreaming', SwitchSettingCard(
            FIF.PLAY, '流式播放', '收到音频片段就开始播放', configItem=cfg.fishAudioStreaming, parent=group,
        ))
    if advanced or credentials:
        combo('fishAudioLatency', '延迟模式', '可在响应速度与音质之间调整',
              texts=['标准', '均衡', '低延迟'])
        slider('fishAudioVolume', '音量（dB）', '0 为原音量', step=1, decimals=0)
        slider('fishAudioTemperature', '采样温度', step=0.05, decimals=2)
        slider('fishAudioTopP', 'Top P', step=0.05, decimals=2)
    return group, cards
