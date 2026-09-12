"""SiliconFlow STT: httpx is mocked — no network, no real key."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.services.stt import (
    KNOWN_STT_PROVIDERS,
    STTConfigError,
    SiliconFlowSTT,
    TranscriptSegment,
    get_stt,
    transcribe_audio_resilient,
)


@pytest.fixture(autouse=True)
def fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _enable_siliconflow(monkeypatch: pytest.MonkeyPatch, key: str = "sk-test-siliconflow-key") -> None:
    monkeypatch.setenv("STT_PROVIDER", "siliconflow")
    monkeypatch.setenv("SILICONFLOW_API_KEY", key)
    get_settings.cache_clear()


class _FakeResponse:
    def __init__(self, status_code: int, payload: object | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        if text:
            self.text = text
        elif payload is None:
            self.text = ""
        else:
            self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("not json")
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, headers=None, files=None, data=None) -> _FakeResponse:
        self.calls.append({"url": url, "headers": headers or {}, "data": data or {}, "files": files})
        return self._response


def _patch_client(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> _FakeClient:
    client = _FakeClient(response)

    def _factory(**_kwargs: object) -> _FakeClient:
        return client

    monkeypatch.setattr("app.services.stt.httpx.AsyncClient", _factory)
    return client


@pytest.mark.asyncio
async def test_text_only_response_maps_to_one_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_siliconflow(monkeypatch)
    wav = tmp_path / "lecture.wav"
    wav.write_bytes(b"RIFF....")

    async def fake_probe(_path: Path) -> float:
        return 42.5

    monkeypatch.setattr("app.services.stt.probe_duration", fake_probe)
    client = _patch_client(monkeypatch, _FakeResponse(200, {"text": "Bonjour, mesdames et messieurs."}))

    stt = get_stt()
    assert isinstance(stt, SiliconFlowSTT)
    assert stt.name == "siliconflow"
    segs = await stt.transcribe(wav)

    assert segs == [
        TranscriptSegment(start=0.0, end=42.5, text="Bonjour, mesdames et messieurs.")
    ]
    assert client.calls
    assert client.calls[0]["url"].endswith("/audio/transcriptions")
    assert client.calls[0]["url"].startswith("https://api.siliconflow.cn/v1")
    assert client.calls[0]["headers"]["Authorization"] == "Bearer sk-test-siliconflow-key"
    assert client.calls[0]["data"]["model"] == "FunAudioLLM/SenseVoiceSmall"
    assert "language" not in client.calls[0]["data"]


@pytest.mark.asyncio
async def test_segments_payload_is_used_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_siliconflow(monkeypatch)
    wav = tmp_path / "lecture.wav"
    wav.write_bytes(b"fake")
    _patch_client(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "text": "Guten Tag. Photosynthesis.",
                "segments": [
                    {"start": 0.0, "end": 1.5, "text": " Guten Tag. "},
                    {"start": 1.5, "end": 4.0, "text": "Photosynthesis."},
                    {"start": 4.0, "end": 5.0, "text": "   "},
                ],
            },
        ),
    )

    segs = await SiliconFlowSTT().transcribe(wav)
    assert [(s.start, s.end, s.text) for s in segs] == [
        (0.0, 1.5, "Guten Tag."),
        (1.5, 4.0, "Photosynthesis."),
    ]


@pytest.mark.asyncio
async def test_empty_text_is_an_error_not_a_mock_lesson(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_siliconflow(monkeypatch)
    wav = tmp_path / "silence.wav"
    wav.write_bytes(b"fake")
    _patch_client(monkeypatch, _FakeResponse(200, {"text": "   "}))

    with pytest.raises(RuntimeError, match="empty text"):
        await SiliconFlowSTT().transcribe(wav)


@pytest.mark.parametrize("key", ["", "   ", "xxx", "changeme", "none", "sk"])
def test_missing_or_placeholder_key_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setenv("STT_PROVIDER", "siliconflow")
    if key == "":
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    else:
        monkeypatch.setenv("SILICONFLOW_API_KEY", key)
    get_settings.cache_clear()

    with pytest.raises(STTConfigError, match="SILICONFLOW_API_KEY"):
        get_stt()


@pytest.mark.asyncio
async def test_http_401_is_a_readable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_siliconflow(monkeypatch)
    wav = tmp_path / "lecture.wav"
    wav.write_bytes(b"fake")
    _patch_client(
        monkeypatch,
        _FakeResponse(401, {"message": "Invalid token"}),
    )

    with pytest.raises(RuntimeError, match=r"HTTP 401.*Invalid token"):
        await SiliconFlowSTT().transcribe(wav)


@pytest.mark.asyncio
async def test_http_400_string_body_is_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_siliconflow(monkeypatch)
    wav = tmp_path / "lecture.wav"
    wav.write_bytes(b"fake")
    _patch_client(monkeypatch, _FakeResponse(400, text="file too large"))

    with pytest.raises(RuntimeError, match=r"HTTP 400.*file too large"):
        await SiliconFlowSTT().transcribe(wav)


@pytest.mark.asyncio
async def test_model_and_base_url_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_siliconflow(monkeypatch)
    monkeypatch.setenv("STT_SILICONFLOW_MODEL", "TeleAI/TeleSpeechASR")
    monkeypatch.setenv("STT_SILICONFLOW_BASE_URL", "https://api.siliconflow.com/v1/")
    get_settings.cache_clear()
    wav = tmp_path / "lecture.wav"
    wav.write_bytes(b"fake")

    async def fake_probe(_path: Path) -> float:
        return 1.0

    monkeypatch.setattr("app.services.stt.probe_duration", fake_probe)
    client = _patch_client(monkeypatch, _FakeResponse(200, {"text": "ok"}))

    await SiliconFlowSTT().transcribe(wav)
    assert client.calls[0]["data"]["model"] == "TeleAI/TeleSpeechASR"
    assert client.calls[0]["url"] == "https://api.siliconflow.com/v1/audio/transcriptions"


@pytest.mark.asyncio
async def test_resilient_windows_still_work_with_siliconflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_siliconflow(monkeypatch)
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"fake")

    async def fake_probe(_path: Path) -> float:
        return 10.0

    monkeypatch.setattr("app.services.stt.probe_duration", fake_probe)
    _patch_client(monkeypatch, _FakeResponse(200, {"text": "Hallo zusammen."}))

    result = await transcribe_audio_resilient(SiliconFlowSTT(), wav, chunk_seconds=300.0, attempts=2)
    assert result.failed_windows == 0
    assert result.segments[0].text == "Hallo zusammen."
    assert "siliconflow" in result.note
    assert "mock" not in result.note.lower()


def test_known_providers_include_siliconflow() -> None:
    assert KNOWN_STT_PROVIDERS == ("mock", "faster_whisper", "siliconflow")


@pytest.mark.asyncio
async def test_health_lists_siliconflow_among_stt_providers() -> None:
    from app.api.health import health

    data = await health()
    assert data["stt_provider"] == "mock"
    assert data["stt_providers"] == list(KNOWN_STT_PROVIDERS)
    assert "siliconflow" in data["stt_providers"]


def test_unknown_provider_does_not_silently_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_PROVIDER", "not-a-vendor")
    get_settings.cache_clear()
    with pytest.raises(STTConfigError, match="Unknown STT_PROVIDER"):
        get_stt()


def test_env_example_keeps_mock_default_and_documents_siliconflow() -> None:
    example = Path(__file__).resolve().parents[2] / ".env.example"
    text = example.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.startswith("STT_PROVIDER=")]
    assert lines == ["STT_PROVIDER=mock"]
    assert "siliconflow" in text
    assert "STT_SILICONFLOW_MODEL" in text
    assert "STT_SILICONFLOW_BASE_URL" in text
    assert "SILICONFLOW_API_KEY=" in text
