"""Named voice selection and editing without synthesis or network calls."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

A = '4ed45d53ee9245d9abf20db5221b19b2'
B = '1234567890abcdef1234567890abcdef'


@pytest.fixture
def editor(config):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import QWidget
    from gui.components.fish_voice_card import FishVoiceCard, FishVoiceDialog

    names = ('fishAudioVoices', 'fishAudioReferenceId', 'fishAudioApiKey')
    saved = {name: deepcopy(getattr(config, name).value) for name in names}
    config.fishAudioVoices.value = {A: '原有音色'}
    config.fishAudioReferenceId.value = A
    config.fishAudioApiKey.value = 'test-only-key'
    for font in ('segoeui.ttf', 'msyh.ttc'):
        font_path = Path('C:/Windows/Fonts') / font
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))
    host = QWidget()
    host.resize(1000, 650)
    cards = [FishVoiceCard(host), FishVoiceCard(host)]
    dialog = FishVoiceDialog(parent=host)
    yield config, host, cards, dialog
    dialog._closed = True
    host.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    for name, value in saved.items():
        getattr(config, name).value = value


@pytest.mark.asyncio
async def test_add_named_voice_syncs_two_pages_and_preserves_queued_voice(editor, monkeypatch):
    from core.const import DATA_DIR
    from gui.components import fish_voice_card
    from tts_service.fish_audio import FishAudioService

    cfg, _, cards, dialog = editor
    old = FishAudioService()

    async def forbidden(*_):
        pytest.fail('A manually named voice must save without fetching metadata')

    monkeypatch.setattr(fish_voice_card, 'fetch_voice_name', forbidden)
    dialog.idEdit.setText(f'https://fish.audio/zh-CN/app/m/{B}/')
    dialog.nameEdit.setText('第二个音色')
    await dialog.save()
    assert cfg.fishAudioVoices.value == {A: '原有音色', B: '第二个音色'}
    assert cfg.fishAudioReferenceId.value == B
    assert all(card.comboBox.currentData() == B for card in cards)
    assert old._request('test')[1]['reference_id'] == A
    assert FishAudioService()._request('test')[1]['reference_id'] == B
    persisted = json.loads((DATA_DIR / 'config.json').read_text('utf-8'))['FishAudioService']
    assert persisted['Voices'][B] == '第二个音色'
    cards[0].comboBox.setCurrentIndex(cards[0].comboBox.findData(A))
    assert cards[1].comboBox.currentData() == A


@pytest.mark.asyncio
async def test_save_fetches_missing_name_and_updates_duplicate_id(editor, monkeypatch):
    from gui.components import fish_voice_card

    cfg, _, _, dialog = editor
    calls = []

    async def fetch(voice, key):
        calls.append((voice, key))
        return '网站音色名称'

    monkeypatch.setattr(fish_voice_card, 'fetch_voice_name', fetch)
    dialog.idEdit.setText(A.upper())
    await dialog.save()
    assert calls == [(A, 'test-only-key')]
    assert cfg.fishAudioVoices.value == {A: '网站音色名称'}


@pytest.mark.asyncio
async def test_failed_lookup_keeps_draft_and_allows_manual_name(editor, monkeypatch):
    from gui.components import fish_voice_card

    cfg, _, _, dialog = editor

    async def failed(*_):
        raise ValueError('获取名称失败')

    monkeypatch.setattr(fish_voice_card, 'fetch_voice_name', failed)
    dialog.idEdit.setText(B)
    await dialog.save()
    assert cfg.fishAudioVoices.value == {A: '原有音色'}
    assert '手动填写' in dialog.status.text()
    assert dialog.idEdit.text() == B
    dialog.nameEdit.setText('手动名称')
    await dialog.save()
    assert cfg.fishAudioVoices.value[B] == '手动名称'


@pytest.mark.asyncio
async def test_pending_lookup_does_not_overwrite_new_input_or_save_after_close(editor, monkeypatch):
    from gui.components import fish_voice_card

    cfg, _, _, dialog = editor
    ready, release = asyncio.Event(), asyncio.Event()

    async def slow(*_):
        ready.set()
        await release.wait()
        return '过时的名称'

    monkeypatch.setattr(fish_voice_card, 'fetch_voice_name', slow)
    dialog.idEdit.setText(A)
    task = asyncio.create_task(dialog._lookup())
    await ready.wait()
    dialog.idEdit.setText(B)
    dialog.nameEdit.setText('新的名称')
    release.set()
    assert await task is None
    assert dialog.nameEdit.text() == '新的名称'
    assert cfg.fishAudioVoices.value == {A: '原有音色'}
    dialog.nameEdit.clear()
    dialog._closed = True
    await dialog.save()
    assert cfg.fishAudioVoices.value == {A: '原有音色'}


def test_remove_active_voice_clears_selection_without_selecting_another(editor):
    from gui.components.fish_voice_card import FishVoiceDialog

    cfg, host, cards, _ = editor
    cfg.fishAudioVoices.value = {A: '原有音色', B: '另一个音色'}
    dialog = FishVoiceDialog(A, host)
    dialog.remove()
    assert cfg.fishAudioVoices.value == {B: '另一个音色'}
    assert cfg.fishAudioReferenceId.value == ''
    assert all(card.comboBox.currentData() == '' for card in cards)


def test_legacy_url_migration_selects_one_canonical_voice_and_sends_id(editor):
    from core.qconfig import _migrate_fish_audio_voices
    from tts_service.fish_audio import FishAudioService

    cfg, _, cards, _ = editor
    cfg.fishAudioReferenceId.value = f'https://fish.audio/zh-CN/app/m/{A}/'
    _migrate_fish_audio_voices(cfg)
    assert all(card.comboBox.currentData() == A for card in cards)
    assert all(card.comboBox.count() == 2 for card in cards)  # placeholder + voice
    assert FishAudioService()._request('test')[1]['reference_id'] == A


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['cancel', 'remove'])
async def test_closing_during_name_lookup_cannot_save_afterwards(editor, monkeypatch, action):
    from gui.components import fish_voice_card

    cfg, host, _, _ = editor
    dialog = fish_voice_card.FishVoiceDialog(A, host)
    ready, release = asyncio.Event(), asyncio.Event()

    async def slow(*_):
        ready.set()
        await release.wait()
        return '不应保存'

    monkeypatch.setattr(fish_voice_card, 'fetch_voice_name', slow)
    dialog.nameEdit.clear()
    task = dialog.save()
    await ready.wait()
    if action == 'cancel':
        dialog.cancelButton.click()
    else:
        dialog.removeButton.click()
    assert dialog._closed
    release.set()
    await task
    expected = {A: '原有音色'} if action == 'cancel' else {}
    assert cfg.fishAudioVoices.value == expected


def test_editor_layout_capture(editor):
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtTest import QTest

    _, host, _, dialog = editor
    host.show()
    dialog.idEdit.setText(A)
    dialog.nameEdit.setText('可自行命名的音色')
    dialog.open()
    QTest.qWait(250)
    QCoreApplication.processEvents()
    assert dialog.idEdit.geometry().right() < dialog.nameEdit.geometry().left()
    output = Path(__file__).resolve().parents[1] / 'build' / 'fish-voices-check'
    output.mkdir(parents=True, exist_ok=True)
    assert host.grab().save(str(output / 'voice-editor.png'))
