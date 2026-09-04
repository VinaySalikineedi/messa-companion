#!/usr/bin/env python3
"""Pulls runtime or build logs directly from Hugging Face Spaces (Vin999/aiMessage)
and saves them to error.txt (or stdout).

Usage:
    python3 scripts/pull_hfs_logs.py               # Writes latest run logs to error.txt
    python3 scripts/pull_hfs_logs.py --build       # Writes build logs to error.txt
    python3 scripts/pull_hfs_logs.py --tail 50     # Prints last 50 lines to stdout
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

def get_hf_token() -> str | None:
    # 1. Environment variable
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    if token:
        return token.strip()

    # 2. Git credential helper (cached by macOS keychain or git)
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=huggingface.co\n",
            text=True,
            capture_output=True,
            check=False,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass

    # 3. Local huggingface token cache
    cache_token_path = Path.home() / ".cache" / "huggingface" / "token"
    if cache_token_path.exists():
        try:
            return cache_token_path.read_text().strip()
        except Exception:
            pass

    return None

def main():
    parser = argparse.ArgumentParser(description="Pull logs from Hugging Face Space")
    parser.add_argument("--repo-id", default="Vin999/aiMessage", help="Space repo ID (default: Vin999/aiMessage)")
    parser.add_argument("--build", action="store_true", help="Fetch build logs instead of runtime logs")
    parser.add_argument("--output", default="error.txt", help="Destination file (default: error.txt, use '-' for stdout)")
    parser.add_argument("--tail", type=int, default=None, help="Only show the last N lines on stdout")
    args = parser.parse_args()

    token = get_hf_token()
    if not token:
        print("Error: Could not find Hugging Face token. Please set HF_TOKEN.", file=sys.stderr)
        sys.exit(1)

    try:
        from huggingface_hub import fetch_space_logs
    except ImportError:
        print("Installing huggingface_hub...", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"])
        from huggingface_hub import fetch_space_logs

    log_type = "build" if args.build else "runtime"
    print(f"Fetching {log_type} logs from {args.repo_id}...", file=sys.stderr)

    try:
        logs_generator = fetch_space_logs(args.repo_id, build=args.build, follow=False, token=token)
        raw_lines = list(logs_generator)
    except Exception as e:
        print(f"Error fetching logs from Hugging Face: {e}", file=sys.stderr)
        sys.exit(1)

    # Ensure proper line breaks and strip ANSI escape codes
    import re
    formatted = []
    for chunk in raw_lines:
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", chunk)
        if not clean:
            formatted.append("\n")
        elif clean.endswith("\n"):
            formatted.append(clean)
        else:
            formatted.append(clean + "\n")

    full_text = "".join(formatted)
    lines = full_text.splitlines(keepends=True)
    print(f"Fetched {len(lines)} lines from {args.repo_id}.", file=sys.stderr)

    if args.tail is not None:
        tail_lines = lines[-args.tail:] if len(lines) > args.tail else lines
        print("".join(tail_lines), end="")
        return

    if args.output == "-":
        print(full_text, end="")
    else:
        out_path = Path(args.output)
        out_path.write_text(full_text, encoding="utf-8")
        print(f"Saved clean logs to {out_path.resolve()} ({len(lines)} lines, {len(full_text)} bytes).", file=sys.stderr)

if __name__ == "__main__":
    main()
