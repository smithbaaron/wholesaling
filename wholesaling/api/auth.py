"""Single-operator HTTP Basic auth for the operator surface (/review, /policies).

Design (REQUIREMENTS.md POL-01 "human-visible event", ENG-Q13):

- One operator, identified by ``OPERATOR_USERNAME`` and verified against
  ``OPERATOR_PASSWORD_HASH``. The hash is PBKDF2-HMAC-SHA256 with a random salt, stored as
  ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`` so no new dependency is needed.
- Every comparison (username *and* password) runs in constant time through
  ``hmac.compare_digest``; a wrong username still performs the full PBKDF2 derivation so the
  response time does not reveal which half was wrong.
- Fail closed: without a configured hash the operator routes answer 503, never "open".
- Credentials are never logged. The only thing bound to the audit trail is the username.

Generate a hash::

    uv run python -c "from wholesaling.api.auth import hash_password; print(hash_password('pw'))"
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from wholesaling.config.settings import Settings
from wholesaling.logging import get_logger

log = get_logger(__name__)

SCHEME = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 600_000
# Used to keep the timing of a "no such user / malformed hash" path identical to a real check.
_DUMMY_SALT = bytes(16)

_basic = HTTPBasic(realm="wholesaling-operator", auto_error=False)


def hash_password(
    password: str, *, iterations: int = DEFAULT_ITERATIONS, salt: bytes | None = None
) -> str:
    """Return ``pbkdf2_sha256$iterations$salt_hex$hash_hex`` for ``password``."""
    if not password:
        msg = "password must not be empty"
        raise ValueError(msg)
    if iterations < 1:
        msg = "iterations must be positive"
        raise ValueError(msg)
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{SCHEME}${iterations}${salt.hex()}${digest.hex()}"


def _parse(stored: str) -> tuple[int, bytes, bytes] | None:
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != SCHEME:
        return None
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        digest = bytes.fromhex(parts[3])
    except ValueError:
        return None
    if iterations < 1 or not salt or not digest:
        return None
    return iterations, salt, digest


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check of ``password`` against a stored hash string.

    A missing or malformed ``stored`` value still performs a derivation with default cost so
    the failure path takes as long as a genuine mismatch.
    """
    parsed = _parse(stored or "")
    if parsed is None:
        hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _DUMMY_SALT, DEFAULT_ITERATIONS)
        return False
    iterations, salt, expected = parsed
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _settings(request: Request) -> Settings:
    settings = request.app.state.settings
    assert isinstance(settings, Settings)
    return settings


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="operator credentials required",
        headers={"WWW-Authenticate": 'Basic realm="wholesaling-operator"'},
    )


async def require_operator(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> str:
    """FastAPI dependency: authenticate the single operator and return the username.

    401 (with ``WWW-Authenticate``) when credentials are absent or wrong; 503 when no
    ``OPERATOR_PASSWORD_HASH`` is configured (fail closed).
    """
    settings = _settings(request)
    stored = (
        settings.operator_password_hash.get_secret_value()
        if settings.operator_password_hash is not None
        else ""
    )
    if not stored:
        log.error("operator_auth.unconfigured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operator auth is not configured (set OPERATOR_PASSWORD_HASH)",
        )
    if credentials is None:
        raise _unauthorized()
    # Compare the username in constant time too, and always run the password derivation so
    # a bad username costs exactly as much as a bad password.
    user_ok = hmac.compare_digest(
        credentials.username.encode("utf-8"), settings.operator_username.encode("utf-8")
    )
    pass_ok = verify_password(credentials.password, stored)
    if not (user_ok and pass_ok):
        log.warning("operator_auth.rejected", path=request.url.path)
        raise _unauthorized()
    return settings.operator_username


Operator = Annotated[str, Depends(require_operator)]

__all__ = [
    "DEFAULT_ITERATIONS",
    "SCHEME",
    "Operator",
    "hash_password",
    "require_operator",
    "verify_password",
]
