# Hugging Face Spaces (Docker SDK). See README.md "Deploying to Hugging
# Face Spaces" for the full walkthrough -- converting your existing Space
# from Gradio to this just means: sdk: docker + app_port: 7860 in
# README.md's frontmatter, this Dockerfile, and a push.
FROM python:3.11-slim

# Without this, Python fully block-buffers stdout whenever it isn't
# attached to a real terminal -- which is exactly Docker's case. uvicorn's
# own request logs go through the `logging` module (which flushes each
# record), so those always showed up fine in the Space's Logs tab; but
# every plain print() this project uses for tracing (console.py --
# console.system/tool_error, the exact lines that show whether deepsearch
# even attempted a Browserbase session, and why it failed if not) could
# sit in an unflushed buffer indefinitely on a long-running server
# process. Confirmed as a real gap, not a hypothetical: this is *the*
# standard reason "print() debugging shows nothing in my container logs"
# happens, and there was nothing forcing a flush here before now.
ENV PYTHONUNBUFFERED=1

# Non-root user + HOME/PATH -- required for HF Spaces' Dev Mode, and good
# practice regardless (see huggingface.co/docs/hub/spaces-sdks-docker-first-demo).
RUN useradd -m -u 1000 user

# Node.js -- deepsearch's browser tools come from `npx @playwright/mcp@latest`
# (see messa/tools/deepsearch_tools.py), which needs a real Node runtime.
# Debian bookworm's own `nodejs` package is too old for it, so pull current
# LTS from NodeSource instead.
#
# ffmpeg -- messa/media_understanding.py shells out to it to transcode an
# inbound iMessage voice memo (.caf, a container neither Gemini nor
# OpenRouter accept natively) to .m4a before sending it for transcription.
# Only reached when MESSA_MEDIA_UNDERSTANDING_ENABLED=true (see config.py);
# installed unconditionally here since flipping that flag on shouldn't
# require a new image build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg ffmpeg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --chown=user requirements.txt requirements.txt
# pip/setuptools/wheel upgraded first, specifically so `composio`'s `pysher`
# dependency builds cleanly (see requirements.txt's own note -- reproduced
# directly against an old/Debian-patched setuptools while building the
# email-connection feature; cheap insurance if this image's base ever
# drifts the same way).
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir --upgrade -r requirements.txt

# (No local npm install step needed here anymore -- the real human-cursor
# driver, messa/nodehelpers/cursor_driver.mjs, was removed after a real run
# showed its drawn arrow never actually appeared in the live-view tiles
# users were watching, making the extra Node subprocess + CDP connection a
# pure cost with no payoff. The remaining cosmetic overlay -- cursor arrow,
# click ripple, typing highlight, page-transition flash, reading animation
# -- is a single static JS file @playwright/mcp injects via --init-script
# below, no npm dependency of its own.)

COPY --chown=user . /app

# Ephemeral by default on HF Spaces' free CPU Basic tier: these do NOT
# survive a restart/redeploy without attaching persistent storage (a paid
# add-on). Any local session/output files reset to empty each time the
# Space restarts. (deepsearch's own login persistence no longer lives here
# at all -- it's a per-user Browserbase Context now, see
# messa/channels/browserbase.py -- so that part is unaffected by restarts.)
# chown matters here: mkdir as root defaults to root ownership, which
# `user` (below) couldn't write into otherwise.
RUN mkdir -p sessions outputs \
    && chown -R user:user sessions outputs

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Pre-resolve the @playwright/mcp package under `user`'s own npx cache
# (~/.npm/_npx -- npm's *global* install location is a separate cache npx
# doesn't consult for a versioned `npx pkg@latest` invocation, confirmed
# while testing this Dockerfile) so the very first deepsearch delegation
# isn't a cold npm-registry fetch. Still worth doing even though the
# browser itself is remote now (Browserbase) -- this is just the MCP
# *client* package, which still runs locally and drives the remote browser
# over CDP. Best-effort: `|| true` because this is a speed optimization,
# not a requirement -- runtime falls back to fetching it live (same as
# local dev) if this fails during a build with flaky network.
RUN npx --yes @playwright/mcp@latest --version > /dev/null 2>&1 || true

EXPOSE 7860
CMD ["uvicorn", "messa.server:app", "--host", "0.0.0.0", "--port", "7860"]
