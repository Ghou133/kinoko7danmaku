from core.qconfig import cfg
from models.service import ServiceType

from .base import TTSService
from .edge import EdgeService
from .dots import DotsTTSService
from .fish_speech import FishSpeechService
from .fish_audio import FishAudioService
from .gpt_sovits import GPTSovitsService
from .minimax import MinimaxService
from .piper import PiperService


def get_tts_service() -> TTSService:
    """Create a lightweight adapter with the current engine and configuration."""
    services = {
        ServiceType.DOTS: DotsTTSService,
        ServiceType.FISH_SPEECH: FishSpeechService,
        ServiceType.FISH_AUDIO: FishAudioService,
        ServiceType.GPT_SOVITS: GPTSovitsService,
        ServiceType.MINIMAX: MinimaxService,
        ServiceType.PIPER: PiperService,
        ServiceType.EDGE: EdgeService,
    }
    return services[cfg.activeTTS.value]()


__all__ = [
    'DotsTTSService',
    'EdgeService',
    'FishSpeechService',
    'FishAudioService',
    'GPTSovitsService',
    'MinimaxService',
    'PiperService',
    'TTSService',
    'get_tts_service',
]
