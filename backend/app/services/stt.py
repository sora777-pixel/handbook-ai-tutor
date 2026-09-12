from __future__ import annotations

import asyncio
import httpx
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.ffmpeg import FFmpegMissing, ffmpeg_cmd, ffprobe_cmd


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


class STTProvider(ABC):
    name: str

    @abstractmethod
    async def transcribe(self, audio_path: Path) -> list[TranscriptSegment]:
        raise NotImplementedError


async def probe_duration(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        ffprobe_cmd(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip() or "0")
    except ValueError:
        return 0.0


async def extract_audio(video_bytes: bytes, suffix: str = ".mp4") -> tuple[Path, Path, float]:
    """Write video to a temp file, extract WAV via FFmpeg, return (workdir, wav, duration)."""
    work = Path(tempfile.mkdtemp(prefix="tutor-video-"))
    video_path = work / f"source{suffix}"
    wav_path = work / "audio.wav"
    video_path.write_bytes(video_bytes)
    try:
        ffmpeg = ffmpeg_cmd()
    except FFmpegMissing:
        shutil.rmtree(work, ignore_errors=True)
        raise
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(wav_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise FFmpegMissing(
            "未找到 ffmpeg，无法从视频提取音频。"
            "请把 ffmpeg.exe 与 ffprobe.exe 放到项目的 tools\\ffmpeg\\bin\\ "
            "（双击 start.vbs 会尝试自动下载），然后重新导入。"
        ) from exc
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not wav_path.exists():
        shutil.rmtree(work, ignore_errors=True)
        raise RuntimeError(f"FFmpeg failed: {stderr.decode()[-500:]}")
    duration = await probe_duration(wav_path)
    if duration <= 0:
        duration = await probe_duration(video_path)
    return work, wav_path, duration


class MockSTT(STTProvider):
    """Offline/dev STT so the video loop works without a GPU or Whisper weights."""

    name = "mock"

    MOCK_LESSON = [
        "Welcome to this short lesson on photosynthesis in green plants.",
        "Light energy is captured by chlorophyll inside chloroplasts.",
        "Water and carbon dioxide are converted into glucose and oxygen.",
        "The light-dependent reactions produce ATP and NADPH.",
        "The Calvin cycle then fixes carbon into sugars the plant can store.",
    ]

    async def transcribe(self, audio_path: Path) -> list[TranscriptSegment]:
        duration = await probe_duration(audio_path)
        if duration <= 0:
            duration = float(len(self.MOCK_LESSON) * 5)
        n = len(self.MOCK_LESSON)
        window = duration / n
        segments: list[TranscriptSegment] = []
        for i, sentence in enumerate(self.MOCK_LESSON):
            start = round(i * window, 2)
            end = round(min(duration, (i + 1) * window), 2)
            segments.append(TranscriptSegment(start=start, end=end, text=sentence))
        return segments


class FasterWhisperSTT(STTProvider):
    name = "faster_whisper"

    # 长音频按此长度切段再逐段识别。faster-whisper 会对整段音频一次性构建
    # log-mel 特征（25 分钟 ≈ 220+ MB float32），在小内存机器上直接 OOM；
    # 切段后峰值内存随段长线性下降，同时段间用时间偏移拼回全局时间戳。
    CHUNK_SECONDS = 300.0

    async def transcribe(self, audio_path: Path) -> list[TranscriptSegment]:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper is not installed. Use STT_PROVIDER=mock.") from exc

        settings = get_settings()
        model_size = settings.stt_whisper_model
        use_vad = settings.stt_whisper_vad

        def _resolve_model() -> str:
            """Prefer a repo-local model dir (tools/whisper-<size>) when present —
            HuggingFace 在本网络不可达，模型可经 ModelScope 离线放置。否则按
            模型名交给 faster-whisper 走 HF 下载。"""
            from app.core.config import REPO_ROOT

            local = REPO_ROOT / "tools" / f"whisper-{model_size}"
            if (local / "model.bin").is_file() and (local / "model.bin").stat().st_size > 1_000_000:
                return str(local)
            return model_size

        def _transcribe_one(model, wav: Path, offset: float) -> list[TranscriptSegment]:
            # language=None → auto-detect：中文讲课夹杂法语/英语朗读时逐段识别，
            # multilingual 模型天然支持 code-switching，无需专门微调。
            segs, _info = model.transcribe(
                str(wav),
                vad_filter=use_vad,
                beam_size=5,
            )
            out: list[TranscriptSegment] = []
            for seg in segs:
                out.append(
                    TranscriptSegment(
                        start=float(seg.start) + offset,
                        end=float(seg.end) + offset,
                        text=seg.text.strip(),
                    )
                )
            return out

        def _run() -> list[TranscriptSegment]:
            model = WhisperModel(_resolve_model(), device="cpu", compute_type="int8")
            duration = _duration_sync(audio_path)
            if duration <= self.CHUNK_SECONDS:
                return _transcribe_one(model, audio_path, 0.0)

            chunks = _split_wav_sync(audio_path, self.CHUNK_SECONDS)
            out: list[TranscriptSegment] = []
            try:
                for wav, offset in chunks:
                    last_exc: Exception | None = None
                    done = False
                    for _attempt in range(3):
                        try:
                            out.extend(_transcribe_one(model, wav, offset))
                            done = True
                            break
                        except Exception as exc:
                            last_exc = exc
                    if not done:
                        detail = str(last_exc)[:160] if last_exc else "unknown error"
                        out.append(
                            TranscriptSegment(
                                start=offset,
                                end=offset + self.CHUNK_SECONDS,
                                text=f"[STT window at {offset:.0f}s failed after retry: {detail}]",
                            )
                        )
            finally:
                for wav, _ in chunks:
                    wav.unlink(missing_ok=True)
            return out or [
                TranscriptSegment(start=0, end=1, text="[empty transcription]")
            ]

        from asyncio import to_thread

        return await to_thread(_run)


def _duration_sync(path: Path) -> float:
    """Blocking ffprobe duration probe (called inside the whisper worker thread)."""
    import subprocess

    proc = subprocess.run(
        [
            ffprobe_cmd(),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
    )
    try:
        return float(proc.stdout.decode().strip() or "0")
    except ValueError:
        return 0.0


def _split_wav_sync(path: Path, chunk_seconds: float) -> list[tuple[Path, float]]:
    """Split a wav into chunk_seconds-long pieces with ffmpeg; returns
    [(chunk_path, offset_seconds)] in order. Pieces land next to the source in
    the same temp dir, so the caller's cleanup removes them with it."""
    import subprocess

    pattern = path.with_name("chunk_%05d.wav")
    proc = subprocess.run(
        [
            ffmpeg_cmd(),
            "-y",
            "-i",
            str(path),
            "-f",
            "segment",
            "-segment_time",
            str(int(chunk_seconds)),
            "-c",
            "copy",
            str(pattern),
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        # Fall back to the whole file rather than failing the lesson.
        return [(path, 0.0)]
    pieces = sorted(path.parent.glob("chunk_*.wav"))
    return [(p, i * chunk_seconds) for i, p in enumerate(pieces)]


# STT providers exposed by /health and the settings page. Anything outside this
# list is rejected loudly instead of silently falling back to MockSTT.
KNOWN_STT_PROVIDERS = ("mock", "faster_whisper", "siliconflow")


class STTConfigError(RuntimeError):
    """Missing key or unknown provider — never fall back to mock."""


_STT_PLACEHOLDER_KEYS = {
    "-",
    "none",
    "null",
    "changeme",
    "your-key",
    "your_key_here",
    "your_api_key",
    "xxx",
    "todo",
    "replace-me",
}
_STT_MIN_KEY_LEN = 8


def _usable_stt_key(value: str | None) -> bool:
    raw = (value or "").strip()
    if len(raw) < _STT_MIN_KEY_LEN:
        return False
    return raw.lower() not in _STT_PLACEHOLDER_KEYS


class SiliconFlowSTT(STTProvider):
    """Cloud ASR via POST /v1/audio/transcriptions (file + model only)."""

    name = "siliconflow"

    def __init__(self) -> None:
        settings = get_settings()
        key = (os.environ.get("SILICONFLOW_API_KEY") or "").strip()
        if not _usable_stt_key(key):
            raise STTConfigError(
                "STT_PROVIDER=siliconflow 但 SILICONFLOW_API_KEY 为空或无效。"
                "请在 .env 填入密钥后重新导入，不会回退到 mock。"
            )
        self._key = key
        self._model = (
            getattr(settings, "stt_siliconflow_model", None) or "FunAudioLLM/SenseVoiceSmall"
        ).strip()
        base = (
            getattr(settings, "stt_siliconflow_base_url", None) or "https://api.siliconflow.cn/v1"
        ).rstrip("/")
        self._url = f"{base}/audio/transcriptions"

    async def transcribe(self, audio_path: Path) -> list[TranscriptSegment]:
        duration = await probe_duration(audio_path)
        filename = audio_path.name or "audio.wav"
        mime = "audio/wav" if filename.lower().endswith(".wav") else "application/octet-stream"
        data = audio_path.read_bytes()
        # Official API: multipart file + model only. language / response_format → 400.
        async with httpx.AsyncClient(timeout=360.0, trust_env=True) as client:
            resp = await client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}"},
                files={"file": (filename, data, mime)},
                data={"model": self._model},
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"SiliconFlow STT HTTP {resp.status_code}: {(resp.text or '')[:400]}")
        payload = resp.json()
        raw_segs = payload.get("segments") or []
        out: list[TranscriptSegment] = []
        for seg in raw_segs:
            if not isinstance(seg, dict):
                continue
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            out.append(
                TranscriptSegment(
                    start=float(seg.get("start") or 0.0),
                    end=float(seg.get("end") or duration or 0.0),
                    text=text,
                )
            )
        if out:
            return out
        text = str(payload.get("text") or "").strip()
        if not text:
            raise RuntimeError("SiliconFlow STT empty text (not a mock lesson); check the audio.")
        return [TranscriptSegment(start=0.0, end=max(float(duration or 0.0), 0.1), text=text)]


def get_stt() -> STTProvider:
    name = (get_settings().stt_provider or "mock").strip().lower()
    # Accept historical aliases, but only the canonical KNOWN_STT_PROVIDERS names
    # are surfaced to users.
    if name == "silicon_flow":
        name = "siliconflow"
    elif name == "faster-whisper":
        name = "faster_whisper"
    if name not in KNOWN_STT_PROVIDERS:
        raise STTConfigError(f"Unknown STT_PROVIDER {name!r}; choose one of {KNOWN_STT_PROVIDERS}")
    if name == "siliconflow":
        return SiliconFlowSTT()
    if name == "faster_whisper":
        return FasterWhisperSTT()
    return MockSTT()


@dataclass
class SegmentTranscript:
    """STT result plus an honest note about retries / mock provenance."""

    segments: list[TranscriptSegment]
    note: str
    failed_windows: int = 0
    total_windows: int = 1


async def transcribe_audio_resilient(
    stt: STTProvider,
    wav: Path,
    *,
    chunk_seconds: float = 300.0,
    attempts: int = 3,
) -> SegmentTranscript:
    """Transcribe audio, retrying failed time windows independently.

    A single bad STT/summarize window must not force a re-run of the whole
    recording. Failed windows are labelled in the transcript so the learner
    can see what is missing instead of getting a silent gap.
    """
    duration = await probe_duration(wav)
    cleanup: list[Path] = []
    if duration <= 0 or duration <= chunk_seconds * 1.2:
        windows: list[tuple[Path, float]] = [(wav, 0.0)]
    else:
        windows = await asyncio.to_thread(_split_wav_sync, wav, chunk_seconds)
        cleanup = [path for path, _ in windows if path != wav]

    all_segs: list[TranscriptSegment] = []
    failed = 0
    try:
        for index, (path, offset) in enumerate(windows, 1):
            last_exc: Exception | None = None
            succeeded = False
            for _attempt in range(max(1, attempts)):
                try:
                    segs = await stt.transcribe(path)
                    for seg in segs:
                        # Split pieces report timestamps relative to the piece.
                        shift = offset if path != wav else 0.0
                        all_segs.append(
                            TranscriptSegment(
                                start=float(seg.start) + shift,
                                end=float(seg.end) + shift,
                                text=seg.text,
                            )
                        )
                    succeeded = True
                    break
                except Exception as exc:
                    last_exc = exc
            if not succeeded:
                failed += 1
                end = offset + (chunk_seconds if duration <= 0 else min(chunk_seconds, max(0.0, duration - offset)))
                detail = str(last_exc)[:160] if last_exc else "unknown error"
                all_segs.append(
                    TranscriptSegment(
                        start=offset,
                        end=end,
                        text=f"[segment {index} transcription failed after {attempts} attempts: {detail}]",
                    )
                )
    finally:
        for path in cleanup:
            path.unlink(missing_ok=True)

    note = f"STT {stt.name} ({len(all_segs)} segments, {len(windows)} window(s))"
    if stt.name == "mock":
        note += " · mock STT (not a real lecture transcript)"
    if failed:
        note += f" · {failed}/{len(windows)} window(s) failed after retry"
    return SegmentTranscript(
        segments=all_segs,
        note=note,
        failed_windows=failed,
        total_windows=len(windows),
    )
