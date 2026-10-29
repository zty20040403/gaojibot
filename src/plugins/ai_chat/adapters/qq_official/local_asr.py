"""SDK-free extraction of the existing ffmpeg + whisper-cli local ASR path.

Only host configuration supplies executables/model. No shell, model path, URL
or output path comes from a message. Raw media and transcript stay temporary.
"""
import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from .media_refs import MediaDenied, bounded_text


@dataclass(frozen=True)
class LocalASRConfig:
    model: Path = field(repr=False)
    ffmpeg: str = 'ffmpeg'
    ffprobe: str = 'ffprobe'
    whisper: str = 'whisper-cli'
    max_bytes: int = 2 * 1024 * 1024
    max_duration: int = 120
    timeout: int = 45
    threads: int = 2

    def __post_init__(self):
        if (not isinstance(self.model, Path) or not self.model.is_absolute()
                or any(type(v) is not int or not low <= v <= high for v, low, high in (
                    (self.max_bytes, 1, 20 * 1024 * 1024), (self.max_duration, 1, 300),
                    (self.timeout, 1, 180), (self.threads, 1, 8)))):
            raise MediaDenied('invalid_asr_configuration')

    def available(self):
        return self.model.is_file() and all(shutil.which(p) for p in (
            self.ffmpeg, self.ffprobe, self.whisper))


def audio_format(data):
    if data.startswith(b'RIFF') and data[8:12] == b'WAVE':
        return 'wav'
    if data.startswith(b'fLaC'):
        return 'flac'
    if data.startswith(b'OggS'):
        return 'ogg'
    if data.startswith(b'ID3') or len(data) > 2 and data[0] == 255 and data[1] & 0xE0 == 0xE0:
        return 'mp3'
    raise MediaDenied('audio_format_unavailable')


async def _process(argv, current, timeout):
    await current()
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.DEVNULL)
    async def read():
        value = bytearray()
        while chunk := await process.stdout.read(4096):
            if len(value) + len(chunk) > 16384:
                raise MediaDenied('asr_output_limit')
            value.extend(chunk)
        await process.wait()
        if process.returncode:
            raise MediaDenied('asr_process_failed')
        return bytes(value)
    task = asyncio.create_task(read())
    try:
        async with asyncio.timeout(timeout):
            while not task.done():
                await current()
                await asyncio.wait({task}, timeout=0.1)
            result = task.result()
            await current()
            return result
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class LocalASR:
    def __init__(self, config, *, process=_process):
        if not isinstance(config, LocalASRConfig):
            raise MediaDenied('invalid_asr_configuration')
        self.config, self.process = config, process

    async def transcribe(self, content, *, current):
        cfg = self.config
        if not cfg.available():
            raise MediaDenied('asr_not_configured')
        if type(content) is not bytes or not 1 <= len(content) <= cfg.max_bytes:
            raise MediaDenied('audio_size_limit')
        suffix = audio_format(content)
        try:
            async with asyncio.timeout(cfg.timeout):
                await current()
                with TemporaryDirectory(prefix='qq-official-asr-') as directory:
                    root = Path(directory)
                    source, wav, output = root / ('input.' + suffix), root / 'audio.wav', root / 'transcript'
                    source.write_bytes(content)
                    raw = await self.process([cfg.ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                        '-show_entries', 'format=duration', '-of', 'json', str(source)], current, cfg.timeout)
                    duration = float(json.loads(raw)['format']['duration'])
                    if not math.isfinite(duration) or not 0 < duration <= cfg.max_duration:
                        raise MediaDenied('audio_duration_limit')
                    await self.process([cfg.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                        '-protocol_whitelist', 'file,pipe', '-i', str(source), '-t', str(cfg.max_duration),
                        '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(wav)], current, cfg.timeout)
                    await self.process([cfg.whisper, '-m', str(cfg.model), '-f', str(wav), '-l', 'auto',
                        '-t', str(cfg.threads), '-otxt', '-of', str(output), '-np', '-ng'], current, cfg.timeout)
                    await current()
                    transcript = output.with_suffix('.txt')
                    if not transcript.is_file() or transcript.stat().st_size > 65536:
                        raise MediaDenied('asr_output_limit')
                    text, _ = bounded_text(transcript.read_text(encoding='utf-8'))
                    return text
        except (MediaDenied, asyncio.CancelledError):
            raise
        except (ValueError, OSError, KeyError, TimeoutError):
            raise MediaDenied('asr_unavailable') from None
