"""Read the user's local Gradio reference presets for danmaku synthesis."""

from pathlib import Path

DEFAULT_PROMPTS_DIR = Path(r'E:\AITTS\dots.tts\apps\gradio\default_prompts')
AUDIO_SUFFIXES = {'.wav', '.mp3', '.flac', '.ogg', '.m4a'}


def read_prompt_text(path: Path) -> str:
    for encoding in ('utf-8-sig', 'gbk'):
        try:
            return path.read_text(encoding=encoding).strip()
        except UnicodeDecodeError:
            continue
    raise ValueError(f'参考文本编码无法识别: {path}')


def local_reference(voice: str, directory: Path = DEFAULT_PROMPTS_DIR) -> tuple[str, str] | None:
    """An explicit preset wins; blank means first sorted local audio, then server fallback."""
    if not directory.is_dir():
        return None
    if voice:
        candidate = (directory / voice).resolve()
        if not candidate.is_relative_to(directory.resolve()):
            return None
        if not candidate.is_file() or candidate.suffix.lower() not in AUDIO_SUFFIXES:
            return None
    else:
        files = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES)
        if not files:
            return None
        candidate = files[0].resolve()
    sidecar = candidate.with_suffix('.txt')
    if sidecar.is_file():
        return str(candidate), read_prompt_text(sidecar)
    mapping = directory / 'prompt_text'
    if mapping.is_file():
        for line in read_prompt_text(mapping).splitlines():
            name, separator, text = line.partition('|')
            if separator and name.strip() in (candidate.stem, candidate.name):
                return str(candidate), text.strip()
    return str(candidate), ''
