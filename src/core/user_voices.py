"""Resolve exact original usernames without changing the global TTS selection."""

from copy import deepcopy
from pathlib import Path

from core.qconfig import cfg
from core.sovits_references import pair_key
from models.service import ServiceType


def model_reference(gpt: str, sovits: str) -> dict:
    if (Path(gpt) == Path(cfg.gptSovitsGptModel.value)
            and Path(sovits) == Path(cfg.gptSovitsSovitsModel.value)):
        return {
            'audio': cfg.gptSovitsRefAudioPath.value,
            'text': cfg.gptSovitsRefText.value,
            'language': cfg.gptSovitsRefTextLang.value,
            'text_free': cfg.gptSovitsRefTextFree.value,
        }
    return dict(cfg.gptSovitsReferences.value.get(pair_key(gpt, sovits), {}))


def user_voice_binding(username: str) -> dict | None:
    """Read an enabled binding without I/O or mutating saved legacy entries."""
    if not cfg.gptSovitsUserModelsEnabled.value:
        return None
    binding = cfg.gptSovitsUserModels.value.get(username)
    if not isinstance(binding, dict) or not binding or not binding.get('enabled', True):
        return None
    service = binding.get('service', ServiceType.GPT_SOVITS)
    if service not in (ServiceType.DOTS, ServiceType.GPT_SOVITS, ServiceType.FISH_AUDIO):
        return None
    result = deepcopy(binding)
    result['service'] = str(service)
    return result


def user_model_overrides(username: str) -> dict | None:
    model = user_voice_binding(username)
    if model is None or model['service'] != ServiceType.GPT_SOVITS:
        return None
    gpt, sovits = str(model.get('gpt') or ''), str(model.get('sovits') or '')
    ref = model_reference(gpt, sovits)
    # An unconfigured mapped role must fail explicitly instead of speaking in
    # the default role or inheriting another character's reference.
    return {
        'gptSovitsGptModel': gpt,
        'gptSovitsSovitsModel': sovits,
        'gptSovitsRefAudioPath': ref.get('audio', ''),
        'gptSovitsRefText': ref.get('text', ''),
        'gptSovitsRefTextLang': ref.get('language', 'auto'),
        'gptSovitsRefTextFree': ref.get('text_free', False),
    }


def service_for_user(username: str):
    """Snapshot a user's adapter; availability is checked inside the FIFO slot."""
    from tts_service import DotsTTSService, FishAudioService, GPTSovitsService, get_tts_service

    binding = user_voice_binding(username)
    if binding is None:
        return get_tts_service()
    if binding['service'] == ServiceType.DOTS:
        return DotsTTSService()
    if binding['service'] == ServiceType.FISH_AUDIO:
        return FishAudioService(reference_id=binding.get('reference_id', ''))
    return GPTSovitsService(overrides=user_model_overrides(username))
