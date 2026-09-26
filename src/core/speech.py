"""Field-scoped pronunciation and atomic text / sound-clip synthesis."""

import asyncio
import io
import re
import wave

from dataclasses import dataclass
from pathlib import Path
from string import Formatter

import miniaudio

from loguru import logger


@dataclass(frozen=True)
class SpeechPart:
    text: str
    audio_path: str | None = None


def replace_words(text: str, rules: dict[str, str]) -> str:
    """Literal, longest-first, single-pass replacement (never cascades)."""
    keys = sorted((key for key in rules if key), key=len, reverse=True)
    if not keys:
        return text
    return re.sub('|'.join(re.escape(key) for key in keys), lambda match: rules[match[0]], text)


def message_parts(text: str, aliases: dict[str, str], clips: dict[str, str]) -> list[SpeechPart]:
    """Match clips in the original message, then replace ordinary text only."""
    keys = sorted((key for key, path in clips.items() if key and path), key=len, reverse=True)
    if not keys:
        return [SpeechPart(replace_words(text, aliases))]
    parts = []
    start = 0
    for match in re.finditer('|'.join(re.escape(key) for key in keys), text):
        parts.append(SpeechPart(replace_words(text[start : match.start()], aliases)))
        parts.append(SpeechPart(match[0], clips[match[0]]))
        start = match.end()
    parts.append(SpeechPart(replace_words(text[start:], aliases)))
    return parts


def build_speech_parts(
    template: str,
    fields: dict,
    user_aliases: dict[str, str],
    message_aliases: dict[str, str],
    clips: dict[str, str],
) -> list[SpeechPart]:
    """Apply dictionaries before formatting, without changing display text."""
    parts = []
    formatter = Formatter()
    for literal, field, spec, conversion in formatter.parse(template):
        parts.append(SpeechPart(literal))
        if field is None:
            continue
        value, _ = formatter.get_field(field, (), fields)
        if field == 'user_name':
            value = replace_words(str(value), user_aliases)
        if conversion:
            value = formatter.convert_field(value, conversion)
        text = formatter.format_field(value, spec)
        if field == 'message':
            parts.extend(message_parts(text, message_aliases, clips))
        else:
            parts.append(SpeechPart(text))
    # Coalesce template punctuation and text to avoid excessive TTS calls.
    merged = []
    for part in parts:
        if not part.text:
            continue
        if merged and part.audio_path is None and merged[-1].audio_path is None:
            merged[-1] = SpeechPart(merged[-1].text + part.text)
        else:
            merged.append(part)
    return merged


def concatenate_audio(audio: list[bytes]) -> bytes:
    """Decode WAV/MP3/FLAC/OGG to one 48 kHz stereo PCM WAV."""
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        for data in audio:
            decoded = miniaudio.decode(
                data,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=2,
                sample_rate=48000,
            )
            if not decoded.num_frames:
                raise ValueError('音频没有可播放的采样')
            wav.writeframes(decoded.samples.tobytes())
    return output.getvalue()


async def synthesize_parts(parts: list[SpeechPart], service) -> bytes | None:
    """Prepare an entire utterance before enqueueing; errors never drop a clip silently."""
    # Validate clips before spending time on synthesis.
    clip_data = {}
    for part in parts:
        if part.audio_path is not None and part.audio_path not in clip_data:
            path = Path(part.audio_path).expanduser()
            if not path.is_absolute():
                raise ValueError(f'触发音频请使用绝对路径: {part.audio_path}')
            try:
                data = await asyncio.to_thread(path.read_bytes)
                clip_data[part.audio_path] = await asyncio.to_thread(concatenate_audio, [data])
            except (OSError, miniaudio.DecodeError, ValueError) as exc:
                raise ValueError(f'无法读取触发音频: {path} ({exc})') from exc
    audio = []
    for part in parts:
        if part.audio_path is not None:
            audio.append(clip_data[part.audio_path])
        elif any(char.isalnum() for char in part.text):
            audio.append(await service.text_to_speech(part.text))
    if not audio:
        return None
    if len(audio) == 1:
        return audio[0]
    return await asyncio.to_thread(concatenate_audio, audio)


