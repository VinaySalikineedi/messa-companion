"""Encryption and password generation for site_credentials
(migrations/021_site_credentials.sql) -- accounts deepsearch creates on the
user's behalf on sites Composio doesn't support (see
tools/deepsearch_tools.py's generate_account_credential/
get_account_credential and that migration's own header comment for the
full design/tradeoffs).

Deliberately its own small standalone module (same shape as timeutil.py/
weather.py) rather than folded into db.py: db.py's own convention
throughout this project is "pure DB access, no business logic, no
external dependencies beyond asyncpg" -- encryption is a pure computation,
not a DB concern, and keeping it here means db.py's site_credentials
functions only ever see/return already-encrypted text, never a plaintext
password.

Uses `cryptography`'s Fernet (symmetric, authenticated encryption) rather
than rolling anything custom. Not a new hard dependency: already present
in this environment as a transitive dependency, but listed explicitly in
requirements.txt now that this project actually calls it directly rather
than relying on it being pulled in by something else.
"""
from __future__ import annotations

import secrets
import string

from . import config


class CredentialsNotConfigured(Exception):
    pass


def _get_fernet():
    if not config.CREDENTIALS_ENCRYPTION_KEY:
        raise CredentialsNotConfigured(
            "Site credential storage isn't configured yet -- add MESSA_CREDENTIALS_ENCRYPTION_KEY "
            "to .env (see config.py's comment for how to generate one)."
        )
    from cryptography.fernet import Fernet, InvalidToken  # imported lazily, same reasoning as composio

    try:
        return Fernet(config.CREDENTIALS_ENCRYPTION_KEY.encode()), InvalidToken
    except Exception as e:  # noqa: BLE001 - a malformed key is a config error, not a crash
        raise CredentialsNotConfigured(
            f"MESSA_CREDENTIALS_ENCRYPTION_KEY doesn't look like a valid Fernet key: {e}"
        ) from e


def encrypt_secret(plaintext: str) -> str:
    """Returns an opaque, URL-safe base64 token -- what actually gets
    stored in site_credentials.encrypted_password. Raises
    CredentialsNotConfigured if MESSA_CREDENTIALS_ENCRYPTION_KEY isn't set
    or isn't a valid Fernet key -- callers (tools/deepsearch_tools.py)
    catch this and report plainly rather than ever generating a real
    password with nowhere safe to put it."""
    fernet, _ = _get_fernet()
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Inverse of encrypt_secret. Raises CredentialsNotConfigured if the
    key is missing/invalid, or ValueError if the token can't be decrypted
    under the CURRENT key -- e.g. the key was rotated/lost since this row
    was written (see config.py's own warning about that) -- distinguished
    from a config problem so a caller can tell "you haven't set this up"
    apart from "this specific stored credential is no longer readable"."""
    fernet, InvalidToken = _get_fernet()
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ValueError(
            "Couldn't decrypt this stored credential -- MESSA_CREDENTIALS_ENCRYPTION_KEY may have "
            "changed since it was saved."
        ) from e


# Deliberately excludes visually-ambiguous characters (0/O, 1/l/I) and the
# handful of punctuation marks that most commonly break naive form-parsing
# or URL-embedding on real signup forms (quotes, backslash, whitespace) --
# still large enough (86 characters) for strong entropy at
# DEFAULT_PASSWORD_LENGTH, while reducing the odds of a generated password
# tripping up the very form it's meant to be typed into.
_LETTERS = "".join(c for c in string.ascii_letters if c not in "lIO")
_DIGITS = "23456789"  # excludes 0/1
_SYMBOLS = "!@#$%^&*-_=+"
_ALL = _LETTERS + _DIGITS + _SYMBOLS

DEFAULT_PASSWORD_LENGTH = 20


def generate_strong_password(length: int = DEFAULT_PASSWORD_LENGTH) -> str:
    """A cryptographically random password (via `secrets`, not `random`)
    meant to satisfy typical site complexity rules on the first try --
    guarantees at least one lowercase letter, one uppercase letter, one
    digit, and one symbol, then fills the rest randomly and shuffles so
    the guaranteed characters aren't predictably in the first four
    positions."""
    if length < 8:
        raise ValueError("length must be at least 8 to guarantee one of each character class")
    required = [
        secrets.choice([c for c in _LETTERS if c.islower()]),
        secrets.choice([c for c in _LETTERS if c.isupper()]),
        secrets.choice(_DIGITS),
        secrets.choice(_SYMBOLS),
    ]
    rest = [secrets.choice(_ALL) for _ in range(length - len(required))]
    chars = required + rest
    # Fisher-Yates via secrets.randbelow -- random.shuffle is NOT
    # cryptographically secure and shouldn't touch a real password's
    # character order even post-generation.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)
