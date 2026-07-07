"""Transcribes voice-note attachments (e.g. a dictated call recap emailed to the agent
inbox) via OpenAI's Whisper API, so a voice memo flows through the same note/interaction
pipeline as a typed email."""
from __future__ import annotations
import logging
import os

import httpx

logger = logging.getLogger(__name__)

WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"


async def transcribe(content: bytes, filename: str) -> str | None:
    """Transcribes one audio attachment. Returns None on failure (logged, not raised) —
    a failed transcription shouldn't drop the whole email; other attachments/body text
    should still get processed."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("crm_voice: OPENAI_API_KEY not set, skipping transcription")
        return None
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                WHISPER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                data={"model": "whisper-1"},
                files={"file": (filename, content)},
            )
            resp.raise_for_status()
            return (resp.json().get("text") or "").strip() or None
    except Exception as e:
        logger.error(f"crm_voice: transcription failed for {filename}: {e}")
        return None


async def transcribe_attachments(attachments: list[dict]) -> str | None:
    """Transcribes every audio attachment on a message and joins them into one block of
    text. Returns None if none transcribed successfully."""
    transcripts = []
    for att in attachments:
        text = await transcribe(att["content"], att["filename"])
        if text:
            transcripts.append(text)
    return "\n\n".join(transcripts) if transcripts else None
