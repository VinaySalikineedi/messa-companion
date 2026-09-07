"""Publish Messa's business Contact Sharing profile (name + photo) on
Sendblue -- what makes the Messa phone number show up in a recipient's
Messages app as "Messa AI" with a logo instead of a bare phone number, the
way a verified business iMessage contact does. Docs: Sendblue's Contact
Sharing v2 API (https://docs.sendblue.com/api-v2/contact-sharing/), wrapped
in channels/sendblue.py's set_contact_profile/get_contact_profile_state/
share_contact_profile/delete_contact_profile.

WHY THIS IS A SCRIPT, NOT SOMETHING THE AGENT SESSION RAN DIRECTLY: the
cloud sandbox this was written in has no outbound network route to
api.sendblue.com (proxy-blocked) and no push access to deploy this repo's
own change (needed so /contact-photo.png resolves on your live domain) --
so the actual API calls below have only been checked against Sendblue's
documented request/response shapes, never fired for real. Run this
yourself, from a machine with real internet access and this repo's real
.env, after deploying the /contact-photo.png route (server.py) and its
image (messa/assets/landing/contact-photo.png) that came with this change.

What it does, in order:
  1. GET the current profile state and print it (always -- read-only,
     harmless, tells you whether anything's already set).
  2. Unless --check-only: POST the new name/photo (idempotent -- calling
     this again with the same values just re-confirms them; Sendblue's own
     docs say applying a change can continue after the response returns,
     so don't panic if it's not visible in Messages instantly).
  3. Only if you pass --share-existing: loop over every current user's
     phone number (db.get_all_user_phone_numbers -- a handful of rows at
     this project's current scale, not a mass broadcast) and call
     share_contact_profile for each, since Sendblue's docs are explicit
     that setting the profile does NOT push it into any conversation that
     already exists -- each one needs its own share request. New users who
     text in for the first time AFTER this is set still won't automatically
     see it either (per the same docs) -- wiring that into the live inbound
     path (e.g. server.py's _process_inbound, alongside the read-receipt/
     reaction work) is a separate, deliberate follow-up, not bundled into
     this one-off script.

Requirements:
  - Real SENDBLUE_API_KEY / SENDBLUE_API_SECRET / SENDBLUE_NUMBER in this
    repo's .env (already there for the messaging endpoints this project
    already uses).
  - For --share-existing: a real DATABASE_URL too.
  - The /contact-photo.png route actually deployed and live at
    --photo-url (default https://textmessa.com/contact-photo.png) -- a
    404/unreachable photo URL will make Sendblue reject the profile call.

Usage:
    python3 scripts/publish_contact_profile.py                  # check current state only... no wait, see --check-only below
    python3 scripts/publish_contact_profile.py --check-only      # just GET and print current state
    python3 scripts/publish_contact_profile.py                   # set name="Messa" name="AI" + the deployed photo
    python3 scripts/publish_contact_profile.py --photo-url https://example.com/logo.png --first-name Messa --last-name AI
    python3 scripts/publish_contact_profile.py --share-existing  # also push it into every existing user's thread
    python3 scripts/publish_contact_profile.py --delete          # remove the profile entirely (undo)
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from messa import config  # noqa: E402
from messa.channels import sendblue  # noqa: E402


DEFAULT_PHOTO_URL = "https://textmessa.com/contact-photo.png"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--first-name", default="Messa", help="Business first name (default: Messa)")
    parser.add_argument("--last-name", default="AI", help="Business last name (default: AI)")
    parser.add_argument(
        "--photo-url", default=DEFAULT_PHOTO_URL,
        help=f"Public direct JPEG/PNG URL (default: {DEFAULT_PHOTO_URL}, "
        "the /contact-photo.png route that ships with this change -- must "
        "actually be deployed and reachable, or pass a different public "
        "URL you already have, e.g. to test before deploying).",
    )
    parser.add_argument("--check-only", action="store_true", help="Only GET and print current state, change nothing.")
    parser.add_argument("--delete", action="store_true", help="Delete the profile entirely instead of setting one.")
    parser.add_argument(
        "--share-existing", action="store_true",
        help="After setting the profile, also push it into every existing user's conversation "
        "(db.get_all_user_phone_numbers) -- required for CURRENT users to actually see it; "
        "see this script's own docstring for why setting alone isn't enough.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt before --share-existing (for non-interactive/CI use).",
    )
    args = parser.parse_args()

    print(f"Sendblue number: {config.SENDBLUE_NUMBER}")
    print("\n=== Current profile state (GET) ===")
    try:
        state = await sendblue.get_contact_profile_state()
        print(json.dumps(state, indent=2))
    except sendblue.SendblueError as e:
        print(f"[ERROR] Could not read current state: {e}")
        if args.check_only:
            sys.exit(1)

    if args.check_only:
        return

    if args.delete:
        print("\n=== Deleting profile ===")
        result = await sendblue.delete_contact_profile()
        print(json.dumps(result, indent=2))
        print("\nDone -- profile removed. This does not un-share it from threads that already saw it.")
        return

    display_name = f"{args.first_name} {args.last_name}".strip()
    print(f"\n=== Setting profile: \"{display_name}\", photo={args.photo_url} ===")
    result = await sendblue.set_contact_profile(
        first_name=args.first_name, last_name=args.last_name, photo_url=args.photo_url,
    )
    print(json.dumps(result, indent=2))
    print(
        "\nProfile call sent. Per Sendblue's own docs this can keep applying after the response "
        "returns -- give it a bit before assuming it didn't work if it's not visible instantly."
    )

    if not args.share_existing:
        print(
            "\nNOTE: this only sets the profile -- it does NOT push it into any conversation that "
            "already exists, and it won't automatically show up for brand-new users either. Re-run "
            "with --share-existing to push it into every CURRENT user's thread right now."
        )
        return

    from messa import db

    numbers = await db.get_all_user_phone_numbers()
    print(f"\n=== Sharing with {len(numbers)} existing user(s) ===")
    for n in numbers:
        print(f"  {n}")
    if not args.yes:
        confirm = input(f"\nProceed sharing with all {len(numbers)} number(s) above? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted -- profile was still set above, just skipping the share-to-existing-users step.")
            return

    shared, failed = [], []
    for n in numbers:
        try:
            await sendblue.share_contact_profile(n)
            shared.append(n)
            print(f"  [OK] {n}")
        except sendblue.SendblueError as e:
            failed.append((n, str(e)))
            print(f"  [FAIL] {n}: {e}")
        await asyncio.sleep(0.5)  # light pacing -- no documented rate limit, just being polite to a new endpoint

    print(f"\nShared with {len(shared)}/{len(numbers)}.")
    if failed:
        print("Failed:")
        for n, err in failed:
            print(f"  {n}: {err}")


if __name__ == "__main__":
    asyncio.run(main())
