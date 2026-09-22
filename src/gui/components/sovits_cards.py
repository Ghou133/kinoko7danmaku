"""Local GPT-SoVITS installation and paired voice selection."""

import os
import subprocess
import shutil
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QFileDialog
from qasync import asyncSlot
from qfluentwidgets import ComboBox, FluentIcon as FIF, PushButton, SettingCard, qconfig

from core.qconfig import cfg
from core.const import RESOURCE_DIR
from core.sovits_models import scan_models
from core.sovits_references import reference_memory
from gui.components.str_setting_card import StrSettingCard


class SovitsReferenceCard(StrSettingCard):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        button = PushButton('浏览', self)
        self.hBoxLayout.insertWidget(self.hBoxLayout.count() - 1, button)
        button.clicked.connect(self.choose)

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择当前角色参考音频',
                                             cfg.gptSovitsRefAudioPath.value,
                                             '音频 (*.wav *.mp3 *.flac *.ogg *.m4a);;所有文件 (*)')
        if path:
            self.setValue(path)


class SovitsFolderCard(SettingCard):
    def __init__(self, parent=None):
        super().__init__(FIF.FOLDER, 'GPT-SoVITS 安装文件夹', cfg.gptSovitsFolder.value or '选择包含 api_v2.py 的文件夹', parent)
        button = PushButton('选择文件夹', self)
        self.hBoxLayout.addWidget(button)
        self.hBoxLayout.addSpacing(16)
        button.clicked.connect(self.choose)

    def choose(self):
        folder = QFileDialog.getExistingDirectory(self, '选择 GPT-SoVITS 文件夹', cfg.gptSovitsFolder.value)
        if folder:
            try:
                scan_models(folder)
            except ValueError as exc:
                self.setContent(str(exc))
                return
            if cfg.gptSovitsApiUrl.value.rstrip('/') in ('http://localhost:19874', 'http://127.0.0.1:19874'):
                qconfig.set(cfg.gptSovitsApiUrl, 'http://127.0.0.1:9880')
            qconfig.set(cfg.gptSovitsFolder, folder)
            self.setContent(folder)


class SovitsModelCard(SettingCard):
    def __init__(self, parent=None):
        super().__init__(FIF.MUSIC, '角色模型', '自动配对 GPT 与 SoVITS 权重', parent)
        self.combo = ComboBox(self)
        self.combo.setMinimumWidth(310)
        self.combo.setMaxVisibleItems(10)
        self.refresh = PushButton('刷新', self)
        self.hBoxLayout.addWidget(self.combo)
        self.hBoxLayout.addWidget(self.refresh)
        self.hBoxLayout.addSpacing(16)
        self.pairs = []
        self.combo.currentIndexChanged.connect(self.select)
        self.refresh.clicked.connect(self.reload)
        cfg.gptSovitsFolder.valueChanged.connect(self.reload)
        cfg.gptSovitsGptModel.valueChanged.connect(self.syncSelection)
        cfg.gptSovitsSovitsModel.valueChanged.connect(self.syncSelection)
        self.reload()

    def syncSelection(self, *_):
        """Reflect another page's selection without selecting a voice again."""
        selected = next((i + 1 for i, p in enumerate(self.pairs)
                         if Path(p.gpt) == Path(cfg.gptSovitsGptModel.value)
                         and Path(p.sovits) == Path(cfg.gptSovitsSovitsModel.value)), 0)
        with QSignalBlocker(self.combo):
            self.combo.setCurrentIndex(selected)

    def reload(self, *_):
        self.combo.blockSignals(True)
        self.combo.clear()
        self.pairs = []
        try:
            self.pairs, issues = scan_models(cfg.gptSovitsFolder.value)
            self.combo.addItem('请选择角色模型')
            self.combo.addItems([p.label for p in self.pairs])
            self.syncSelection()
            self.setContent(f'已配对 {len(self.pairs)} 组模型' + (f'；{len(issues)} 项未配对（悬停查看）' if issues else '；选择一次即可切换两份权重'))
            self.setToolTip('\n'.join(issues))
        except (ValueError, OSError) as exc:
            self.setContent(str(exc))
        finally:
            self.combo.blockSignals(False)

    def select(self, index):
        if 0 < index <= len(self.pairs):
            pair = self.pairs[index - 1]
            reference_memory.select(pair.gpt, pair.sovits)


class SovitsApiCard(SettingCard):
    def __init__(self, parent=None):
        super().__init__(FIF.PLAY, '本地 API 服务', '默认 http://127.0.0.1:9880；首次加载模型需要一些时间', parent)
        self.process = None
        self.button = PushButton('启动 / 检查 API', self)
        self.hBoxLayout.addWidget(self.button)
        self.hBoxLayout.addSpacing(16)
        self.button.clicked.connect(self.start)

    @asyncSlot()
    async def start(self):
        self.button.setEnabled(False)
        try:
            url = cfg.gptSovitsApiUrl.value.rstrip('/')
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                try:
                    response = await client.get(url + '/openapi.json')
                    response.raise_for_status()
                    paths = response.json().get('paths', {})
                    if not all(p in paths for p in ('/tts', '/set_gpt_weights', '/set_sovits_weights')):
                        raise ValueError('这个地址不是 GPT-SoVITS api_v2 服务')
                    if '/kinoko/tts' in paths:
                        self.setContent('API 已就绪：模型常驻，同角色连续播报无需重新加载')
                    else:
                        self.setContent('API 已就绪（原版）；停止旧 API 后由此按钮启动，可启用模型常驻优化')
                    return
                except httpx.RequestError:
                    pass
            parsed = urlparse(url)
            if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path:
                raise ValueError('远程服务请在服务器上启动；本地地址示例：http://127.0.0.1:9880')
            if self.process is not None and self.process.poll() is None:
                self.setContent('服务仍在加载，请稍后再检查；日志：安装文件夹/kinoko-api.log')
                return
            root = Path(cfg.gptSovitsFolder.value)
            scan_models(str(root))
            python = root / 'runtime' / 'python.exe'
            if not python.is_file():
                raise ValueError('未找到 runtime/python.exe，请手动运行 api_v2.py')
            env = os.environ.copy()
            env['PATH'] = str(root / 'runtime') + os.pathsep + str(root) + os.pathsep + env.get('PATH', '')
            env['PYTHONIOENCODING'] = 'utf-8'
            env['NO_PROXY'] = '127.0.0.1,localhost,::1'
            config = root / 'kinoko-tts-infer.yaml'
            if not config.exists():
                shutil.copyfile(root / 'GPT_SoVITS/configs/tts_infer.yaml', config)
            launcher = root / 'kinoko_sovits_api.py'
            shutil.copyfile(RESOURCE_DIR / 'kinoko_sovits_api.py', launcher)
            with (root / 'kinoko-api.log').open('ab') as log:
                self.process = subprocess.Popen(
                    [str(python), '-u', '-s', str(launcher), '-a', '127.0.0.1', '-p', str(parsed.port or 9880), '-c', str(config)],
                    cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            self.setContent('已启动，加载完成后再检查；日志：安装文件夹/kinoko-api.log')
        except (ValueError, OSError, httpx.HTTPError) as exc:
            self.setContent(f'启动 / 检查失败：{exc}')
        finally:
            self.button.setEnabled(True)
