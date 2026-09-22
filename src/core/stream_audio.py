"""Incremental decoder for the PCM16 WAV stream exposed by dots.tts."""

import struct

from dataclasses import dataclass


@dataclass(frozen=True)
class PCMChunk:
    data: bytes
    sample_rate: int
    channels: int


class WavStreamDecoder:
    def __init__(self, *, zero_size_is_stream: bool = False) -> None:
        self.zero_size_is_stream = zero_size_is_stream
        self.buffer = bytearray()
        self.riff = False
        self.format = None
        self.in_data = False
        self.remaining = None
        self.frames = 0

    def feed(self, data: bytes) -> list[PCMChunk]:
        self.buffer.extend(data)
        if not self.riff:
            if len(self.buffer) < 12:
                return []
            if self.buffer[:4] != b'RIFF' or self.buffer[8:12] != b'WAVE':
                raise ValueError('dots.tts 流不是 WAV 格式')
            del self.buffer[:12]
            self.riff = True
        while not self.in_data:
            if len(self.buffer) < 8:
                return []
            tag, size = struct.unpack_from('<4sI', self.buffer)
            if tag == b'data':
                if self.format is None:
                    raise ValueError('WAV 流缺少 fmt 头')
                del self.buffer[:8]
                self.in_data = True
                self.remaining = None if size == 0xFFFFFFFF or (size == 0 and self.zero_size_is_stream) else size
                break
            if size > 1024 * 1024:
                raise ValueError('WAV 流头异常')
            if len(self.buffer) < 8 + size + size % 2:
                return []
            if tag == b'fmt ':
                if size < 16:
                    raise ValueError('WAV fmt 头不完整')
                encoding, channels, rate, _, align, bits = struct.unpack_from('<HHIIHH', self.buffer, 8)
                if encoding != 1 or bits != 16 or channels not in (1, 2) or not 8000 <= rate <= 192000:
                    raise ValueError('仅支持单/双声道 PCM16 WAV 流')
                if align != channels * 2:
                    raise ValueError('WAV 帧大小无效')
                self.format = (rate, channels, align)
            del self.buffer[: 8 + size + size % 2]
        rate, channels, align = self.format
        available = len(self.buffer) if self.remaining is None else min(len(self.buffer), self.remaining)
        count = available - available % align
        if not count:
            return []
        pcm = bytes(self.buffer[:count])
        del self.buffer[:count]
        if self.remaining is not None:
            self.remaining -= count
        self.frames += count // align
        return [PCMChunk(pcm, rate, channels)]

    def finish(self) -> None:
        if not self.frames or not self.in_data:
            raise ValueError('dots.tts 流未返回音频')
        if self.remaining not in (None, 0) or (self.remaining is None and self.buffer):
            raise ValueError('dots.tts 音频流不完整')
