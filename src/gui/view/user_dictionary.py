"""User dictionaries and per-user TTS services and voices."""

import asyncio

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy
from qasync import asyncSlot
from qfluentwidgets import (
    BodyLabel, ComboBox, LineEdit, PushButton, ScrollArea,
    SettingCardGroup, SwitchButton, SwitchSettingCard, TitleLabel, qconfig,
)
from qfluentwidgets import FluentIcon as FIF

from core.qconfig import cfg
from core.fish_voices import normalize_voice_id
from core.dobao_voices import normalize_dobao_voice, dobao_voice_name
from core.sovits_models import scan_models
from core.tts_availability import check_service_availability
from core.user_voices import model_reference
from models.service import ServiceType
from gui.components.dobao_voice_card import populate_dobao_voice_combo
from shiboken6 import isValid


_SERVICES = {
    ServiceType.GPT_SOVITS: 'GPT-SoVITS',
    ServiceType.DOTS: 'dots',
    ServiceType.FISH_AUDIO: 'Fish Audio',
    ServiceType.DOBAO: 'Doubao',
}


class UserModelsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(TitleLabel('用户名 → TTS 服务 / 音色', self))
        self.hint = BodyLabel('', self)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.modelsEnabledCard = SwitchSettingCard(
            icon=FIF.PEOPLE,
            title='启用用户名 → TTS 服务 / 音色',
            content='关闭后使用默认服务和音色，保留所有绑定。',
            configItem=cfg.gptSovitsUserModelsEnabled,
            parent=self,
        )
        layout.addWidget(self.modelsEnabledCard)
        self.form = QWidget(self)
        form = QVBoxLayout(self.form)
        form.setContentsMargins(0, 0, 0, 0)
        identity = QHBoxLayout()
        self.username = LineEdit(self)
        self.username.setPlaceholderText('原始用户名（区分大小写）')
        self.service = ComboBox(self)
        self.service.setPlaceholderText('请选择新的 TTS 服务')
        self.service.setMinimumWidth(160)
        for kind, name in _SERVICES.items():
            self.service.addItem(name, userData=kind.value)
        identity.addWidget(self.username, 1)
        identity.addWidget(self.service)
        form.addLayout(identity)
        voice_row = QHBoxLayout()
        self.models = ComboBox(self)
        self.models.setMaxVisibleItems(10)
        self.models.setMinimumWidth(120)
        self.models.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.fishVoices = ComboBox(self)
        self.fishVoices.setMaxVisibleItems(10)
        self.fishVoices.setMinimumWidth(120)
        self.fishVoices.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.dobaoVoices = ComboBox(self)
        self.dobaoVoices.setPlaceholderText('请选择 Doubao 音色')
        self.dobaoVoices.setMaxVisibleItems(12)
        self.dobaoVoices.setMinimumWidth(120)
        self.dobaoVoices.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        populate_dobao_voice_combo(self.dobaoVoices, cfg.dobaoVoice.value)
        self.dotsModel = BodyLabel('使用 dots 当前已加载的固定模型，无需另外选择。', self)
        self.dotsModel.setWordWrap(True)
        self.bind = PushButton('保存 / 更新', self)
        self.refresh = PushButton('刷新列表', self)
        for widget in (self.models, self.fishVoices, self.dobaoVoices, self.dotsModel):
            voice_row.addWidget(widget, 1)
        for widget in (self.bind, self.refresh):
            voice_row.addWidget(widget)
        form.addLayout(voice_row)
        self.selectionHint = BodyLabel('', self)
        self.selectionHint.setWordWrap(True)
        form.addWidget(self.selectionHint)
        layout.addWidget(self.form)
        availability_row = QHBoxLayout()
        self.checkButton = PushButton('检测本地服务', self)
        self.availability = BodyLabel('尚未检测本地服务；Doubao 需要本地 API 在线并登录。', self)
        self.availability.setWordWrap(True)
        availability_row.addWidget(self.checkButton)
        availability_row.addWidget(self.availability, 1)
        layout.addLayout(availability_row)
        self.status = BodyLabel('', self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.rows = QVBoxLayout()
        layout.addLayout(self.rows)
        self.pairs = []
        self._checking = False
        self._probe_generation = 0
        self.bind.clicked.connect(self.save)
        self.refresh.clicked.connect(self.reload_lists)
        self.checkButton.clicked.connect(self.check_services)
        self.service.currentIndexChanged.connect(self.service_changed)
        cfg.gptSovitsFolder.valueChanged.connect(self.reload_models)
        cfg.fishAudioVoices.valueChanged.connect(self.reload_fish_voices)
        cfg.fishAudioVoices.valueChanged.connect(self.reload_rows)
        cfg.activeTTS.valueChanged.connect(self.engine_changed)
        cfg.gptSovitsUserModelsEnabled.valueChanged.connect(self.engine_changed)
        self.reload_lists()
        self.reload_rows()
        self.engine_changed()
        self.service_changed()

    def engine_changed(self, *_):
        default = _SERVICES.get(cfg.activeTTS.value, '已隐藏的服务')
        hint = f'当前默认服务：{default}。按原始用户名精确匹配，未绑定或独立开关关闭的用户使用默认服务和音色。'
        if not cfg.gptSovitsUserModelsEnabled.value:
            hint = f'总开关已关闭，所有用户使用默认服务 {default} 和音色；可继续编辑，已保存的绑定和独立开关会保留。'
        self.hint.setText(hint + ' 指定的本地服务离线时临时使用默认服务并提示；已排队弹幕保留入队时的设置。')

    def service_changed(self, *_):
        kind = self.service.currentData()
        self.status.clear()
        self.models.setVisible(kind == ServiceType.GPT_SOVITS)
        self.fishVoices.setVisible(kind == ServiceType.FISH_AUDIO)
        self.dobaoVoices.setVisible(kind == ServiceType.DOBAO)
        self.dotsModel.setVisible(kind == ServiceType.DOTS)
        self.refresh.setVisible(kind in (ServiceType.GPT_SOVITS, ServiceType.FISH_AUDIO))
        hints = {
            ServiceType.GPT_SOVITS: '先在 TTS 设置中保存该模型的参考音频与文本；服务未启动也可提前保存绑定。',
            ServiceType.FISH_AUDIO: '从已保存的音色中选择；可在 TTS 设置或音频测试中添加音色。',
            ServiceType.DOBAO: '为此用户名保存任意 Doubao 音色；在 TTS 设置中启动 / 检查 API 并登录，语速沿用全局设置。',
            ServiceType.DOTS: '使用 TTS 设置中的 dots 参数；此绑定不会切换 dots 模型。',
        }
        self.selectionHint.setText(hints.get(kind, '请选择 TTS 服务。'))

    def reload_lists(self, *_):
        self.reload_models()
        self.reload_fish_voices()

    def reload_models(self, *_):
        old_index = self.models.currentIndex()
        old_pair = self.pairs[old_index] if 0 <= old_index < len(self.pairs) else None
        try:
            self.pairs, _ = scan_models(cfg.gptSovitsFolder.value)
        except (OSError, ValueError) as exc:
            self.pairs = []
            self.status.setText(str(exc))
        with QSignalBlocker(self.models):
            self.models.clear()
            self.models.addItems([p.label for p in self.pairs])
            if old_pair:
                index = next((i for i, p in enumerate(self.pairs)
                              if p.gpt == old_pair.gpt and p.sovits == old_pair.sovits), -1)
                self.models.setCurrentIndex(index)

    def reload_fish_voices(self, *_):
        selected = self.fishVoices.currentData()
        with QSignalBlocker(self.fishVoices):
            self.fishVoices.clear()
            self.fishVoices.addItem('请选择已保存的音色', userData='')
            for voice_id, label in cfg.fishAudioVoices.value.items():
                try:
                    normalized = normalize_voice_id(voice_id)
                except ValueError:
                    continue
                if self.fishVoices.findData(normalized) < 0:
                    self.fishVoices.addItem(f'{label} · {normalized[:8]}', userData=normalized)
            self.fishVoices.setCurrentIndex(max(0, self.fishVoices.findData(selected)))

    @asyncSlot()
    async def check_services(self):
        if self._checking:
            return
        self._checking = True
        generation = self._probe_generation
        endpoints = (
            (ServiceType.DOTS, cfg.dotsApiUrl.value),
            (ServiceType.GPT_SOVITS, cfg.gptSovitsApiUrl.value),
            (ServiceType.DOBAO, cfg.dobaoApiUrl.value),
        )
        self.checkButton.setEnabled(False)
        self.availability.setText('正在检测 dots、GPT-SoVITS 和 Doubao…')
        try:
            results = await asyncio.gather(*(
                check_service_availability(kind, url, force=True) for kind, url in endpoints
            ), return_exceptions=True)
            if not isValid(self) or generation != self._probe_generation:
                return
            summaries = []
            current_urls = {
                ServiceType.DOTS: cfg.dotsApiUrl.value,
                ServiceType.GPT_SOVITS: cfg.gptSovitsApiUrl.value,
                ServiceType.DOBAO: cfg.dobaoApiUrl.value,
            }
            for (kind, url), result in zip(endpoints, results):
                if url != current_urls[kind]:
                    summaries.append(f'{_SERVICES[kind]}：地址已修改，请重新检测')
                elif isinstance(result, BaseException):
                    summaries.append(f'{_SERVICES[kind]}：检测失败，可稍后重试')
                else:
                    state = '可用' if result.available else '不可用'
                    summaries.append(f'{_SERVICES[kind]}：{state}（{result.detail}）')
            self.availability.setText('；'.join(summaries) + '。Fish Audio：直接使用已配置 API，无需本地启动。')
        finally:
            if isValid(self):
                self._checking = False
                self.checkButton.setEnabled(True)

    def closeEvent(self, event):
        self._probe_generation += 1
        super().closeEvent(event)

    def save(self):
        name = self.username.text()
        kind = self.service.currentData()
        if not name.strip():
            self.status.setText('请填写原始用户名。')
            return
        if kind not in _SERVICES:
            self.status.setText('请选择 TTS 服务。')
            return
        models = dict(cfg.gptSovitsUserModels.value)
        previous = models.get(name, {})
        model = {'service': kind, 'enabled': previous.get('enabled', True) if isinstance(previous, dict) else True}
        if kind == ServiceType.GPT_SOVITS:
            index = self.models.currentIndex()
            if not 0 <= index < len(self.pairs):
                self.status.setText('请选择 GPT-SoVITS 角色模型。')
                return
            pair = self.pairs[index]
            ref = model_reference(pair.gpt, pair.sovits)
            if not ref.get('audio') or (not ref.get('text_free') and not ref.get('text')):
                self.status.setText('请先到 TTS 设置选择这个角色，配置参考音频与对应文本，再保存绑定。')
                return
            model.update(gpt=pair.gpt, sovits=pair.sovits, label=pair.label)
        elif kind == ServiceType.FISH_AUDIO:
            try:
                voice_id = normalize_voice_id(self.fishVoices.currentData())
            except ValueError:
                self.status.setText('请选择已保存的 Fish Audio 音色；可先到 TTS 设置或音频测试中添加。')
                return
            label = cfg.fishAudioVoices.value.get(voice_id) or f'音色 {voice_id[:8]}'
            model.update(reference_id=voice_id, label=label)
        elif kind == ServiceType.DOBAO:
            try:
                voice = normalize_dobao_voice(self.dobaoVoices.currentData())
            except ValueError:
                self.status.setText('请选择 Doubao 音色。')
                return
            model.update(voice=voice, label=dobao_voice_name(voice))
        else:
            model['label'] = '当前固定模型'
        models[name] = model
        qconfig.set(cfg.gptSovitsUserModels, models)
        self.reload_rows()
        self.status.setText(f'已保存：{name} → {_SERVICES[kind]} · {model["label"]}')

    def reload_rows(self, *_):
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.userSwitches = {}
        for name, model in cfg.gptSovitsUserModels.value.items():
            if not isinstance(model, dict):
                continue
            kind = model.get('service', ServiceType.GPT_SOVITS)
            if kind == ServiceType.SEED_TTS:
                continue  # Hide legacy controls without rewriting the saved binding.
            row = QWidget(self)
            row_layout = QHBoxLayout(row)
            title = _SERVICES.get(kind, '未知服务')
            voice = model.get('label', '角色模型')
            if kind == ServiceType.FISH_AUDIO:
                voice_id = model.get('reference_id', '')
                voice = cfg.fishAudioVoices.value.get(voice_id) or f'{voice}（音色列表中已移除，绑定保留）'
            elif kind == ServiceType.DOBAO:
                voice = dobao_voice_name(model.get('voice'))
            label = BodyLabel(f'{name} → {title} · {voice}', row)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            row_layout.addWidget(label, 1)
            enabled = SwitchButton(parent=row)
            enabled.setOnText('启用')
            enabled.setOffText('停用')
            enabled.setChecked(model.get('enabled', True))
            enabled.setToolTip('关闭后该用户使用默认服务和音色；不会删除绑定。')
            enabled.checkedChanged.connect(lambda checked, n=name: self.set_user_enabled(n, checked))
            self.userSwitches[name] = enabled
            row_layout.addWidget(enabled)
            edit = PushButton('编辑', row)
            remove = PushButton('删除', row)
            edit.clicked.connect(lambda checked=False, n=name: self.edit(n))
            remove.clicked.connect(lambda checked=False, n=name: self.remove(n))
            row_layout.addWidget(edit)
            row_layout.addWidget(remove)
            self.rows.addWidget(row)

    def set_user_enabled(self, name, enabled):
        models = dict(cfg.gptSovitsUserModels.value)
        if name not in models:
            return
        model = dict(models[name])
        model['enabled'] = enabled
        models[name] = model
        qconfig.set(cfg.gptSovitsUserModels, models)

    def edit(self, name):
        self.username.setText(name)
        model = cfg.gptSovitsUserModels.value[name]
        kind = model.get('service', ServiceType.GPT_SOVITS)
        self.service.setCurrentIndex(self.service.findData(kind))
        self.service_changed()
        if kind not in _SERVICES:
            self.status.setText('这个旧服务已隐藏，原绑定仍保留；选择新的服务后保存即可更换。')
            return
        if kind == ServiceType.GPT_SOVITS:
            index = next((i for i, p in enumerate(self.pairs)
                          if p.gpt == model.get('gpt') and p.sovits == model.get('sovits')), -1)
            self.models.setCurrentIndex(index)
            if index < 0:
                self.status.setText('该角色的模型文件不在当前安装目录中，请重新选择。')
        elif kind == ServiceType.FISH_AUDIO:
            self.reload_fish_voices()
            index = self.fishVoices.findData(model.get('reference_id'))
            self.fishVoices.setCurrentIndex(max(0, index))
            if index < 0:
                self.status.setText('该音色不在已保存的音色列表中，请先添加或重新选择。')
        elif kind == ServiceType.DOBAO:
            populate_dobao_voice_combo(self.dobaoVoices, model.get('voice'))
            if self.dobaoVoices.currentIndex() < 0:
                self.status.setText('该绑定尚未选择音色，请重新选择后保存。')

    def remove(self, name):
        models = dict(cfg.gptSovitsUserModels.value)
        models.pop(name, None)
        qconfig.set(cfg.gptSovitsUserModels, models)
        self.reload_rows()


class UserDictionaryInterface(QWidget):
    def __init__(self, settings):
        super().__init__(settings)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 30, 36, 20)
        layout.addWidget(TitleLabel('用户字典', self))
        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet('QScrollArea { border: none; background: transparent; }')
        self.body = QWidget()
        self.body.setObjectName('userDictionaryBody')
        self.body.setStyleSheet('#userDictionaryBody { background: transparent; }')
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setAlignment(Qt.AlignTop)
        body_layout.setSpacing(24)
        self.models = UserModelsWidget(self.body)
        body_layout.addWidget(self.models)
        self.dictionaries = SettingCardGroup('发音与关键词字典', self.body)
        for card in (settings.aliasDictCard, settings.messageAliasDictCard, settings.audioClipDictCard):
            card.setParent(self.dictionaries)
            self.dictionaries.addSettingCard(card)
        body_layout.addWidget(self.dictionaries)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
