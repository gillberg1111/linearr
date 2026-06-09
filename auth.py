"""Optional single-user web-UI authentication (v3.3.0).

Default OFF: login is enabled only once an auth password hash exists in
`managed_settings`. The password is stored hashed (werkzeug). Setting
`LINEARR_AUTH_PASSWORD` in the environment force-(re)sets the password on boot —
the documented no-email reset path (set the env var, restart, log in, then clear
the env var; the hash persists in the DB).

Kept as a small standalone module so the logic is unit-testable without a Flask
request context. Only `db` is imported.
"""

from __future__ import annotations

import logging
import os
import secrets
import time

from werkzeug.security import check_password_hash, generate_password_hash

import db

log = logging.getLogger(__name__)

_USER_KEY = "auth_username"
_HASH_KEY = "auth_password_hash"
DEFAULT_USERNAME = "admin"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def auth_enabled() -> bool:
    """True when a password hash is set — that's what turns login on."""
    return bool(db.get_setting(_HASH_KEY))


def get_username() -> str:
    return db.get_setting(_USER_KEY) or DEFAULT_USERNAME


def set_credentials(username: str, password: str) -> None:
    """Enable login / set both username and a new (hashed) password."""
    if not (password or "").strip():
        raise ValueError("Password is required")
    set_username(username)
    db.set_setting(_HASH_KEY, generate_password_hash(password))


def set_username(username: str) -> None:
    db.set_setting(_USER_KEY, (username or "").strip() or DEFAULT_USERNAME)


def change_password(password: str) -> None:
    if not (password or "").strip():
        raise ValueError("Password is required")
    db.set_setting(_HASH_KEY, generate_password_hash(password))


def disable_auth() -> None:
    """Turn login off by clearing the stored hash."""
    db.set_setting(_HASH_KEY, "")


def verify(username: str, password: str) -> bool:
    """Constant-ish-time credential check. False when auth is disabled."""
    h = db.get_setting(_HASH_KEY) or ""
    if not h:
        return False
    user_ok = secrets.compare_digest(username or "", get_username())
    # Always run the hash check so a wrong username doesn't return faster.
    pass_ok = check_password_hash(h, password or "")
    return user_ok and pass_ok


def apply_env_reset() -> None:
    """Boot hook: honor LINEARR_AUTH_PASSWORD / LINEARR_AUTH_USERNAME.

    If a password env var is present, (re)set the stored credentials from the
    env on every start — the no-email reset. If only a username is set and none
    is stored yet, seed it (does NOT enable login on its own; a password does).
    """
    pw = os.environ.get("LINEARR_AUTH_PASSWORD", "").strip()
    user = os.environ.get("LINEARR_AUTH_USERNAME", "").strip()
    if pw:
        db.set_setting(_USER_KEY, user or db.get_setting(_USER_KEY) or DEFAULT_USERNAME)
        db.set_setting(_HASH_KEY, generate_password_hash(pw))
        log.warning(
            "Auth credentials (re)set from LINEARR_AUTH_PASSWORD. Login is now "
            "ENABLED — clear LINEARR_AUTH_PASSWORD from your environment after "
            "verifying you can log in (the hash persists in the database)."
        )
    elif user and not db.get_setting(_USER_KEY):
        db.set_setting(_USER_KEY, user)


def is_safe_next(target: str | None) -> bool:
    """Only allow local relative paths as the post-login redirect target,
    so `?next=` can't be abused for an open redirect."""
    if not target:
        return False
    return (
        target.startswith("/")
        and not target.startswith("//")
        and "\\" not in target
    )


# --------------------------------------------------------------------------- #
# Lightweight in-memory login throttle (per client IP)
# --------------------------------------------------------------------------- #

_MAX_FAILS = 5
_LOCKOUT_S = 60.0
_fails: dict[str, list] = {}   # ip -> [count, window_start]


def throttle_ok(ip: str) -> bool:
    """False when this IP is currently locked out after too many failures."""
    rec = _fails.get(ip)
    if not rec:
        return True
    count, start = rec
    if time.time() - start > _LOCKOUT_S:
        _fails.pop(ip, None)
        return True
    return count < _MAX_FAILS


def throttle_record_failure(ip: str) -> None:
    now = time.time()
    rec = _fails.get(ip)
    if not rec or now - rec[1] > _LOCKOUT_S:
        _fails[ip] = [1, now]
    else:
        rec[0] += 1


def throttle_clear(ip: str) -> None:
    _fails.pop(ip, None)
