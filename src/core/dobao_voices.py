"""Offline Doubao voice catalog, legacy aliases, and portable installation lookup."""

import json
import sys

from functools import lru_cache
from pathlib import Path

from .const import RESOURCE_DIR


DEFAULT_DOBAO_VOICE = 'zh_female_wenroutaozi_uranus_bigtts'
CLASSIC_DOBAO_VOICE = 'zh_female_wenroutaozi_v2_mars_bigtts'
_CATALOG_PATH = RESOURCE_DIR / 'dobao_voices.json'
_ALIASES = {'taozi': DEFAULT_DOBAO_VOICE, 'taozi-classic': CLASSIC_DOBAO_VOICE}
_INVALID_VOICE = '请选择豆包音色，或填写不超过 200 字符的有效音色 ID'


@lru_cache(maxsize=1)
def _catalog() -> tuple[tuple[str, str, str], ...]:
    """Read bundled metadata only; absence never erases a saved voice ID."""
    try:
        raw = json.loads(_CATALOG_PATH.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raw = {}
    rows = raw.get('voices', []) if isinstance(raw, dict) else []
    result = []
    seen = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        voice_id, name = row.get('style_id'), row.get('name')
        if not isinstance(voice_id, str) or not isinstance(name, str):
            continue
        voice_id, name = voice_id.strip(), name.strip()
        if not voice_id or len(voice_id) > 200 or not name or voice_id in seen:
            continue
        seen.add(voice_id)
        result.append((voice_id, name, str(row.get('id', ''))))
    if not result:
        result = [
            (DEFAULT_DOBAO_VOICE, '温柔桃子（升级版）', ''),
            (CLASSIC_DOBAO_VOICE, '温柔桃子（经典版）', ''),
        ]
    return tuple(result)


def get_dobao_voices() -> list[dict[str, str]]:
    """Return independent catalog rows with canonical ``id`` and display ``name``."""
    return [{'id': voice_id, 'name': name} for voice_id, name, _ in _catalog()]


def normalize_dobao_voice(value: str) -> str:
    """Map old aliases to IDs while preserving unknown, nonempty saved IDs."""
    if not isinstance(value, str):
        raise ValueError(_INVALID_VOICE)
    voice_id = value.strip()
    if not voice_id or len(voice_id) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in voice_id):
        raise ValueError(_INVALID_VOICE)
    if voice_id in _ALIASES:
        return _ALIASES[voice_id]
    for canonical, name, legacy_id in _catalog():
        if voice_id in (canonical, name, legacy_id):
            return canonical
    return voice_id


def dobao_voice_name(value: str) -> str:
    """Resolve a display name without replacing unknown values by another voice."""
    try:
        voice_id = normalize_dobao_voice(value)
    except ValueError:
        return '未选择音色'
    return next((name for canonical, name, _ in _catalog() if canonical == voice_id), voice_id)


def detect_dobao_folder() -> str:
    """Find DoBao-TTS-Win next to the source checkout or portable executable."""
    start = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
    for parent in (start, *tuple(start.parents)[:3]):
        candidate = parent / 'DoBao-TTS-Win'
        if (candidate / 'local-api' / 'server.mjs').is_file() and (candidate / 'runtime' / 'node.exe').is_file():
            return str(candidate)
    return ''
