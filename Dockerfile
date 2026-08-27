# Hugging Face Spaces (Docker SDK). See README.md "Deploying to Hugging
# Face Spaces" for the full walkthrough -- converting your existing Space
# from Gradio to this just means: sdk: docker + app_port: 7860 in
# README.md's frontmatter, this Dockerfile, and a push.
FROM python:3.11-slim

# Non-root user + HOME/PATH -- required for HF Spaces' Dev Mode, and good
# practice regardless (see huggingface.co/docs/hub/spaces-sdks-docker-first-demo).
RUN useradd -m -u 1000 user

# Node.js -- deepsearch's browser tools come from `npx @playwright/mcp@latest`
# (see messa/tools/deepsearch_tools.py), which needs a real Node runtime.
# Debian bookworm's own `nodejs` package is too old for it, so pull current
# LTS from NodeSource instead.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --chown=user requirements.txt requirements.txt
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Pre-install Chromium + its OS-level deps at build time (root -- needed for
# --with-deps' apt access) rather than on the first deepsearch delegation.
# PLAYWRIGHT_BROWSERS_PATH pins the install to a location the non-root
# `user` account can still read at runtime: left at the default, this
# writes into /root/.cache, which `user` can't see, and deepsearch would
# then silently try (and likely fail) to redownload the browser on its
# very first delegation.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN npx --yes playwright install --with-deps chromium \
    && chmod -R a+rX /opt/pw-browsers

COPY --chown=user . /app

# Ephemeral by default on HF Spaces' free CPU Basic tier: these do NOT
# survive a restart/redeploy without attaching persistent storage (a paid
# add-on). deepsearch's per-user Chromium profiles (login persistence
# across separate delegations) and any local session/output files reset
# to empty each time the Space restarts -- deepsearch still works, it just
# starts logged-out again. See README for the tradeoff. chown matters here:
# mkdir as root defaults to root ownership, which `user` (below) couldn't
# write into otherwise.
RUN mkdir -p deepsearch_profiles sessions outputs \
    && chown -R user:user deepsearch_profiles sessions outputs

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    MESSA_DEEPSEARCH_HEADLESS=true

# Pre-resolve the @playwright/mcp package under `user`'s own npx cache
# (~/.npm/_npx -- npm's *global* install location is a separate cache npx
# doesn't consult for a versioned `npx pkg@latest` invocation, confirmed
# while testing this Dockerfile) so the very first deepsearch delegation
# isn't a cold npm-registry fetch. Best-effort: `|| true` because this is a
# speed optimization, not a requirement -- runtime falls back to fetching
# it live (same as local dev) if this fails during a build with flaky
# network.
RUN npx --yes @playwright/mcp@latest --version > /dev/null 2>&1 || true

EXPOSE 7860
CMD ["uvicorn", "messa.server:app", "--host", "0.0.0.0", "--port", "7860"]
