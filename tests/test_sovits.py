import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def sovits_config(config):
    names = [n for n in dir(config) if n.startswith('gptSovits')]
    saved = {n: getattr(config, n).value for n in names}
    config.gptSovitsReferences.value = {}
    yield config
    from core.sovits_references import reference_memory
    reference_memory.switching = True
    for n, value in saved.items():
        getattr(config, n).value = value
    reference_memory.switching = False


def model(root, folder, name):
    path = root / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return str(path)


def test_pairs_versions_epochs_and_ambiguity(tmp_path):
    from core.sovits_models import scan_models
    (tmp_path / 'api_v2.py').touch()
    model(tmp_path, 'GPT_weights_v4', '诗歌剧-e15.ckpt')
    model(tmp_path, 'SoVITS_weights_v4', '诗歌剧_e6_s2370_l32.pth')
    model(tmp_path, 'GPT_weights_v2ProPlus', '爱音.ckpt')
    model(tmp_path, 'SoVITS_weights_v2ProPlus', '爱音.pth')
    model(tmp_path, 'GPT_weights_v2', '爱音.ckpt')
    pairs, issues = scan_models(str(tmp_path))
    assert [(p.name, p.version) for p in pairs] == [('爱音', 'v2ProPlus'), ('诗歌剧', 'v4')]
    assert len(issues) == 1
    model(tmp_path, 'SoVITS_weights_v4', '诗歌剧_e8_s3000.pth')
    pairs, issues = scan_models(str(tmp_path))
    assert len(pairs) == 1
    assert any('多个 SoVITS' in i for i in issues)


def test_reference_memory_and_disk(sovits_config):
    from core.sovits_references import reference_memory
    from qfluentwidgets import qconfig
    from core.const import DATA_DIR
    cfg = sovits_config
    reference_memory.select('A.ckpt', 'A.pth')
    qconfig.set(cfg.gptSovitsRefAudioPath, '音频 A.wav')
    qconfig.set(cfg.gptSovitsRefText, '角色 A 台词')
    qconfig.set(cfg.gptSovitsRefTextLang, 'Japanese')
    reference_memory.select('B.ckpt', 'B.pth')
    assert cfg.gptSovitsRefAudioPath.value == ''
    assert cfg.gptSovitsRefText.value == ''
    qconfig.set(cfg.gptSovitsRefText, '角色 B 台词')
    reference_memory.select('A.ckpt', 'A.pth')
    assert cfg.gptSovitsRefAudioPath.value == '音频 A.wav'
    assert cfg.gptSovitsRefText.value == '角色 A 台词'
    assert cfg.gptSovitsRefTextLang.value == 'Japanese'
    persisted = json.loads((DATA_DIR / 'config.json').read_text('utf-8'))
    assert any('ModelReferences' in group for group in persisted.values() if isinstance(group, dict))
    reference_memory.select('B.ckpt', 'B.pth')
    assert cfg.gptSovitsRefText.value == '角色 B 台词'


@pytest.mark.asyncio
async def test_rest_payload_and_switch_failure(sovits_config, monkeypatch):
    from tts_service.gpt_sovits import GPTSovitsService
    cfg = sovits_config
    cfg.gptSovitsFolder.value = ''
    cfg.gptSovitsApiUrl.value = 'http://127.0.0.1:9880'
    cfg.gptSovitsGptModel.value = '角色.ckpt'
    cfg.gptSovitsSovitsModel.value = '角色.pth'
    cfg.gptSovitsRefAudioPath.value = '参考.wav'
    calls = []
    fail = False
    wav = b'RIFF' + bytes(4) + b'WAVE' + bytes(32)
    def handle(request):
        if request.url.path == '/kinoko/status':
            return httpx.Response(404)
        calls.append(request)
        if fail:
            return httpx.Response(400, json={'message': 'bad weights'})
        return httpx.Response(200, content=wav) if request.url.path == '/tts' else httpx.Response(200, json={'message': 'success'})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    service = GPTSovitsService()
    assert await service.text_to_speech('你好', text_lang='Chinese', ref_text_lang='Japanese', text_split_method='按标点符号切') == wav
    assert [r.url.path for r in calls] == ['/set_sovits_weights', '/set_gpt_weights', '/tts']
    payload = json.loads(calls[-1].content)
    assert payload['text_lang'] == 'all_zh'
    assert payload['prompt_lang'] == 'all_ja'
    assert payload['text_split_method'] == 'cut5'
    assert payload['media_type'] == 'wav'
    fail = True
    calls.clear()
    with pytest.raises(ValueError, match='bad weights'):
        await service.text_to_speech('你好')
    assert len(calls) == 1


