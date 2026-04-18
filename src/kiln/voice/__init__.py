"""Kiln Voice Service — STT and TTS with pluggable backends."""

from .openai import generate_speech, transcribe
from .discord import send_voice_message

__all__ = ["generate_speech", "transcribe", "send_voice_message"]
