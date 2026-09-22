import os
import sys
import tempfile

from pathlib import Path

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
_config = tempfile.TemporaryDirectory(prefix='kinoko-test-')
os.environ['KINOKO_DATA_DIR'] = _config.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import pytest


@pytest.fixture(scope='session')
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def config(app):
    from core.qconfig import cfg

    names = [
        'aliasDict',
        'messageAliasDict',
        'audioClipDict',
        'activeTTS',
        'dotsStreaming',
        'dotsApiUrl',
        'dotsVoice',
        'dotsPromptText',
        'dotsLanguage',
        'dotsNumSteps',
        'dotsSpeed',
        'dotsVolume',
        'dotsFfmpegPath',
    ]
    saved = {name: getattr(cfg, name).value for name in names}
    yield cfg
    for name, value in saved.items():
        getattr(cfg, name).value = value
