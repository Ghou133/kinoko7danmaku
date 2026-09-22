"""Named Fish Audio voice presets, shared between settings and auditions."""

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout
from qasync import asyncSlot
from qfluentwidgets import (
    BodyLabel, ComboBox, LineEdit, MessageBoxBase, PushButton,
    SettingCard, SubtitleLabel, qconfig,
)
from qfluentwidgets import FluentIcon as FIF

from core.fish_voices import fetch_voice_name, normalize_voice_id
from core.qconfig import cfg


class FishVoiceDialog(MessageBoxBase):
    def __init__(self, voice_id='', parent=None):
        super().__init__(parent)
        self.original_id = voice_id
        self._closed = False
        self.widget.setMinimumWidth(680)
        self.viewLayout.addWidget(SubtitleLabel('保存 fish.audio 音色', self))
        fields = QHBoxLayout()
        self.idEdit = LineEdit(self)
        self.idEdit.setPlaceholderText('粘贴音色 ID 或 fish.audio 音色链接')
        self.idEdit.setText(voice_id)
        self.nameEdit = LineEdit(self)
        self.nameEdit.setPlaceholderText('自定义名称，或点击自动获取')
        self.nameEdit.setText(cfg.fishAudioVoices.value.get(voice_id, ''))
        for title, edit in (('音色 ID / 链接', self.idEdit), ('音色名称', self.nameEdit)):
            column = QVBoxLayout()
            column.addWidget(BodyLabel(title, self))
            column.addWidget(edit)
            fields.addLayout(column, 1)
        self.viewLayout.addLayout(fields)
        actions = QHBoxLayout()
        self.fetchButton = PushButton('自动获取名称', self)
        self.removeButton = PushButton('从列表移除', self)
        self.removeButton.setVisible(voice_id in cfg.fishAudioVoices.value)
        actions.addWidget(self.fetchButton)
        actions.addWidget(self.removeButton)
        actions.addStretch()
        self.viewLayout.addLayout(actions)
        self.status = BodyLabel('相同 ID 会更新名称；名称留空时，保存会自动获取。', self)
        self.status.setWordWrap(True)
        self.viewLayout.addWidget(self.status)
        self.yesButton.setText('保存并使用')
        self.cancelButton.setText('取消')
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self.save)
        self.fetchButton.clicked.connect(self.fetch_name)
        self.removeButton.clicked.connect(self.remove)
    def done(self, code):
        # Block a pending lookup before the dialog's fade-out animation completes.
        self._closed = True
        super().done(code)

    async def _lookup(self):
        raw_id, old_name = self.idEdit.text(), self.nameEdit.text()
        try:
            voice_id = normalize_voice_id(raw_id)
        except ValueError as error:
            self.status.setText(str(error))
            return None
        self.fetchButton.setEnabled(False)
        self.yesButton.setEnabled(False)
        self.status.setText('正在获取名称…')
        try:
            name = await fetch_voice_name(voice_id, cfg.fishAudioApiKey.value)
            if self._closed:
                return None
            if raw_id != self.idEdit.text() or old_name != self.nameEdit.text():
                self.status.setText('输入已变化，未覆盖当前内容；请重新获取或手动保存。')
                return None
            self.idEdit.setText(voice_id)
            self.nameEdit.setText(name)
            self.status.setText('已获取名称，可自行修改后保存。')
            return voice_id
        except ValueError as error:
            if not self._closed:
                self.status.setText(f'{error}；也可以手动填写名称后保存。')
            return None
        finally:
            if not self._closed:
                self.fetchButton.setEnabled(True)
                self.yesButton.setEnabled(True)

    @asyncSlot()
    async def fetch_name(self):
        await self._lookup()

    @asyncSlot()
    async def save(self):
        if self._closed:
            return
        try:
            voice_id = normalize_voice_id(self.idEdit.text())
            if not self.nameEdit.text().strip():
                if await self._lookup() is None:
                    return
            if self._closed:
                return
            # The fetch only completes if the fields still describe the same voice.
            voice_id = normalize_voice_id(self.idEdit.text())
            name = self.nameEdit.text().strip()
            voices = dict(cfg.fishAudioVoices.value)
            voices[voice_id] = name
            qconfig.set(cfg.fishAudioVoices, voices)
            qconfig.set(cfg.fishAudioReferenceId, voice_id)
            self.accept()
        except ValueError as error:
            self.status.setText(str(error))

    def remove(self):
        voices = dict(cfg.fishAudioVoices.value)
        voices.pop(self.original_id, None)
        qconfig.set(cfg.fishAudioVoices, voices)
        if cfg.fishAudioReferenceId.value == self.original_id:
            qconfig.set(cfg.fishAudioReferenceId, '')
        self.accept()


class FishVoiceCard(SettingCard):
    def __init__(self, parent=None):
        super().__init__(FIF.MUSIC, '切换音色', '保存音色名称后，可直接从列表切换', parent)
        self.comboBox = ComboBox(self)
        self.comboBox.setMinimumWidth(260)
        self.comboBox.setMaxVisibleItems(10)
        self.addButton = PushButton('添加', self)
        self.editButton = PushButton('编辑', self)
        self.hBoxLayout.addWidget(self.comboBox)
        self.hBoxLayout.addWidget(self.addButton)
        self.hBoxLayout.addWidget(self.editButton)
        self.hBoxLayout.addSpacing(16)
        self.comboBox.currentIndexChanged.connect(self.select)
        self.addButton.clicked.connect(self.add_voice)
        self.editButton.clicked.connect(self.edit_voice)
        cfg.fishAudioVoices.valueChanged.connect(self.reload)
        cfg.fishAudioReferenceId.valueChanged.connect(self.reload)
        self.dialog = None
        self.reload()

    def reload(self, *_):
        selected = cfg.fishAudioReferenceId.value
        voices = dict(cfg.fishAudioVoices.value)
        if selected and selected not in voices:
            voices[selected] = f'音色 {selected[:8]}（未命名）'
        with QSignalBlocker(self.comboBox):
            self.comboBox.clear()
            self.comboBox.addItem('请选择音色', userData='')
            for voice_id, name in voices.items():
                # Same-name voices stay distinguishable without displaying long IDs.
                self.comboBox.addItem(f'{name} · {voice_id[:8]}', userData=voice_id)
            self.comboBox.setCurrentIndex(self.comboBox.findData(selected))
        self.editButton.setEnabled(bool(selected))
        self.comboBox.setToolTip(selected)

    def select(self, index):
        if index >= 0:
            qconfig.set(cfg.fishAudioReferenceId, self.comboBox.itemData(index))

    def add_voice(self):
        self.open_editor('')

    def edit_voice(self):
        self.open_editor(cfg.fishAudioReferenceId.value)

    def open_editor(self, voice_id):
        if self.dialog is not None:
            self.dialog.deleteLater()
        self.dialog = FishVoiceDialog(voice_id, self.window())
        self.dialog.open()
