"""Voice memo + image understanding for inbound SMS/iMessage attachments.

The third instance of a pattern this project already uses twice:
_maybe_read_inbound_pdf (server.py) and _pick_contextual_reaction
(server.py) are both isolated, single-purpose, timeout-guarded model calls
that turn one thing (a PDF, a message's vibe) into a small piece of text
and hand it back, degrading to a safe default on ANY failure rather than
ever risking the real reply. This module adds the same shape for two more
inbound media kinds: a photo MMS/iMessage attachment, and an iMessage voice
memo.

Deliberately NOT a swap of the main orchestrator's model to something
multimodal -- see config.py's own comment above MEDIA_UNDERSTANDING_ENABLED
for the full reasoning (deepagents' harness-profile tool-stripping is keyed
off every agent being a ChatOpenAI instance; risking that, and paying a
different model's latency on every plain-text message, isn't worth it for
a capability only a minority of messages need). Instead: one bounded
config.build_model() call per attachment, using
config.MEDIA_UNDERSTANDING_MODEL_NAME (google/gemini-2.5-flash via
OpenRouter by default -- the same model+routing STAGEHAND_MODEL already
uses in production for Stagehand's own vision calls, so this is a proven
combination in this codebase, not a new integration).

Both describe_inbound_image and transcribe_inbound_audio return None on
literally any failure (download error, oversized file, timeout, API
error, empty/garbage model reply) -- see each function's own docstring for
why the caller (server.py's _maybe_understand_inbound_media) treats a
failed image differently from a failed audio transcription.
"""
from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import tempfile
from enum import Enum

import httpx

from . import config, console

# Extensions/content-types Gemini 2.5 Flash (via OpenRouter) accepts
# natively as inline image/audio content blocks -- anything audio NOT in
# this set gets transcoded by ffmpeg first (see _ensure_supported_audio
# below). Deliberately conservative, documented lists (not "whatever
# ffprobe reports"): a format guess that's wrong just means an unnecessary
# transcode, never a silent miscategorization.
_IMAGE_CONTENT_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/heic", "image/heif")
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif")
_AUDIO_CONTENT_TYPES = (
    "audio/wav", "audio/x-wav", "audio/mp3", "audio/mpeg", "audio/aiff", "audio/x-aiff",
    "audio/aac", "audio/ogg", "audio/flac", "audio/x-flac", "audio/mp4", "audio/x-m4a",
)
_AUDIO_EXTENSIONS = (".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac", ".m4a", ".caf")
# Formats OpenRouter/Gemini accept directly in an input_audio block --
# anything else detected (currently just .caf, iMessage's own voice-memo
# container) gets transcoded to .m4a first.
_NATIVELY_SUPPORTED_AUDIO_FORMATS = {"wav", "mp3", "aiff", "aac", "ogg", "flac", "m4a"}


class AttachmentKind(str, Enum):
    """What server.py's _maybe_understand_inbound_media should do with a
    downloaded attachment. PDF is intentionally NOT a member here --
    _maybe_read_inbound_pdf already owns that classification+handling
    end-to-end and stays completely untouched; this enum only covers the
    two NEW kinds this module adds."""

    IMAGE = "image"
    AUDIO = "audio"
    UNKNOWN = "unknown"


def classify_attachment(content_type: str, media_url: str, data: bytes) -> AttachmentKind:
    """Best-effort classification of a downloaded attachment, checked in
    order: the CDN's Content-Type header, then the URL's file extension,
    then (for images only) magic bytes -- iPhones frequently send HEIC
    photos and Sendblue's CDN Content-Type for those has been unreliable
    in the wild, so a bare header/extension guess isn't trusted alone for
    that one case. Never raises; an attachment that matches nothing here
    is UNKNOWN, and the caller's job (server.py) is to fall through to
    _maybe_read_inbound_pdf's own PDF-or-nothing check, same as today."""
    content_type = (content_type or "").lower()
    url_lower = (media_url or "").lower()

    if any(ct in content_type for ct in _IMAGE_CONTENT_TYPES) or url_lower.endswith(_IMAGE_EXTENSIONS):
        return AttachmentKind.IMAGE
    if any(ct in content_type for ct in _AUDIO_CONTENT_TYPES) or url_lower.endswith(_AUDIO_EXTENSIONS):
        return AttachmentKind.AUDIO

    # Magic-byte fallback for images specifically: HEIC/HEIF containers are
    # ISO-BMFF ("ftyp" box at offset 4 with a heic/heif/mif1/msf1 brand),
    # JPEG starts with \xff\xd8, PNG with the 8-byte PNG signature, WEBP is
    # a RIFF container with a "WEBP" fourcc at offset 8. Checked only when
    # the header/extension didn't already resolve it, so this never
    # overrides an explicit, correct Content-Type.
    if data[:2] == b"\xff\xd8":
        return AttachmentKind.IMAGE
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return AttachmentKind.IMAGE
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return AttachmentKind.IMAGE
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"heif", b"mif1", b"msf1"):
        return AttachmentKind.IMAGE
    # CAF (iMessage voice memo container) magic: "caff" at offset 0.
    if data[:4] == b"caff":
        return AttachmentKind.AUDIO
    # M4A voice container magic: "ftyp" box at offset 4 with M4A brand
    if data[4:8] == b"ftyp" and data[8:12] in (b"M4A ", b"m4a "):
        return AttachmentKind.AUDIO

    return AttachmentKind.UNKNOWN


