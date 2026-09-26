"""Optional Doubao API lifecycle controls for the ordinary settings page."""

import httpx
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog
from qasync import asyncSlot
from qfluentwidgets import FluentIcon as FIF, PushButton, SettingCard, qconfig

from core.dobao_launcher import DoBaoApiManager, installation_folder, local_api_url
from core.dobao_voices import detect_dobao_folder
from core.qconfig import cfg


class DoBaoFolderCard(SettingCard):
    def __init__(self, configItem=None, parent=None):
        self.configItem = configItem or cfg.dobaoFolder
        super().__init__(FIF.FOLDER, 'Doubao 安装文件夹', '', parent)
        self.button = PushButton('选择文件夹', self)
        self.hBoxLayout.addWidget(self.button)
        self.hBoxLayout.addSpacing(16)
        self.button.clicked.connect(self.choose)
        self.configItem.valueChanged.connect(self.refresh)
        self.refresh()

    def refresh(self, *_):
        self.setContent(self.configItem.value or detect_dobao_folder() or '选择包含 local-api 的 DoBao-TTS-Win 文件夹')

    def choose(self):
        folder = QFileDialog.getExistingDirectory(self, '选择 Doubao 安装文件夹', self.configItem.value or detect_dobao_folder())
        if not folder:
            return
        try:
            root = installation_folder(folder)
            qconfig.set(self.configItem, str(root))
        except (OSError, ValueError) as exc:
            self.setContent(str(exc))


class DoBaoApiCard(SettingCard):
    def __init__(self, parent=None):
        super().__init__(FIF.PLAY, '本地 API 服务', '按需启动 Doubao；默认 http://127.0.0.1:9882', parent)
        self.manager = DoBaoApiManager()
        self.button = PushButton('启动 / 检查 API', self)
        self.loginButton = PushButton('登录 / 状态', self)
        self.hBoxLayout.addWidget(self.button)
        self.hBoxLayout.addWidget(self.loginButton)
        self.hBoxLayout.addSpacing(16)
        self.button.clicked.connect(self.start)
        self.loginButton.clicked.connect(self.open_login)
        cfg.dobaoApiUrl.valueChanged.connect(self.address_changed)

    def address_changed(self, *_):
        self.setContent('API 地址已更改；点击“启动 / 检查 API”确认状态')

    @asyncSlot()
    async def start(self):
        requested_url = cfg.dobaoApiUrl.value
        self.button.setEnabled(False)
        self.setContent('正在检查本地 API，未运行时自动启动…')
        try:
            state = await self.manager.start_or_check(requested_url, cfg.dobaoFolder.value)
            if cfg.dobaoApiUrl.value == requested_url:
                self.setContent(state.detail)
        except httpx.HTTPError:
            self.setContent('启动 / 检查失败：服务未返回有效响应，请检查 API 地址')
        except (ValueError, OSError) as exc:
            self.setContent(f'启动 / 检查失败：{exc}')
        finally:
            if cfg.dobaoApiUrl.value != requested_url:
                self.address_changed()
            self.button.setEnabled(True)

    def open_login(self):
        try:
            url, _ = local_api_url(cfg.dobaoApiUrl.value)
            QDesktopServices.openUrl(QUrl(url))
        except ValueError as exc:
            self.setContent(str(exc))
