"""Persist reference settings separately for each exact pair of model files."""

import json
import os

from qfluentwidgets import qconfig
from core.qconfig import cfg


def pair_key(gpt: str, sovits: str) -> str:
    return json.dumps([os.path.normcase(os.path.abspath(p)) for p in (gpt, sovits)], ensure_ascii=False)


def save_reference() -> None:
    if not cfg.gptSovitsGptModel.value or not cfg.gptSovitsSovitsModel.value:
        return
    profiles = dict(cfg.gptSovitsReferences.value)
    profiles[pair_key(cfg.gptSovitsGptModel.value, cfg.gptSovitsSovitsModel.value)] = {
        'audio': cfg.gptSovitsRefAudioPath.value,
        'text': cfg.gptSovitsRefText.value,
        'language': cfg.gptSovitsRefTextLang.value,
        'text_free': cfg.gptSovitsRefTextFree.value,
    }
    qconfig.set(cfg.gptSovitsReferences, profiles)


class ReferenceMemory:
    def __init__(self):
        self.switching = False
        for item in (cfg.gptSovitsRefAudioPath, cfg.gptSovitsRefText,
                     cfg.gptSovitsRefTextLang, cfg.gptSovitsRefTextFree):
            item.valueChanged.connect(self.changed)

    def changed(self, *_):
        if not self.switching:
            save_reference()

    def select(self, gpt: str, sovits: str):
        save_reference()
        profile = cfg.gptSovitsReferences.value.get(pair_key(gpt, sovits), {})
        self.switching = True
        try:
            qconfig.set(cfg.gptSovitsGptModel, gpt)
            qconfig.set(cfg.gptSovitsSovitsModel, sovits)
            for item, key, default in (
                (cfg.gptSovitsRefAudioPath, 'audio', ''),
                (cfg.gptSovitsRefText, 'text', ''),
                (cfg.gptSovitsRefTextLang, 'language', 'auto'),
                (cfg.gptSovitsRefTextFree, 'text_free', False),
            ):
                qconfig.set(item, profile.get(key, default))
        finally:
            self.switching = False


reference_memory = ReferenceMemory()