def test_model_card_and_reference_restore(sovits_config, tmp_path):
    from gui.components.sovits_cards import SovitsModelCard
    from qfluentwidgets import qconfig
    (tmp_path / 'api_v2.py').touch()
    for name in ('A', 'B'):
        model(tmp_path, 'GPT_weights_v4', name + '.ckpt')
        model(tmp_path, 'SoVITS_weights_v4', name + '.pth')
    qconfig.set(sovits_config.gptSovitsFolder, str(tmp_path))
    card = SovitsModelCard()
    assert card.combo.count() == 3
    card.combo.setCurrentIndex(1)
    qconfig.set(sovits_config.gptSovitsRefText, 'A reference')
    card.combo.setCurrentIndex(2)
    assert sovits_config.gptSovitsRefText.value == ''
    card.combo.setCurrentIndex(1)
    assert sovits_config.gptSovitsRefText.value == 'A reference'
    card.deleteLater()

@pytest.mark.asyncio
async def test_sovits_stream_early_pcm_and_close(sovits_config, monkeypatch):
    import io
    import wave
    from tts_service.gpt_sovits import GPTSovitsService
    cfg = sovits_config
    cfg.gptSovitsFolder.value = ''
    cfg.gptSovitsApiUrl.value = 'http://127.0.0.1:9880'
    cfg.gptSovitsGptModel.value = 'A.ckpt'
    cfg.gptSovitsSovitsModel.value = 'A.pth'
    cfg.gptSovitsRefAudioPath.value = 'A.wav'
    cfg.gptSovitsTextSplitMethod.value = '不切'
    ended, closed = [], []
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
    header = buffer.getvalue()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield header[:17]
            yield header[17:] + b'\x01\x00' * 10
            yield b'\x02\x00' * 10
            ended.append(True)
        async def aclose(self):
            closed.append(True)
    def handle(request):
        if request.url.path == '/kinoko/status':
            return httpx.Response(404)
        if request.url.path == '/tts':
            payload = json.loads(request.content)
            assert payload['streaming_mode'] is True
            assert payload['text_split_method'] == 'cut5'
            assert payload['batch_size'] == 1
            return httpx.Response(200, stream=Stream())
        return httpx.Response(200, json={'message': 'success'})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    stream = GPTSovitsService().stream_speech('第一句。第二句。')
    first = await anext(stream)
    assert first.sample_rate == 48000 and first.data and not ended
    await stream.aclose()
    assert closed and not ended
    stream = GPTSovitsService().stream_speech('第一句。第二句。')
    chunks = [chunk async for chunk in stream]
    assert sum(len(c.data) for c in chunks) == 40
    assert ended


def test_sovits_stream_does_not_depend_on_dots(sovits_config):
    from tts_service.gpt_sovits import GPTSovitsService
    sovits_config.gptSovitsApiUrl.value = 'http://127.0.0.1:9880'
    sovits_config.gptSovitsStreaming.value = True
    sovits_config.dotsStreaming.value = False
    assert GPTSovitsService().streaming_enabled
    sovits_config.gptSovitsStreaming.value = False
    assert not GPTSovitsService().streaming_enabled
