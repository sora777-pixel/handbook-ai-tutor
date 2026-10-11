"""Resolve the local ffmpeg/ffprobe binaries.

Video ingest shells out to ffmpeg (audio extract) and ffprobe (duration).
The Windows launcher downloads a copy into ``tools\\ffmpeg\\bin`` and prepends
that folder for the session — it must never be installed onto the system PATH.

Python still has to find the tools when uvicorn is started by hand, when tests
run, and when CreateProcess would otherwise raise ``WinError 2``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.core.config import REPO_ROOT


class FFmpegMissing(RuntimeError):
    """ffmpeg/ffprobe is not in tools/ffmpeg/bin and not on PATH."""


def _windows() -> bool:
    return os.name == "nt"


def bundled_bin_dir() -> Path:
    return REPO_ROOT / "tools" / "ffmpeg" / "bin"


def _names(tool: str) -> list[str]:
    if _windows():
        return [f"{tool}.exe", tool]
    return [tool, f"{tool}.exe"]


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    for key in ("FFMPEG_DIR", "FFMPEG_BIN_DIR"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            dirs.append(Path(raw))
    dirs.append(bundled_bin_dir())
    return dirs


def resolve_ffmpeg_tool(tool: str) -> str:
    """Absolute path to ``ffmpeg`` or ``ffprobe``.

    Raises :class:`FFmpegMissing` with a bilingual, actionable message.
    """
    if tool not in {"ffmpeg", "ffprobe"}:
        raise ValueError(f"unsupported tool: {tool}")

    for directory in _candidate_dirs():
        for name in _names(tool):
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)

    for name in _names(tool):
        found = shutil.which(name)
        if found:
            return found

    bundled = bundled_bin_dir()
    raise FFmpegMissing(
        "未找到 ffmpeg/ffprobe，无法从视频提取音频。"
        "请把 ffmpeg.exe 与 ffprobe.exe 放到项目的 tools\\ffmpeg\\bin "
        f"（当前探测目录：{bundled}）。"
        "双击 start.bat 会尝试自动下载到该目录，然后重新导入视频。"
        "不要把 ffmpeg 加到系统 PATH。"
        " ffmpeg/ffprobe not found; put the binaries in tools/ffmpeg/bin "
        f"(probed {bundled}) or set FFMPEG_DIR. start.bat downloads them there. "
        "Do not install ffmpeg on the system PATH."
    )


def ffmpeg_cmd() -> str:
    return resolve_ffmpeg_tool("ffmpeg")


def ffprobe_cmd() -> str:
    return resolve_ffmpeg_tool("ffprobe")
