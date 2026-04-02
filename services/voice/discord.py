"""Discord voice message delivery via raw HTTP API.

Sends pre-generated audio files as Discord voice messages using the
3-step attachment upload flow. No discord.py dependency — pure HTTP.
"""

import base64
import logging
import math
import subprocess
from pathlib import Path

import aiohttp

log = logging.getLogger("voice.discord")

DISCORD_API_BASE = "https://discord.com/api/v10"


def _load_discord_token(agent_home: Path) -> str | None:
    key_path = agent_home / "credentials" / "DISCORD_BOT_TOKEN"
    if key_path.exists():
        return key_path.read_text().strip()
    return None


def _get_audio_duration(audio_path: Path) -> float:
    """Get duration of an audio file in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        pass
    # Fallback: estimate from file size (Opus ~12 kB/s for voice)
    return max(1.0, audio_path.stat().st_size / 12000)


def _generate_waveform(duration_secs: float) -> str:
    """Generate a plausible base64-encoded waveform for Discord voice messages.

    Discord expects up to 256 bytes of amplitude data.
    """
    num_samples = min(256, max(1, int(duration_secs * 10)))
    samples = []
    for i in range(num_samples):
        t = i / max(num_samples - 1, 1)
        val = int(
            80
            + 60 * math.sin(t * math.pi)
            + 40 * math.sin(t * 17.3)
        )
        samples.append(max(0, min(255, val)))
    return base64.b64encode(bytes(samples)).decode("ascii")


async def send_voice_message(
    channel_id: int | str,
    audio_path: str | Path,
    agent_home: Path | str | None = None,
) -> bool:
    """Send an audio file as a Discord voice message.

    Uses raw Discord HTTP API (3-step process):
    1. Request an upload URL for the attachment
    2. Upload the audio file
    3. Post the message with voice message flags
    """
    if agent_home is None:
        agent_home = Path.home()
    agent_home = Path(agent_home)

    bot_token = _load_discord_token(agent_home)
    if not bot_token:
        log.error("No Discord bot token — cannot send voice message")
        return False

    audio_path = Path(audio_path)
    if not audio_path.exists():
        log.error("Audio file not found: %s", audio_path)
        return False

    file_size = audio_path.stat().st_size
    duration = _get_audio_duration(audio_path)
    waveform = _generate_waveform(duration)
    channel_id = str(channel_id)

    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            # Step 1: Request upload URL
            attach_url = f"{DISCORD_API_BASE}/channels/{channel_id}/attachments"
            attach_payload = {
                "files": [{
                    "filename": "voice-message.ogg",
                    "file_size": file_size,
                    "id": "0",
                }]
            }

            async with session.post(attach_url, headers=headers, json=attach_payload) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    log.error("Failed to get upload URL (%d): %s", resp.status, error)
                    return False
                attach_resp = await resp.json()

            attachments = attach_resp.get("attachments", [])
            if not attachments:
                log.error("No attachment slots returned from Discord")
                return False

            upload_url = attachments[0]["upload_url"]
            upload_filename = attachments[0]["upload_filename"]

            # Step 2: Upload the audio file
            audio_data = audio_path.read_bytes()
            async with session.put(
                upload_url, headers={"Content-Type": "audio/ogg"}, data=audio_data
            ) as resp:
                if resp.status not in (200, 204):
                    error = await resp.text()
                    log.error("Failed to upload audio (%d): %s", resp.status, error)
                    return False

            # Step 3: Post the voice message
            msg_url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
            msg_payload = {
                "flags": 8192,  # IS_VOICE_MESSAGE
                "attachments": [{
                    "id": "0",
                    "filename": "voice-message.ogg",
                    "uploaded_filename": upload_filename,
                    "duration_secs": round(duration, 2),
                    "waveform": waveform,
                }],
            }

            async with session.post(msg_url, headers=headers, json=msg_payload) as resp:
                if resp.status == 200:
                    log.info(
                        "Sent voice message to channel %s (%.1fs, %d bytes)",
                        channel_id, duration, file_size,
                    )
                    return True
                else:
                    error = await resp.text()
                    log.error("Failed to post voice message (%d): %s", resp.status, error)
                    return False

    except Exception:
        log.exception("Failed to send voice message to channel %s", channel_id)
        return False
