"""Tests for messa/media_understanding.py (voice memo + image understanding
for inbound SMS/iMessage attachments) and its wiring into server.py's
_process_inbound via _maybe_understand_inbound_media.

Mirrors this project's existing plain-script `check(label, cond)`
convention (tests/test_document_share_bytes.py, tests/test_app_connect_
queue.py) -- no live browser, no live Composio, no live LLM call, no live
ffmpeg. Five things verified:

  1. classify_attachment -- content-type/extension/magic-byte
     classification, including the HEIC and CAF edge cases config.py's own
     plan flagged as risky to get wrong.
  2. describe_inbound_image -- success/oversized/timeout/exception/empty
     paths against a fake model, never touching a real OpenRouter call.
  3. transcribe_inbound_audio + _ensure_supported_audio -- the same
     success/failure shape, plus the ffmpeg transcode path (native formats
     skip the subprocess entirely; .caf goes through a fully faked
     asyncio.create_subprocess_exec so this suite needs no real ffmpeg
     binary installed).
  4. server._maybe_understand_inbound_media -- the dispatcher: routes to
     image/audio/PDF correctly, and MEDIA_UNDERSTANDING_ENABLED=False
     produces byte-for-byte the same call (straight to
     _maybe_read_inbound_pdf) as before this feature existed -- the
     safety guarantee PR 1 of the rollout rests on.
  5. The asymmetric audio-vs-image fallback contract described in
     config.py's own comment above MEDIA_UNDERSTANDING_ENABLED: a failed
     audio transcription must still produce non-empty effective_content
     (never silently dropped, since a voice memo is a deliberate act),
     while a failed captionless image is allowed to end up empty.
"""
import asyncio
import os
import sys
import types

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")
    os.environ.setdefault("COMPOSIO_API_KEY", "comp-test-dummy-not-real")
    os.environ.setdefault("SENDBLUE_API_KEY", "sb-test-dummy-key")
    os.environ.setdefault("SENDBLUE_API_SECRET", "sb-test-dummy-secret")

# Fake `composio` package so server.py (which imports tools/integration_tools.py)
# imports cleanly without the real SDK installed -- same technique as
# test_document_share_bytes.py / test_app_connect_queue.py.
class ComposioMultipleConnectedAccountsError(Exception):
    pass


_fake_composio_exceptions = types.ModuleType("composio.exceptions")
_fake_composio_exceptions.ComposioMultipleConnectedAccountsError = ComposioMultipleConnectedAccountsError
_fake_composio_pkg = types.ModuleType("composio")
_fake_composio_pkg.exceptions = _fake_composio_exceptions
sys.modules["composio"] = _fake_composio_pkg
sys.modules["composio.exceptions"] = _fake_composio_exceptions

from messa import config, media_understanding, server  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ---------------------------------------------------------------------------
# Part 1: classify_attachment
# ---------------------------------------------------------------------------

def part1_classify_attachment():
    K = media_understanding.AttachmentKind

    cases = [
        ("by content-type", "image/png", "http://cdn/x", b"", K.IMAGE),
        ("by extension .jpg", "", "http://cdn/photo.JPG", b"", K.IMAGE),
        ("by extension .heic", "application/octet-stream", "http://cdn/photo.heic", b"", K.IMAGE),
        ("by magic bytes JPEG (no header/ext)", "", "http://cdn/blob", b"\xff\xd8\xff\xe0rest", K.IMAGE),
        ("by magic bytes PNG (no header/ext)", "", "http://cdn/blob", b"\x89PNG\r\n\x1a\nrest", K.IMAGE),
        ("by magic bytes WEBP (no header/ext)", "", "http://cdn/blob", b"RIFF\x00\x00\x00\x00WEBPVP8 ", K.IMAGE),
        ("by magic bytes HEIC ftyp box (no header/ext)", "", "http://cdn/blob",
         b"\x00\x00\x00\x18ftypheicrestrestrest", K.IMAGE),
        ("by content-type audio", "audio/mpeg", "http://cdn/x", b"", K.AUDIO),
        ("by extension .caf", "", "http://cdn/memo.caf", b"", K.AUDIO),
        ("by magic bytes CAF (no header/ext)", "", "http://cdn/blob", b"caff" + b"\x00" * 20, K.AUDIO),
        ("by magic bytes M4A (no header/ext)", "", "http://cdn/blob", b"\x00\x00\x00\x1cftypM4A rest", K.AUDIO),
        ("unrecognized stays UNKNOWN", "text/html", "http://cdn/page.html", b"<html>", K.UNKNOWN),
        ("empty everything stays UNKNOWN", "", "http://cdn/blob", b"", K.UNKNOWN),
    ]
    for label, content_type, url, data, expected in cases:
        got = media_understanding.classify_attachment(content_type, url, data)
        check(f"classify_attachment: {label}", got is expected)


