"""OCR for images and scanned (text-layer-less) PDFs.

Two interchangeable engines behind one interface:

* `RapidOcrEngine` — local ONNX inference. Free, offline, no API key. The
  Chinese+English model is bundled with the `rapidocr-onnxruntime` package; other
  languages are ~10 MB ONNX files fetched once into `backend/models/ocr/`.
* `VisionOcr` — sends the page image to a vision-capable LLM through the existing
  model router (`task="transcribe_image"`). Better on handwriting, dense tables
  and unusual layout; needs a vision model key (DashScope `qwen-vl-*`,
  OpenAI `gpt-4o*`, SiliconFlow `Qwen2.5-VL`, Ollama `llava`, …).

`OCR_PROVIDER=auto` (default) prefers the local engine so a fresh install works
with zero configuration, and falls back to a vision model when the local engine
is not installed.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from PIL import Image

from app.core.config import BACKEND_DIR, get_settings
from app.services.llm.base import ChatMessage, ImagePart

logger = logging.getLogger("app.ocr")

OCR_MODEL_DIR = BACKEND_DIR / "models" / "ocr"

# ONNX recognition models converted and published by the RapidAI team.
# `ch` (covers Chinese + English + digits) is already inside the pip package, so
# it is intentionally absent from this table.
_MODELSCOPE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.5.0/onnx/PP-OCRv4/rec"
LANG_MODEL_FILES: dict[str, str] = {
    "japan": "japan_PP-OCRv4_rec_infer.onnx",
    "korean": "korean_PP-OCRv4_rec_infer.onnx",
    "latin": "latin_PP-OCRv3_rec_infer.onnx",
    "cyrillic": "cyrillic_PP-OCRv3_rec_infer.onnx",
    "chinese_cht": "chinese_cht_PP-OCRv3_rec_infer.onnx",
    "arabic": "arabic_PP-OCRv4_rec_infer.onnx",
    "devanagari": "devanagari_PP-OCRv4_rec_infer.onnx",
}
BUNDLED_LANGS = ("ch", "en")

# Below this mean confidence the auto language mode retries with another model.
LOW_CONFIDENCE = 0.75

TRANSCRIBE_PROMPT = """You transcribe scanned study material for a learning app.