async def speak_template(template: str, **fields: object) -> str | None:
    """Queue one utterance, returning a visible notice if its route fell back."""
    from core.player import audio_player, audio_thread
    from core.qconfig import cfg
    from core.tts_availability import check_service_availability, invalidate_service_availability
    from core.user_voices import service_for_user, user_voice_binding
    from models.service import ServiceType
    from tts_service import get_tts_service

    parts = build_speech_parts(
        template,
        fields,
        cfg.aliasDict.value,
        cfg.messageAliasDict.value,
        cfg.audioClipDict.value,
    )
    username = str(fields.get('user_name', ''))
    binding = user_voice_binding(username)
    default_type = cfg.activeTTS.value
    target_type = binding['service'] if binding else default_type
    # Both candidates are lightweight snapshots created before the first await.
    # Edits while this utterance waits cannot alter its voice or fallback.
    target = service_for_user(username)
    fallback = get_tts_service() if binding else target
    legacy_streaming = cfg.dotsStreaming.value
    notice = None
    selected_type = target_type
    local_types = (ServiceType.DOTS, ServiceType.GPT_SOVITS, ServiceType.DOBAO)
    names = {
        ServiceType.DOTS: 'dots.tts',
        ServiceType.GPT_SOVITS: 'GPT-SoVITS',
        ServiceType.FISH_AUDIO: 'Fish Audio',
        ServiceType.SEED_TTS: '豆包语音 Seed-TTS 2.0',
        ServiceType.DOBAO: 'Doubao',
    }

    def api_url(service_type, service):
        field = {
            ServiceType.DOTS: 'dotsApiUrl',
            ServiceType.GPT_SOVITS: 'gptSovitsApiUrl',
            ServiceType.DOBAO: 'dobaoApiUrl',
        }.get(service_type, '')
        return getattr(getattr(service, '_settings', None), field, getattr(service, 'api_url', ''))

    async def readiness(service_type, url):
        # Doubao can be stopped, logged out or paused from its separate local UI.
        # Read its current state in this FIFO slot, not a previous positive cache.
        if service_type == ServiceType.DOBAO:
            return await check_service_availability(service_type, url, force=True)
        return await check_service_availability(service_type, url)

    async def choose_service():
        nonlocal notice, selected_type
        # Preserve other default/cloud routes. Doubao also checks its local API
        # when it is the default, but there is no implicit provider priority list.
        if target_type not in local_types or (binding is None and target_type != ServiceType.DOBAO):
            return target
        target_url = api_url(target_type, target)
        status = await readiness(target_type, target_url)
        if status.available:
            return target
        target_name = names.get(target_type, str(target_type))
        if binding is None:
            raise ValueError(
                f'当前默认 {target_name} 不可用（{status.detail}）；本条无法播放。'
                '请启动 API，或在 TTS 设置中选择其他默认服务'
            )
        reason = f'指定的 {target_name} 当前不可用（{status.detail}）'
        default_name = names.get(default_type, str(default_type))
        if default_type in local_types:
            fallback_url = api_url(default_type, fallback)
            if target_type == default_type and target_url.rstrip('/') == fallback_url.rstrip('/'):
                raise ValueError(f'{reason}；默认服务也是同一服务，本条无法播放，请启动服务后重试')
            fallback_status = await readiness(default_type, fallback_url)
            if not fallback_status.available:
                raise ValueError(
                    f'{reason}；默认 {default_name} 也不可用（{fallback_status.detail}），本条无法播放'
                )
        notice = f'{reason}，本条临时使用默认 {default_name}'
        logger.warning(notice)
        selected_type = default_type
        return fallback

    async def play(service) -> None:
        streaming = hasattr(service, 'stream_speech') and getattr(service, 'streaming_enabled', legacy_streaming)
        if not streaming:
            audio = await synthesize_parts(parts, service)
            if audio is not None:
                await audio_thread(audio_player.play_bytes, audio)
            return
        # Validate all local clips before speaking; each message owns one queue slot.
        clips = {}
        for part in parts:
            if part.audio_path is not None and part.audio_path not in clips:
                clips[part.audio_path] = await synthesize_parts([part], service)
        for part in parts:
            if part.audio_path is not None:
                await audio_thread(audio_player.play_bytes, clips[part.audio_path])
            elif any(char.isalnum() for char in part.text):
                await audio_player.play_pcm_stream(service.stream_speech(part.text))

    async def managed_play() -> None:
        try:
            # Clip-only messages do not need a TTS server at all.
            needs_tts = any(part.audio_path is None and any(char.isalnum() for char in part.text) for part in parts)
            service = await choose_service() if needs_tts else target
            try:
                await play(service)
            except Exception as exc:
                if selected_type in local_types:
                    invalidate_service_availability(selected_type, api_url(selected_type, service))
                if notice:
                    raise ValueError(f'{notice}；默认服务播放失败：{exc}') from exc
                raise
        finally:
            # Close the unused fallback as well, including on synthesis failure.
            for candidate in (target,) if target is fallback else (target, fallback):
                close = getattr(candidate, 'close', None)
                if close is not None:
                    try:
                        await close()
                    except Exception as exc:
                        logger.warning('TTS 资源关闭失败（{}）', type(exc).__name__)

    await audio_player.play_job_async(managed_play)
    return notice
