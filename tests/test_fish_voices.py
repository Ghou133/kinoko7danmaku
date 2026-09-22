"""Fish voice library validation and metadata lookup without real requests."""

import asyncio
import traceback

from types import SimpleNamespace

import httpx
import pytest

from core.fish_voices import fetch_voice_name, normalize_voice_id


VOICE_ID = '4ed45d53ee9245d9abf20db5221b19b2'
OTHER_ID = '11111111111111111111111111111111'


@pytest.mark.parametrize('value', [
    VOICE_ID,
    f'  {VOICE_ID.upper()}  ',
    f'https://fish.audio/m/{VOICE_ID}',
    f'https://fish.audio/app/m/{VOICE_ID}/',
    f'https://fish.audio/zh-CN/app/m/{VOICE_ID}/',
    f'https://fish.audio/en/m/{VOICE_ID}?share=1#preview',
])
def test_normalize_voice_id_and_official_page_links(value):
    assert normalize_voice_id(value) == VOICE_ID


@pytest.mark.parametrize('value', [
    '', None, 'invalid', VOICE_ID[:-1], 'z' * 32,
    f'https://fish.audio.evil.example/m/{VOICE_ID}',
    f'https://evil.example/m/{VOICE_ID}',
    f'https://fish.audio@evil.example/m/{VOICE_ID}',
    f'https://user:pass@fish.audio/m/{VOICE_ID}',
    f'https://fish.audio:invalid/m/{VOICE_ID}',
    f'https://fish.audio:9880/m/{VOICE_ID}',
    f'http://fish.audio/m/{VOICE_ID}',
    f'https://fish.audio/api/model/{VOICE_ID}',
    f'https://fish.audio/m/{VOICE_ID}/extra',
])
def test_reject_invalid_ids_and_untrusted_links(value):
    with pytest.raises(ValueError, match='32 位音色 ID'):
        normalize_voice_id(value)


def mock_client(monkeypatch, handler):
    original = httpx.AsyncClient
    options = []

    def create(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', create)
    return options


@pytest.mark.asyncio
async def test_fetch_name_uses_fixed_endpoint_and_bearer_auth(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'_id': VOICE_ID, 'title': '  音色名称  '})

    options = mock_client(monkeypatch, handle)
    assert await fetch_voice_name(f'https://fish.audio/zh-CN/app/m/{VOICE_ID}/', ' test-key ') == '音色名称'
    assert len(requests) == 1
    assert requests[0].method == 'GET'
    assert str(requests[0].url) == f'https://api.fish.audio/model/{VOICE_ID}'
    assert requests[0].headers['authorization'] == 'Bearer test-key'
    assert options == [{'timeout': 10.0, 'follow_redirects': False}]


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [302, 401, 403, 404, 429, 500])
async def test_remote_failure_never_follows_redirect_or_exposes_body(monkeypatch, status):
    requests = []
    secret = 'test-secret-must-not-leak'

    def handle(request):
        requests.append(request)
        return httpx.Response(status, text=secret, headers={'location': 'https://other.example/'})

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError, match=f'Fish Audio 返回 {status}') as caught:
        await fetch_voice_name(VOICE_ID, secret)
    assert len(requests) == 1
    assert secret not in ''.join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize('error_class', [httpx.ConnectError, httpx.ReadTimeout, RuntimeError])
async def test_transport_exception_context_is_sanitized(monkeypatch, error_class):
    secret = 'test-secret-must-not-leak'

    def handle(_):
        raise error_class(secret)

    mock_client(monkeypatch, handle)
    with pytest.raises(ValueError) as caught:
        await fetch_voice_name(VOICE_ID, secret)
    assert secret not in ''.join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata', [{}, {'title': None}, {'title': ''}, {'title': '  '}, {'title': 123}, []])
async def test_missing_name_allows_manual_fallback(monkeypatch, metadata):
    mock_client(monkeypatch, lambda _: httpx.Response(200, json=metadata))
    with pytest.raises(ValueError, match='请手动填写'):
        await fetch_voice_name(VOICE_ID, 'test-key')


@pytest.mark.asyncio
async def test_malformed_json_does_not_expose_body(monkeypatch):
    secret = 'test-secret-must-not-leak'
    mock_client(monkeypatch, lambda _: httpx.Response(200, text=secret))
    with pytest.raises(ValueError) as caught:
        await fetch_voice_name(VOICE_ID, secret)
    assert secret not in ''.join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_bad_input_never_makes_a_request(monkeypatch):
    def forbidden_client(**_):
        pytest.fail('Invalid input must not make a request')

    monkeypatch.setattr(httpx, 'AsyncClient', forbidden_client)
    for voice, key in [('invalid', 'test-key'), (VOICE_ID, ''), (VOICE_ID, 'bad\nkey')]:
        with pytest.raises(ValueError):
            await fetch_voice_name(voice, key)


@pytest.mark.asyncio
async def test_name_lookup_cancellation_propagates(monkeypatch):
    started = asyncio.Event()

    async def handle(_):
        started.set()
        await asyncio.Event().wait()

    mock_client(monkeypatch, handle)
    task = asyncio.create_task(fetch_voice_name(VOICE_ID, 'test-key'))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_migration_normalizes_selected_voice_keeps_names_and_is_idempotent(app):
    from core.qconfig import _migrate_fish_audio_voices

    selected = f'https://fish.audio/zh-CN/app/m/{VOICE_ID}/'
    previous = {OTHER_ID: '另一个音色'}
    writes = []
    config = SimpleNamespace(
        fishAudioReferenceId=SimpleNamespace(value=selected),
        fishAudioVoices=SimpleNamespace(value=previous.copy()),
    )

    def save(item, value):
        writes.append(value)
        item.value = value

    config.set = save
    _migrate_fish_audio_voices(config)
    _migrate_fish_audio_voices(config)
    assert config.fishAudioReferenceId.value == VOICE_ID
    assert config.fishAudioVoices.value == {**previous, VOICE_ID: f'音色 {VOICE_ID[:8]}'}
    assert previous == {OTHER_ID: '另一个音色'}
    assert len(writes) == 2
    config.fishAudioVoices.value[VOICE_ID] = '用户填写的名字'
    _migrate_fish_audio_voices(config)
    assert config.fishAudioVoices.value[VOICE_ID] == '用户填写的名字'
    assert len(writes) == 2


@pytest.mark.parametrize('reference', ['', 'legacy-invalid-reference'])
def test_migration_ignores_invalid_reference_without_changing_it(app, reference):
    from core.qconfig import _migrate_fish_audio_voices

    config = SimpleNamespace(
        fishAudioReferenceId=SimpleNamespace(value=reference),
        fishAudioVoices=SimpleNamespace(value={OTHER_ID: '已有音色'}),
        set=lambda *_: pytest.fail('Invalid reference must not be migrated'),
    )
    _migrate_fish_audio_voices(config)
    assert config.fishAudioReferenceId.value == reference
    assert config.fishAudioVoices.value == {OTHER_ID: '已有音色'}