async def download_attachment(media_url: str) -> tuple[str, bytes] | None:
    """Shared download helper -- same timeout/error-handling shape as
    _maybe_read_inbound_pdf's own inline download in server.py. Returns
    (content_type, raw_bytes), or None if the fetch itself failed (a flaky
    CDN must never break the whole inbound turn)."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(media_url)
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        console.system(f"[media_understanding] couldn't download {media_url!r}: {e}")
        return None
    return resp.headers.get("content-type", ""), resp.content


def _guess_image_mime(content_type: str, media_url: str) -> str:
    content_type = (content_type or "").lower()
    if content_type.startswith("image/"):
        return content_type
    guess, _ = mimetypes.guess_type(media_url)
    return guess or "image/jpeg"


async def describe_inbound_image(
    media_url: str, *, _prefetched: tuple[str, bytes] | None = None,
) -> str | None:
    """Downloads `media_url` and, if it's under config.MAX_IMAGE_READ_BYTES,
    asks config.MEDIA_UNDERSTANDING_MODEL_NAME for a concise, factual
    description via an OpenAI-style image_url content block -- the exact
    same shape tools/stagehand_tools.py's _build_stagehand_openrouter_
    callback already sends for Stagehand's own vision calls, just built by
    hand here instead of through Stagehand.

    Returns the description text, or None on ANY failure (download error,
    oversized file, timeout, API error, empty reply). None is a normal,
    expected outcome here -- server.py's caller treats a captionless image
    that couldn't be described the same tolerant way it already treats a
    captionless plain photo MMS today (see config.py's own comment on the
    deliberately asymmetric image-vs-audio fallback contract).

    `_prefetched`, when given, is an already-downloaded (content_type,
    data) pair -- server.py's dispatcher already had to download the
    attachment once to classify it, so this skips a second, redundant
    network round-trip for the common case where a message is fielded
    through _maybe_understand_inbound_media. Calling this directly with
    just media_url (e.g. from a test, or any future caller that hasn't
    already downloaded the attachment) still works exactly as before."""
    downloaded = _prefetched if _prefetched is not None else await download_attachment(media_url)
    if downloaded is None:
        return None
    content_type, data = downloaded

    if len(data) > config.MAX_IMAGE_READ_BYTES:
        limit_mb = config.MAX_IMAGE_READ_BYTES / (1024 * 1024)
        console.system(f"[media_understanding] image too large ({len(data)} bytes, limit {limit_mb:.0f}MB)")
        return None

    mime = _guess_image_mime(content_type, media_url)
    b64 = base64.b64encode(data).decode("ascii")

    try:
        model = config.build_model(
            config.MEDIA_UNDERSTANDING_MODEL_NAME,
            api_key=config.api_key_for_agent("media_understanding"),
        )
        result = await asyncio.wait_for(
            model.ainvoke([
                {
                    "role": "system",
                    "content": (
                        "You describe an image sent over text message, concisely and "
                        "factually, for someone who can't see it. Mention any visible "
                        "text, people, objects, and setting that seem relevant. Keep it "
                        "to 2-3 sentences."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                },
            ]),
            timeout=config.MEDIA_UNDERSTANDING_TIMEOUT_SECONDS,
        )
        text = (getattr(result, "content", None) or "").strip()
    except Exception as e:  # noqa: BLE001 - a failed description must never break the inbound turn
        console.system(f"[media_understanding] image description failed: {e}")
        return None

    if not text:
        return None
    return f"[The user just sent a photo over text. What it shows:]\n{text}"


async def _ensure_supported_audio(data: bytes, media_url: str) -> tuple[bytes, str] | None:
    """Returns (possibly-transcoded bytes, format string OpenRouter
    accepts). Transcodes via ffmpeg ONLY when the detected format isn't
    already in _NATIVELY_SUPPORTED_AUDIO_FORMATS (currently just iMessage's
    .caf voice memo container) -- skipping the subprocess entirely for the
    common case keeps this cheap. Returns None if the format can't be
    determined or the transcode itself fails/times out; the caller treats
    that as a transcription failure like any other."""
    url_lower = (media_url or "").lower()
    detected_format = None
    for ext in _AUDIO_EXTENSIONS:
        if url_lower.endswith(ext):
            detected_format = ext.lstrip(".")
            break
    if detected_format is None and data[:4] == b"caff":
        detected_format = "caf"
    if detected_format is None:
        # Default guess for a URL with no recognizable extension/magic --
        # m4a is iMessage's other common voice-memo container.
        detected_format = "m4a"

    if detected_format in _NATIVELY_SUPPORTED_AUDIO_FORMATS:
        return data, detected_format

    # Needs a transcode (currently only ever "caf" reaches here).
    with tempfile.TemporaryDirectory() as tmp:
        src_path = os.path.join(tmp, f"in.{detected_format}")
        dst_path = os.path.join(tmp, "out.m4a")
        with open(src_path, "wb") as f:
            f.write(data)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", src_path, "-c:a", "aac", dst_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=config.MEDIA_TRANSCODE_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001
            console.system(f"[media_understanding] ffmpeg transcode failed: {e}")
            return None
        if proc.returncode != 0 or not os.path.exists(dst_path):
            console.system(f"[media_understanding] ffmpeg transcode exited {proc.returncode}")
            return None
        with open(dst_path, "rb") as f:
            transcoded = f.read()
    return transcoded, "m4a"


async def transcribe_inbound_audio(
    media_url: str, *, _prefetched: tuple[str, bytes] | None = None,
) -> str | None:
    """Downloads `media_url`, transcodes it if needed (see
    _ensure_supported_audio -- iMessage voice memos are .caf, which
    neither Gemini nor OpenRouter accept natively), and asks
    config.MEDIA_UNDERSTANDING_MODEL_NAME to transcribe/summarize it via
    an OpenRouter input_audio content block (base64-only, no URL support,
    per OpenRouter's documented support for this model).

    Unlike describe_inbound_image, a None here is NOT meant to be treated
    as "nothing to splice in" -- see server.py's _maybe_understand_inbound_
    media and config.py's own comment on why audio failures get a fixed
    apology note instead of being silently dropped: a voice memo is a
    deliberate act, and _process_inbound's `if not effective_content:
    return` guard would otherwise leave the user with no reply at all.

    `_prefetched`, when given, skips a redundant re-download the same way
    describe_inbound_image's own `_prefetched` does -- see its docstring."""
    downloaded = _prefetched if _prefetched is not None else await download_attachment(media_url)
    if downloaded is None:
        return None
    _content_type, data = downloaded

    if len(data) > config.MAX_AUDIO_READ_BYTES:
        limit_mb = config.MAX_AUDIO_READ_BYTES / (1024 * 1024)
        console.system(f"[media_understanding] audio too large ({len(data)} bytes, limit {limit_mb:.0f}MB)")
        return None

    prepared = await _ensure_supported_audio(data, media_url)
    if prepared is None:
        return None
    audio_bytes, audio_format = prepared
    b64 = base64.b64encode(audio_bytes).decode("ascii")

    try:
        model = config.build_model(
            config.MEDIA_UNDERSTANDING_MODEL_NAME,
            api_key=config.api_key_for_agent("media_understanding"),
        )
        result = await asyncio.wait_for(
            model.ainvoke([
                {
                    "role": "system",
                    "content": (
                        "You transcribe a voice memo sent over text message. Reply with "
                        "a faithful transcript of what was said. If speech is unclear or "
                        "absent, say so briefly instead of guessing."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "input_audio": {"data": b64, "format": audio_format}},
                    ],
                },
            ]),
            timeout=config.MEDIA_UNDERSTANDING_TIMEOUT_SECONDS,
        )
        text = (getattr(result, "content", None) or "").strip()
    except Exception as e:  # noqa: BLE001 - see docstring: caller supplies the fallback text, not us
        console.system(f"[media_understanding] audio transcription failed: {e}")
        return None

    if not text:
        return None
    return f"[The user just sent a voice memo over text. Transcript:]\n{text}"