Rules:
- Output ONLY the transcription. No commentary, no translation, no code fences.
- Keep the original language exactly as printed. Japanese stays Japanese, Chinese stays Chinese.
- Preserve reading order and layout cues: put each heading, numbered item and list marker on its own line.
- In language textbooks keep the base sentence and any furigana / romanisation on the same line, separated by a space.
- Write [illegible] for anything you genuinely cannot read. Never guess.
- Do not summarise, explain or add anything that is not in the image."""


class OcrUnavailable(RuntimeError):
    """Raised when image input arrives but no engine can handle it."""


@dataclass
class OcrPage:
    page_number: int
    text: str
    confidence: float | None = None


@dataclass
class OcrResult:
    pages: list[OcrPage] = field(default_factory=list)
    provider: str = "none"
    model: str = ""
    language: str = ""
    note: str | None = None

    @property
    def is_empty(self) -> bool:
        return not any(page.text.strip() for page in self.pages)

    @property
    def mean_confidence(self) -> float | None:
        values = [p.confidence for p in self.pages if p.confidence is not None]
        return sum(values) / len(values) if values else None


# --------------------------------------------------------------------------
# image helpers
# --------------------------------------------------------------------------


def normalise_image(data: bytes, *, max_side: int | None = None) -> Image.Image:
    """Decode anything Pillow understands and flatten it to RGB.

    Scanned PDFs hand us CMYK JPEGs, palette PNGs and 1-bit bitmaps; every engine
    downstream assumes 3-channel RGB.
    """
    image = Image.open(io.BytesIO(data))
    if max_side:
        width, height = image.size
        scale = min(1.0, max_side / max(width, height))
        if scale < 1.0:
            image = image.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS
            )
    if image.mode != "RGB":
        image = image.convert("RGB")
    return image


def to_data_url(image: Image.Image, *, quality: int = 88) -> ImagePart:
    """Encode a page for a vision model. JPEG keeps the request well under the
    per-image payload caps that vendors enforce."""
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return ImagePart(data_url=f"data:image/jpeg;base64,{payload}", mime="image/jpeg")


def _group_lines(rows: list[tuple[list[list[float]], str, float]]) -> str:
    """Rebuild reading order from OCR boxes.

    Detectors return boxes in arbitrary order. Sorting purely by y breaks on
    pages with side-by-side columns and superscripts, so group into rows first
    (y-centre within a tolerance derived from the median box height) and only
    then order each row left-to-right.
    """
    if not rows:
        return ""
    enriched = []
    for box, text, score in rows:
        ys = [point[1] for point in box]
        xs = [point[0] for point in box]
        enriched.append(
            {
                "top": min(ys),
                "bottom": max(ys),
                "left": min(xs),
                "y": sum(ys) / len(ys),
                "height": max(1.0, max(ys) - min(ys)),
                "text": text,
            }
        )
    heights = sorted(item["height"] for item in enriched)
    tolerance = max(6.0, heights[len(heights) // 2] * 0.6)
    enriched.sort(key=lambda item: (item["y"], item["left"]))

    lines: list[list[dict]] = []
    for item in enriched:
        if lines and abs(item["y"] - lines[-1][0]["y"]) <= tolerance:
            lines[-1].append(item)
        else:
            lines.append([item])

    out: list[str] = []
    for line in lines:
        line.sort(key=lambda item: item["left"])
        out.append(" ".join(item["text"] for item in line))
    return "\n".join(out)


def _download_model(url: str, target: Path) -> bool:
    """Best-effort one-off model fetch. Never raises: a failed download simply
    leaves the language unavailable and the caller degrades."""
    if target.exists():
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        logger.info("downloading OCR recognition model -> %s", target.name)
        with urllib.request.urlopen(url, timeout=180) as response:  # noqa: S310 (fixed host)
            tmp.write_bytes(response.read())
        tmp.replace(target)
        return True
    except Exception as exc:
        logger.warning("could not download OCR model %s: %s", url, exc)
        tmp.unlink(missing_ok=True)
        return False


# --------------------------------------------------------------------------
# engines
# --------------------------------------------------------------------------


class OcrEngine(Protocol):
    name: str

    def describe(self) -> str: ...

    async def transcribe(
        self,
        images: list[tuple[int, bytes]],
        *,
        user_id=None,
        source_id=None,
    ) -> OcrResult: ...


class NullOcr:
    """Refuses politely, with instructions, instead of producing empty content."""

    name = "none"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def describe(self) -> str:
        return f"none ({self.reason})"

    async def transcribe(self, images, *, user_id=None, source_id=None) -> OcrResult:
        raise OcrUnavailable(self.reason)


class RapidOcrEngine:
    """Local ONNX OCR. No API key, no network once the language model is cached."""

    name = "rapidocr"

    _instance = None
    _instance_lang: str | None = None
    _lock = threading.Lock()

    @staticmethod
    def importable() -> bool:
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except Exception:
            return False
        return True

    def describe(self) -> str:
        return f"rapidocr (local, langs={BUNDLED_LANGS + tuple(LANG_MODEL_FILES)})"

    # ---- model plumbing

    @classmethod
    def _model_path(cls, lang: str) -> str | None:
        """Path to the recognition model for `lang`, or None when unavailable."""
        if lang in BUNDLED_LANGS:
            return None  # bundled in the package
        filename = LANG_MODEL_FILES.get(lang)
        if not filename:
            return None
        target = OCR_MODEL_DIR / filename
        if target.exists():
            return str(target)
        if _download_model(f"{_MODELSCOPE}/{filename}", target):
            return str(target)
        return None

    @classmethod
    def available_languages(cls) -> list[str]:
        langs = list(BUNDLED_LANGS)
        for lang, filename in LANG_MODEL_FILES.items():
            if (OCR_MODEL_DIR / filename).exists():
                langs.append(lang)
        return langs

    @classmethod
    def _engine(cls, lang: str):
        with cls._lock:
            if cls._instance is not None and cls._instance_lang == lang:
                return cls._instance
            from rapidocr_onnxruntime import RapidOCR

            kwargs: dict = {}
            model_path = cls._model_path(lang)
            if model_path:
                kwargs["rec_model_path"] = model_path
            cls._instance = RapidOCR(**kwargs)
            cls._instance_lang = lang
            logger.info("loaded OCR recognition model for lang=%s (%s)", lang, model_path or "bundled")
            return cls._instance

    def _run_sync(self, image: Image.Image, lang: str) -> tuple[str, float | None]:
        import numpy as np

        engine = self._engine(lang)
        result, _elapsed = engine(np.array(image))
        if not result:
            return "", None
        rows = [
            (row[0], str(row[1]), float(row[2]))
            for row in result
            if len(row) >= 3 and str(row[1]).strip()
        ]
        text = _group_lines(rows)
        scores = [row[2] for row in rows]
        return text, (sum(scores) / len(scores) if scores else None)

    # ---- protocol

    async def transcribe(self, images, *, user_id=None, source_id=None) -> OcrResult:
        settings = get_settings()
        requested = (settings.ocr_lang or "auto").strip().lower()
        available = self.available_languages()

        candidates: list[str]
        if requested == "auto":
            # Try the bundled model, then re-run in Japanese when it looks wrong.
            # Kana get shredded by the Chinese model ("" -> "。"), which shows up
            # as low mean confidence, so confidence is a usable trigger.
            candidates = ["ch", "japan"]
            if "japan" not in available:
                candidates = ["ch"]
        elif requested in available:
            candidates = [requested]
        else:
            candidates = ["ch"]

        pages: list[OcrPage] = []
        used_lang = candidates[0]
        best_conf: float | None = None
        notes: list[str] = []

        for page_number, data in images:
            try:
                image = normalise_image(data)
            except Exception as exc:
                pages.append(OcrPage(page_number, "", None))
                notes.append(f"page {page_number}: unreadable image ({type(exc).__name__})")
                continue

            best_text, best_score = "", None
            chosen = candidates[0]
            for lang in candidates:
                try:
                    text, score = await asyncio.to_thread(self._run_sync, image, lang)
                except Exception as exc:
                    notes.append(f"page {page_number}: {lang} model failed ({type(exc).__name__})")
                    continue
                if score is not None and (best_score is None or score > best_score):
                    best_text, best_score, chosen = text, score, lang
                elif best_score is None:
                    best_text, best_score, chosen = text, score, lang
                # Good enough — no need to pay for a second model pass.
                if score is not None and score >= LOW_CONFIDENCE:
                    break

            if chosen == "japan":
                used_lang = "japan"
            if best_score is not None and (best_conf is None or best_score < best_conf):
                best_conf = best_score
            pages.append(OcrPage(page_number, best_text, best_score))

        return OcrResult(
            pages=pages,
            provider=self.name,
            model=f"PP-OCRv4/{used_lang}",
            language=used_lang,
            note="; ".join(notes) if notes else None,
        )


class VisionOcr:
    """Delegate transcription to a vision-capable LLM through the model router."""

    name = "vision"

    def __init__(self, router) -> None:
        self.router = router

    def describe(self) -> str:
        try:
            provider = self.router.provider_for("transcribe_image")
            return f"vision ({provider.name}/{provider.model_for('transcribe_image')})"
        except Exception as exc:  # config problem surfaces on first use
            return f"vision (unavailable: {exc})"

    async def transcribe(self, images, *, user_id=None, source_id=None) -> OcrResult:
        """Read every page concurrently, bounded, and keep them in page order.

        This loop used to run one page at a time: a 200-page scanned textbook
        meant 200 sequential vision calls, which is most of the "解析很慢"
        experience. With a small pool the same book finishes several times
        faster, and one bad page still only costs its own row.
        """
        settings = get_settings()
        max_side = max(320, int(settings.ocr_vision_max_side or 1800))
        limit = max(1, int(getattr(settings, "ocr_concurrency", 3) or 3))
        pages: list[OcrPage] = []
        notes: list[str] = []
        provider_name = ""
        model_name = ""

        async def read_page(page_number: int, data: bytes) -> OcrPage:
            nonlocal provider_name, model_name
            try:
                image = normalise_image(data, max_side=max_side)
            except Exception as exc:
                notes.append(f"page {page_number}: unreadable image ({type(exc).__name__})")
                return OcrPage(page_number, "", None)
            message = ChatMessage.user_with_image(
                f"{TRANSCRIBE_PROMPT}\n\nThis is page {page_number}.", to_data_url(image)
            )
            result = await self.router.complete(
                task="transcribe_image",
                messages=[message],
                json_mode=False,
                user_id=user_id,
                source_id=source_id,
            )
            provider_name = result.provider
            model_name = result.model
            return OcrPage(page_number, (result.content or "").strip(), None)

        semaphore = asyncio.Semaphore(limit)

        async def guarded(page_number: int, data: bytes) -> OcrPage:
            async with semaphore:
                return await read_page(page_number, data)

        results = await asyncio.gather(
            *(guarded(number, blob) for number, blob in images), return_exceptions=True
        )
        for (page_number, _blob), outcome in zip(images, results, strict=False):
            if isinstance(outcome, BaseException):
                notes.append(f"page {page_number}: OCR failed ({type(outcome).__name__}: {outcome})")
                pages.append(OcrPage(page_number, "", None))
            else:
                pages.append(outcome)

        return OcrResult(
            pages=pages,
            provider=provider_name or "vision",
            model=model_name,
            language="auto",
            note="; ".join(notes) if notes else None,
        )


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


def vision_route_available(router) -> tuple[bool, str]:
    """Whether `transcribe_image` resolves to a provider that really has a vision model."""
    try:
        provider = router.provider_for("transcribe_image")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if not provider.supports_vision():
        return False, (
            f'provider "{provider.name}" has no "transcribe_image" model in providers.yaml'
        )
    if provider.name == "mock":
        return False, "only the mock provider is configured (it cannot read images)"
    return True, ""


def get_ocr(router=None) -> OcrEngine:
    """Pick an engine from OCR_PROVIDER, degrading with an actionable message."""
    settings = get_settings()
    mode = (settings.ocr_provider or "auto").strip().lower()

    if mode == "none":
        return NullOcr(
            "OCR_PROVIDER=none. Set OCR_PROVIDER=rapidocr (offline) or OCR_PROVIDER=vision "
            "with a vision model key to read images and scanned PDFs."
        )
    if mode == "vision":
        if router is None:
            return NullOcr("OCR_PROVIDER=vision needs a database session to call the model router.")
        ok, why = vision_route_available(router)
        if not ok:
            return NullOcr(f"OCR_PROVIDER=vision but the vision route is not ready -> {why}")
        return VisionOcr(router)
    if mode == "rapidocr":
        if not RapidOcrEngine.importable():
            return NullOcr(
                "OCR_PROVIDER=rapidocr but the engine is not installed. Run: "
                "pip install rapidocr-onnxruntime"
            )
        return RapidOcrEngine()

    # ---- auto
    if RapidOcrEngine.importable():
        return RapidOcrEngine()
    if router is not None:
        ok, _why = vision_route_available(router)
        if ok:
            return VisionOcr(router)
    return NullOcr(
        "no OCR engine available. Either install the offline engine "
        "(pip install rapidocr-onnxruntime) or add a vision-capable API key and set "
        "LLM_PROVIDER_VISION (e.g. dashscope for qwen-vl-max)."
    )


def describe_ocr(router=None) -> dict:
    """Diagnostics for "why can't it read my book?".

    Explains both halves of the decision: which engine actually gets used for an
    image / scanned-PDF upload right now, and whether the vision-LLM fallback is
    armed. `ready: false` means those uploads will fail with `reason`.
    """
    settings = get_settings()
    engine = get_ocr(router)
    if router is not None:
        vision_ok, vision_why = vision_route_available(router)
    else:
        vision_ok, vision_why = False, "no model router available in this context"

    summary: dict = {
        "mode": settings.ocr_provider,
        "requested_language": settings.ocr_lang,
        "engine": getattr(engine, "name", type(engine).__name__),
        "vision_fallback_ready": vision_ok,
        "vision_fallback_reason": vision_why or None,
    }
    if isinstance(engine, RapidOcrEngine):
        summary["installed_languages"] = engine.available_languages()
    if isinstance(engine, NullOcr):
        summary["ready"] = False
        summary["reason"] = engine.reason
    else:
        summary["ready"] = True
        summary["reason"] = None
    return summary
