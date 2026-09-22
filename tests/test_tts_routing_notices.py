"""Local fallback is visible for live events as well as auditions."""

import importlib
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_live_event_speech_emits_fallback_or_failure(config, monkeypatch, failure):
    from PySide6.QtCore import QCoreApplication, QEvent
    module = importlib.import_module('bilibili.bili_service')
    service = module.BiliService()
    notices = []
    service.tts_notice.connect(notices.append)

    async def speak(*_, **__):
        if failure:
            raise ValueError('默认服务也未就绪')
        return 'GPT-SoVITS 未就绪，临时使用 Fish Audio'

    monkeypatch.setattr(module, 'speak_template', speak)
    try:
        if failure:
            with pytest.raises(ValueError):
                await service._speak('{message}', user_name='Alice', message='测试')
            assert notices == ['TTS 播报失败：默认服务也未就绪']
        else:
            await service._speak('{message}', user_name='Alice', message='测试')
            assert notices == ['GPT-SoVITS 未就绪，临时使用 Fish Audio']
    finally:
        service.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.mark.asyncio
async def test_gift_fallback_notice_reaches_live_service(config, monkeypatch):
    from models.bilibili import GiftMessage
    module = importlib.import_module('bilibili.gift_merger')
    from bilibili import bili_service
    notices = []
    bili_service.tts_notice.connect(notices.append)

    async def speak(*_, **__):
        return 'dots 未就绪，临时使用 Fish Audio'

    monkeypatch.setattr(module, 'speak_template', speak)
    try:
        await module.gift_merger._process_single_gift(
            GiftMessage(user_name='Alice', gift_name='礼物', gift_num=1)
        )
        assert notices == ['dots 未就绪，临时使用 Fish Audio']
    finally:
        bili_service.tts_notice.disconnect(notices.append)


def test_live_notice_list_keeps_all_messages_and_limits_toasts(config, monkeypatch):
    from gui.view import main

    lines, toasts = [], []
    clock = iter([1.0, 5.0, 12.0])
    monkeypatch.setattr(main.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(main.InfoBar, 'warning', lambda **kwargs: toasts.append(kwargs['content']))
    view = SimpleNamespace(
        home_panel=SimpleNamespace(message_display_card=SimpleNamespace(add_message=lines.append)),
        window=lambda: None,
    )
    for text in ('回退一', '回退二', '回退三'):
        main.MainInterface._on_tts_notice(view, text)
    assert lines == ['[TTS] 回退一', '[TTS] 回退二', '[TTS] 回退三']
    assert toasts == ['回退一', '回退三']