# ---------------------------------------------------------------------------
# Part 2 + 3: describe_inbound_image / transcribe_inbound_audio against a
# fake model -- never touches a real OpenRouter call.
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, content):
        self.content = content


class _FakeModel:
    """Installed in place of config.build_model's return value. `behavior`
    controls what ainvoke does: a plain string -> returns that as .content;
    "SLEEP" -> sleeps past the configured timeout; "RAISE" -> raises."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        if self.behavior == "SLEEP":
            await asyncio.sleep(5)
            return _FakeResult("too slow, should never be seen")
        if self.behavior == "RAISE":
            raise RuntimeError("simulated OpenRouter failure")
        return _FakeResult(self.behavior)


def _install_fake_model(monkeypatch_holder, behavior):
    fake = _FakeModel(behavior)

    def fake_build_model(model_name, *, effective_context_tokens=None, api_key=None):
        monkeypatch_holder["build_model_calls"] += 1
        return fake

    original = config.build_model
    config.build_model = fake_build_model
    monkeypatch_holder["restore"] = lambda: setattr(config, "build_model", original)
    return fake


async def part2_describe_inbound_image():
    holder = {"build_model_calls": 0}

    # Success path.
    _install_fake_model(holder, "A cat sitting on a red couch.")
    result = await media_understanding.describe_inbound_image(
        "http://cdn/cat.jpg", _prefetched=("image/jpeg", b"\xff\xd8fakejpegbytes"),
    )
    check("describe_inbound_image: success returns model text", result and "cat sitting on a red couch" in result)
    check("describe_inbound_image: success is prefixed for the orchestrator",
          result and result.startswith("[The user just sent a photo over text."))
    holder["restore"]()

    # Oversized: build_model should never even be called.
    holder = {"build_model_calls": 0}
    _install_fake_model(holder, "should never be used")
    huge = b"x" * (config.MAX_IMAGE_READ_BYTES + 1)
    result = await media_understanding.describe_inbound_image(
        "http://cdn/huge.jpg", _prefetched=("image/jpeg", huge),
    )
    check("describe_inbound_image: oversized image returns None", result is None)
    check("describe_inbound_image: oversized image never calls the model", holder["build_model_calls"] == 0)
    holder["restore"]()

    # Timeout.
    holder = {"build_model_calls": 0}
    _install_fake_model(holder, "SLEEP")
    original_timeout = config.MEDIA_UNDERSTANDING_TIMEOUT_SECONDS
    config.MEDIA_UNDERSTANDING_TIMEOUT_SECONDS = 0.05
    try:
        result = await media_understanding.describe_inbound_image(
            "http://cdn/cat.jpg", _prefetched=("image/jpeg", b"fakebytes"),
        )
    finally:
        config.MEDIA_UNDERSTANDING_TIMEOUT_SECONDS = original_timeout
    check("describe_inbound_image: timeout returns None (never raises)", result is None)
    holder["restore"]()

    # Exception from the model call.
    holder = {"build_model_calls": 0}
    _install_fake_model(holder, "RAISE")
    result = await media_understanding.describe_inbound_image(
        "http://cdn/cat.jpg", _prefetched=("image/jpeg", b"fakebytes"),
    )
    check("describe_inbound_image: model exception returns None (never propagates)", result is None)
    holder["restore"]()

    # Empty reply from the model.
    holder = {"build_model_calls": 0}
    _install_fake_model(holder, "   ")
    result = await media_understanding.describe_inbound_image(
        "http://cdn/cat.jpg", _prefetched=("image/jpeg", b"fakebytes"),
    )
    check("describe_inbound_image: blank model reply returns None", result is None)
    holder["restore"]()

    # Download failure (no _prefetched given, download_attachment fails).
    original_download = media_understanding.download_attachment

    async def failing_download(media_url):
        return None

    media_understanding.download_attachment = failing_download
    try:
        result = await media_understanding.describe_inbound_image("http://cdn/gone.jpg")
    finally:
        media_understanding.download_attachment = original_download
    check("describe_inbound_image: download failure returns None", result is None)


async def part3_ensure_supported_audio():
    # Native format: no subprocess should be invoked at all.
    calls = {"n": 0}
    original_exec = asyncio.create_subprocess_exec

    async def counting_exec(*args, **kwargs):
        calls["n"] += 1
        return await original_exec(*args, **kwargs)

    asyncio.create_subprocess_exec = counting_exec
    try:
        result = await media_understanding._ensure_supported_audio(b"RIFFfakewavbytes", "http://cdn/memo.wav")
    finally:
        asyncio.create_subprocess_exec = original_exec
    check("_ensure_supported_audio: native wav skips ffmpeg entirely", calls["n"] == 0)
    check("_ensure_supported_audio: native wav returns original bytes+format",
          result == (b"RIFFfakewavbytes", "wav"))

    # .caf needs a transcode -- fully faked subprocess, no real ffmpeg needed.
    class _FakeProc:
        def __init__(self, returncode=0):
            self.returncode = returncode

        async def wait(self):
            return self.returncode

    async def fake_exec_success(*args, **kwargs):
        # args: ("ffmpeg", "-y", "-i", src_path, "-c:a", "aac", dst_path)
        dst_path = args[-1]
        with open(dst_path, "wb") as f:
            f.write(b"fake-transcoded-m4a-bytes")
        return _FakeProc(returncode=0)

    asyncio.create_subprocess_exec = fake_exec_success
    try:
        result = await media_understanding._ensure_supported_audio(b"caff" + b"\x00" * 20, "http://cdn/memo.caf")
    finally:
        asyncio.create_subprocess_exec = original_exec
    check("_ensure_supported_audio: .caf transcodes via ffmpeg to m4a",
          result == (b"fake-transcoded-m4a-bytes", "m4a"))

    # ffmpeg exits non-zero -> treated as a clean failure (None), never raises.
    async def fake_exec_failure(*args, **kwargs):
        return _FakeProc(returncode=1)

    asyncio.create_subprocess_exec = fake_exec_failure
    try:
        result = await media_understanding._ensure_supported_audio(b"caff" + b"\x00" * 20, "http://cdn/memo.caf")
    finally:
        asyncio.create_subprocess_exec = original_exec
    check("_ensure_supported_audio: ffmpeg non-zero exit returns None", result is None)

    # ffmpeg call itself raises -> also a clean None, never propagates.
    async def fake_exec_raises(*args, **kwargs):
        raise OSError("ffmpeg not found")

    asyncio.create_subprocess_exec = fake_exec_raises
    try:
        result = await media_understanding._ensure_supported_audio(b"caff" + b"\x00" * 20, "http://cdn/memo.caf")
    finally:
        asyncio.create_subprocess_exec = original_exec
    check("_ensure_supported_audio: ffmpeg launch failure returns None (never raises)", result is None)


async def part3_transcribe_inbound_audio():
    holder = {"build_model_calls": 0}

    # Success path (native format, no transcode needed).
    _install_fake_model(holder, "Hey, just calling to say hi.")
    result = await media_understanding.transcribe_inbound_audio(
        "http://cdn/memo.wav", _prefetched=("audio/wav", b"RIFFfakewavbytes"),
    )
    check("transcribe_inbound_audio: success returns model transcript",
          result and "just calling to say hi" in result)
    check("transcribe_inbound_audio: success is prefixed for the orchestrator",
          result and result.startswith("[The user just sent a voice memo over text."))
    holder["restore"]()

    # Oversized.
    holder = {"build_model_calls": 0}
    _install_fake_model(holder, "should never be used")
    huge = b"x" * (config.MAX_AUDIO_READ_BYTES + 1)
    result = await media_understanding.transcribe_inbound_audio(
        "http://cdn/huge.wav", _prefetched=("audio/wav", huge),
    )
    check("transcribe_inbound_audio: oversized audio returns None", result is None)
    check("transcribe_inbound_audio: oversized audio never calls the model", holder["build_model_calls"] == 0)
    holder["restore"]()

    # Model exception.
    holder = {"build_model_calls": 0}
    _install_fake_model(holder, "RAISE")
    result = await media_understanding.transcribe_inbound_audio(
        "http://cdn/memo.wav", _prefetched=("audio/wav", b"RIFFfakewavbytes"),
    )
    check("transcribe_inbound_audio: model exception returns None (never propagates)", result is None)
    holder["restore"]()


# ---------------------------------------------------------------------------
# Part 4: server._maybe_understand_inbound_media dispatch
# ---------------------------------------------------------------------------

async def part4_dispatcher_routing():
    K = media_understanding.AttachmentKind
    original = {
        "download_attachment": media_understanding.download_attachment,
        "classify_attachment": media_understanding.classify_attachment,
        "describe_inbound_image": media_understanding.describe_inbound_image,
        "transcribe_inbound_audio": media_understanding.transcribe_inbound_audio,
        "_maybe_read_inbound_pdf": server._maybe_read_inbound_pdf,
        "MEDIA_UNDERSTANDING_ENABLED": config.MEDIA_UNDERSTANDING_ENABLED,
    }

    def restore():
        media_understanding.download_attachment = original["download_attachment"]
        media_understanding.classify_attachment = original["classify_attachment"]
        media_understanding.describe_inbound_image = original["describe_inbound_image"]
        media_understanding.transcribe_inbound_audio = original["transcribe_inbound_audio"]
        server._maybe_read_inbound_pdf = original["_maybe_read_inbound_pdf"]
        config.MEDIA_UNDERSTANDING_ENABLED = original["MEDIA_UNDERSTANDING_ENABLED"]

    try:
        pdf_calls = []

        async def fake_pdf(media_url):
            pdf_calls.append(media_url)
            return "[PDF NOTE]"

        server._maybe_read_inbound_pdf = fake_pdf

        # --- Flag OFF: must behave byte-for-byte like the pre-feature code
        # -- straight to _maybe_read_inbound_pdf, no download/classify at all.
        config.MEDIA_UNDERSTANDING_ENABLED = False
        download_calls = []

        async def tracking_download(media_url):
            download_calls.append(media_url)
            return ("image/jpeg", b"should not matter")

        media_understanding.download_attachment = tracking_download
        note, is_audio_failure = await server._maybe_understand_inbound_media("http://cdn/x.jpg")
        check("dispatcher (flag off): delegates straight to _maybe_read_inbound_pdf", note == "[PDF NOTE]")
        check("dispatcher (flag off): never downloads/classifies for itself", len(download_calls) == 0)
        check("dispatcher (flag off): is_audio_failure is always False", is_audio_failure is False)

        # --- Flag ON, IMAGE kind, successful description.
        config.MEDIA_UNDERSTANDING_ENABLED = True
        pdf_calls.clear()

        async def download_image(media_url):
            return ("image/jpeg", b"\xff\xd8fakejpeg")

        media_understanding.download_attachment = download_image
        media_understanding.classify_attachment = lambda ct, url, data: K.IMAGE

        async def fake_describe(media_url, *, _prefetched=None):
            check("dispatcher: passes prefetched bytes to describe_inbound_image (no double download)",
                  _prefetched == ("image/jpeg", b"\xff\xd8fakejpeg"))
            return "[The user just sent a photo over text.]\nA sunset."

        media_understanding.describe_inbound_image = fake_describe
        note, is_audio_failure = await server._maybe_understand_inbound_media("http://cdn/sunset.jpg")
        check("dispatcher (image, success): returns the description", note and "sunset" in note)
        check("dispatcher (image, success): is_audio_failure is False", is_audio_failure is False)
        check("dispatcher (image path): never falls through to the PDF reader", len(pdf_calls) == 0)

        # --- Flag ON, IMAGE kind, failed (captionless) description.
        async def fake_describe_fail(media_url, *, _prefetched=None):
            return None

        media_understanding.describe_inbound_image = fake_describe_fail
        note, is_audio_failure = await server._maybe_understand_inbound_media("http://cdn/blurry.jpg")
        check("dispatcher (image, failure): returns None (tolerant -- like today's captionless photo)", note is None)
        check("dispatcher (image, failure): is_audio_failure is False (asymmetric contract)",
              is_audio_failure is False)

        # --- Flag ON, AUDIO kind, successful transcription.
        async def download_audio(media_url):
            return ("audio/wav", b"RIFFfakewav")

        media_understanding.download_attachment = download_audio
        media_understanding.classify_attachment = lambda ct, url, data: K.AUDIO

        async def fake_transcribe(media_url, *, _prefetched=None):
            check("dispatcher: passes prefetched bytes to transcribe_inbound_audio (no double download)",
                  _prefetched == ("audio/wav", b"RIFFfakewav"))
            return "[The user just sent a voice memo over text.]\nHi there."

        media_understanding.transcribe_inbound_audio = fake_transcribe
        note, is_audio_failure = await server._maybe_understand_inbound_media("http://cdn/memo.wav")
        check("dispatcher (audio, success): returns the transcript", note and "Hi there" in note)
        check("dispatcher (audio, success): is_audio_failure is False", is_audio_failure is False)

        # --- Flag ON, AUDIO kind, failed transcription -- THE critical
        # asymmetric-contract assertion: is_audio_failure must be True so
        # _process_inbound splices in its own fallback text instead of
        # treating this like a silently-ignorable captionless photo.
        async def fake_transcribe_fail(media_url, *, _prefetched=None):
            return None

        media_understanding.transcribe_inbound_audio = fake_transcribe_fail
        note, is_audio_failure = await server._maybe_understand_inbound_media("http://cdn/mumbled.wav")
        check("dispatcher (audio, failure): note is None", note is None)
        check("dispatcher (audio, failure): is_audio_failure is True (must not be silently dropped)",
              is_audio_failure is True)

        # --- Flag ON, UNKNOWN kind (e.g. a PDF) -- falls through to the PDF reader.
        pdf_calls.clear()
        media_understanding.classify_attachment = lambda ct, url, data: K.UNKNOWN
        note, is_audio_failure = await server._maybe_understand_inbound_media("http://cdn/doc.pdf")
        check("dispatcher (unknown kind): falls through to _maybe_read_inbound_pdf", note == "[PDF NOTE]")
        check("dispatcher (unknown kind): is_audio_failure is False", is_audio_failure is False)
        check("dispatcher (unknown kind): PDF reader called exactly once", pdf_calls == ["http://cdn/doc.pdf"])

        # --- Flag ON, download itself fails -- falls back to the PDF path
        # rather than guessing (matches _maybe_read_inbound_pdf's own
        # "flaky CDN must never break the turn" reasoning).
        pdf_calls.clear()

        async def failing_download(media_url):
            return None

        media_understanding.download_attachment = failing_download
        note, is_audio_failure = await server._maybe_understand_inbound_media("http://cdn/gone")
        check("dispatcher (download failure): falls back to _maybe_read_inbound_pdf", note == "[PDF NOTE]")
        check("dispatcher (download failure): is_audio_failure is False", is_audio_failure is False)
    finally:
        restore()


# ---------------------------------------------------------------------------
# Part 5: the asymmetric fallback contract, replicated at the exact
# splice logic _process_inbound uses (see server.py, right after the
# `if media_url:` block) -- a failed audio transcription must still leave
# effective_content non-empty; a failed captionless image is allowed to
# leave it empty so the existing "nothing to act on" guard still fires.
# ---------------------------------------------------------------------------

def _apply_splice(content, media_url, media_note, is_audio_failure, project_capsules_enabled):
    """Exact mirror of _process_inbound's own splice logic (see server.py,
    right after the `if media_url:` block) -- kept as a tiny standalone
    function here so this test doesn't have to stand up the entire webhook
    pipeline (waitlist/db/sendblue/cli.run_message) just to exercise a
    dozen lines of string logic.

    Updated for V3-autonomous.md Phase 6's fix to a QA-caught gap (P6
    report, Issue 5): the [attachment_url: ...] tag used to live INSIDE
    `if media_note:`, so it silently vanished whenever media_note was None
    for ANY reason -- not just a captionless plain photo, but also a
    vision/audio call failure or an unrecognized format (.docx, .zip) --
    meaning a document a user meant to file into a project's vault could
    never actually reach the model. It's now decoupled: it fires whenever
    media_url is present at all (still gated on project_capsules_enabled),
    and an otherwise-undescribable attachment gets a neutral placeholder
    instead of being silently dropped."""
    effective_content = content
    if media_url:
        if media_note:
            effective_content = f"{content}\n\n{media_note}".strip() if content else media_note
        elif is_audio_failure:
            effective_content = content or "[Voice memo received but couldn't be transcribed]"
        elif project_capsules_enabled:
            effective_content = content or "[Attachment received -- couldn't automatically read its contents.]"
    if not effective_content:
        return effective_content
    if media_url and project_capsules_enabled:
        effective_content += f"\n[attachment_url: {media_url}]"
    return effective_content


def part5_asymmetric_fallback_contract():
    check(
        "asymmetric contract: failed audio (no caption) still yields non-empty effective_content",
        bool(_apply_splice("", "http://cdn/x", None, True, False)),
    )
    check(
        "asymmetric contract: failed image (no caption), project capsules OFF, still yields empty "
        "(tolerant, exactly like before this feature existed)",
        _apply_splice("", "http://cdn/x", None, False, False) == "",
    )
    check(
        "asymmetric contract: failed audio WITH a caption keeps the caption, not the apology",
        _apply_splice("here's a memo", "http://cdn/x", None, True, False) == "here's a memo",
    )
    check(
        "asymmetric contract: successful media note is spliced onto an existing caption",
        _apply_splice("check this out", "http://cdn/x", "[NOTE]", False, False) == "check this out\n\n[NOTE]",
    )

    # --- P6 report Issue 5 fix: the attachment_url tag is decoupled from
    # media_note's own success/failure, so a document Messa couldn't
    # understand the CONTENTS of still reaches the model for vault filing.
    check(
        "Issue 5 fix: an unrecognized/failed attachment, project capsules ON, is no longer silently dropped",
        _apply_splice("", "http://cdn/receipt.docx", None, False, True) != "",
    )
    check(
        "Issue 5 fix: that same unrecognized attachment still carries the attachment_url tag",
        "[attachment_url: http://cdn/receipt.docx]"
        in _apply_splice("", "http://cdn/receipt.docx", None, False, True),
    )
    check(
        "Issue 5 fix: a SUCCESSFULLY understood attachment also carries the tag",
        "[attachment_url: http://cdn/x.jpg]"
        in _apply_splice("", "http://cdn/x.jpg", "[a photo of a receipt]", False, True),
    )
    check(
        "Issue 5 fix: a failed audio transcription also carries the tag",
        "[attachment_url: http://cdn/memo.wav]" in _apply_splice("", "http://cdn/memo.wav", None, True, True),
    )
    check(
        "Issue 5 fix: project capsules OFF -- no tag at all, byte-identical to pre-Phase-6 behavior",
        "[attachment_url:" not in _apply_splice("check this out", "http://cdn/x", "[NOTE]", False, False),
    )

    # Structural check: the actual wiring in server.py contains this exact
    # asymmetric branch, not just this test's standalone reimplementation.
    server_src = (REPO_ROOT / "messa" / "server.py").read_text()
    check(
        "server.py: _process_inbound calls the new dispatcher, not _maybe_read_inbound_pdf directly",
        "media_note, is_audio_failure = await _maybe_understand_inbound_media(media_url)" in server_src,
    )
    check(
        "server.py: the audio-failure fallback text is present",
        "couldn't be transcribed" in server_src,
    )
    check(
        "server.py: _maybe_read_inbound_pdf itself is untouched (still exists, still PDF-only)",
        "async def _maybe_read_inbound_pdf(media_url: str) -> str | None:" in server_src,
    )
    check(
        "Issue 5 fix, structurally: the attachment_url tag is applied AFTER the "
        "'if not effective_content: return' guard -- i.e. outside/decoupled from the "
        "media_note/is_audio_failure branching above it, not nested inside it",
        server_src.index("[attachment_url: {media_url}]") > server_src.index("if not effective_content:"),
    )


def part5_flag_defaults_on():
    check("MEDIA_UNDERSTANDING_ENABLED defaults to True", config.MEDIA_UNDERSTANDING_ENABLED is True)


async def main() -> None:
    part1_classify_attachment()

    await part2_describe_inbound_image()
    await part3_ensure_supported_audio()
    await part3_transcribe_inbound_audio()
    await part4_dispatcher_routing()

    part5_asymmetric_fallback_contract()
    part5_flag_defaults_on()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
