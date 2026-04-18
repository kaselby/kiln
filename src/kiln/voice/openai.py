"""OpenAI voice backends — Whisper STT + OpenAI TTS.

Ported from voice.py with ElevenLabs stripped.
"""

import logging
from pathlib import Path

import aiohttp

from .base import STTBackend, TTSBackend

log = logging.getLogger("voice.openai")

DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_TTS_VOICE = "marin"


def _load_api_key(credentials_dir: Path) -> str | None:
    key_path = credentials_dir / "OPENAI_API_KEY"
    if key_path.exists():
        return key_path.read_text().strip()
    return None


class WhisperSTT(STTBackend):
    """OpenAI Whisper speech-to-text."""

    def __init__(self, credentials_dir: Path):
        self._credentials_dir = credentials_dir

    async def transcribe(self, audio_path: Path, language: str | None = None) -> str | None:
        api_key = _load_api_key(self._credentials_dir)
        if not api_key:
            log.error("No OpenAI API key — cannot transcribe")
            return None

        audio_path = Path(audio_path)
        if not audio_path.exists():
            log.error("Audio file not found: %s", audio_path)
            return None

        url = "https://api.openai.com/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            async with aiohttp.ClientSession() as session:
                data = aiohttp.FormData()
                data.add_field(
                    "file",
                    audio_path.read_bytes(),
                    filename=audio_path.name,
                    content_type="audio/ogg",
                )
                data.add_field("model", "whisper-1")
                data.add_field("response_format", "text")
                if language:
                    data.add_field("language", language)

                async with session.post(url, headers=headers, data=data) as resp:
                    if resp.status == 200:
                        transcript = (await resp.text()).strip()
                        log.info("Transcribed %s (%d chars)", audio_path.name, len(transcript))
                        return transcript
                    else:
                        error_body = await resp.text()
                        log.error("Whisper API error %d: %s", resp.status, error_body)
                        return None
        except Exception:
            log.exception("Failed to transcribe %s", audio_path)
            return None


class OpenAITTS(TTSBackend):
    """OpenAI text-to-speech."""

    def __init__(self, credentials_dir: Path, model: str = DEFAULT_TTS_MODEL):
        self._credentials_dir = credentials_dir
        self._model = model

    async def synthesize(
        self,
        text: str,
        output_path: Path,
        voice: str | None = None,
        instructions: str | None = None,
    ) -> Path | None:
        api_key = _load_api_key(self._credentials_dir)
        if not api_key:
            log.error("No OpenAI API key — cannot generate speech")
            return None

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        url = "https://api.openai.com/v1/audio/speech"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "input": text,
            "voice": voice or DEFAULT_TTS_VOICE,
            "response_format": "opus",  # OGG/Opus — required for Discord voice
        }
        if instructions and self._model == "gpt-4o-mini-tts":
            payload["instructions"] = instructions

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        audio_data = await resp.read()
                        output_path.write_bytes(audio_data)
                        log.info("Generated speech: %s (%d bytes)", output_path.name, len(audio_data))
                        return output_path
                    else:
                        error_body = await resp.text()
                        log.error("TTS API error %d: %s", resp.status, error_body)
                        return None
        except Exception:
            log.exception("Failed to generate speech")
            return None


# ---------------------------------------------------------------------------
# Convenience functions (for use by gateway and tools)
# ---------------------------------------------------------------------------

async def transcribe(audio_path: str | Path, credentials_dir: Path | str | None = None) -> str | None:
    """Transcribe audio using Whisper. Convenience wrapper."""
    if credentials_dir is None:
        credentials_dir = Path.home() / ".beth" / "credentials"
    return await WhisperSTT(Path(credentials_dir)).transcribe(Path(audio_path))


async def generate_speech(
    text: str,
    output_path: str | Path,
    credentials_dir: Path | str | None = None,
    voice: str = DEFAULT_TTS_VOICE,
    model: str = DEFAULT_TTS_MODEL,
    instructions: str | None = None,
) -> Path | None:
    """Generate speech using OpenAI TTS. Convenience wrapper."""
    if credentials_dir is None:
        credentials_dir = Path.home() / ".beth" / "credentials"
    tts = OpenAITTS(Path(credentials_dir), model=model)
    return await tts.synthesize(text, Path(output_path), voice=voice, instructions=instructions)
