"""Abstract base classes for STT and TTS backends."""

from abc import ABC, abstractmethod
from pathlib import Path


class STTBackend(ABC):
    """Speech-to-text backend."""

    @abstractmethod
    async def transcribe(
        self, audio_path: Path, language: str | None = None
    ) -> str | None:
        """Transcribe an audio file to text.

        Returns transcript text, or None on failure.
        """
        ...


class TTSBackend(ABC):
    """Text-to-speech backend."""

    @abstractmethod
    async def synthesize(
        self, text: str, output_path: Path, voice: str | None = None
    ) -> Path | None:
        """Generate speech audio from text.

        Returns path to generated audio file, or None on failure.
        """
        ...
