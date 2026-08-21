"""Approver identity from the AUTHENTICATED principal (HUM-1).

The approval surface runs behind Cloud Run IAM or IAP. The approver identity
is derived server-side from the platform's authentication artifacts — NEVER
from client-supplied fields:

- Behind IAP: the ``x-goog-authenticated-user-email`` header (set by the
  proxy, stripped of its ``accounts.google.com:`` prefix).
- Behind Cloud Run IAM (``--no-allow-unauthenticated``): Cloud Run's front
  end verifies the caller's Google-signed ID token BEFORE the request can
  reach the container, then forwards the token with its signature STRIPPED
  (observed live 2026-08-21: in-container ``verify_oauth2_token`` raises
  ``MalformedError: Could not verify token signature`` on a token the
  platform had just accepted). So when ``TRUST_PLATFORM_AUTH=1`` — set ONLY
  by the Cloud Run deploy, where the platform gate is the proof of
  authenticity — the identity is read from the platform-validated token's
  claims (issuer, verified email, expiry still checked). Anywhere else the
  token is fully verified against Google's public keys.

No principal, or a token that fails verification -> PermissionError (the
surface maps it to 401; verification failures are never 500s). The verifier
is injectable so unit tests exercise every path without Google round-trips.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

IAP_HEADER = "x-goog-authenticated-user-email"
PLATFORM_TRUST_ENV = "TRUST_PLATFORM_AUTH"
_GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


def _google_verify(token: str) -> str:
    import google.auth.transport.requests
    from google.oauth2 import id_token as google_id_token

    claims = google_id_token.verify_oauth2_token(token, google.auth.transport.requests.Request())
    email = claims.get("email")
    if not email or not claims.get("email_verified", True):
        raise PermissionError("ID token carries no verified email")
    return email


def _decode_platform_validated(token: str) -> str:
    """Claims of a token Cloud Run's IAM layer already verified (signature
    stripped in transit). Only reachable when TRUST_PLATFORM_AUTH=1."""
    parts = token.split(".")
    if len(parts) not in (2, 3) or not parts[1]:
        raise PermissionError("malformed platform token")
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise PermissionError("undecodable platform token") from exc
    if claims.get("iss") not in _GOOGLE_ISSUERS:
        raise PermissionError("platform token has a non-Google issuer")
    if int(claims.get("exp", 0)) < time.time():
        raise PermissionError("platform token expired")
    email = claims.get("email")
    if not email or claims.get("email_verified") is not True:
        raise PermissionError("platform token carries no verified email")
    return email


def approver_from_request(headers: Any, *, verifier: Any = _google_verify) -> str:
    """Resolve the authenticated approver email or raise PermissionError."""
    iap = headers.get(IAP_HEADER)
    if iap:
        return iap.split(":", 1)[-1]
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:]
        if os.environ.get(PLATFORM_TRUST_ENV) == "1":
            return _decode_platform_validated(token)
        try:
            return verifier(token)
        except PermissionError:
            raise
        except Exception as exc:  # any verification failure is 401, never 500
            raise PermissionError(f"ID token verification failed: {exc}") from exc
    raise PermissionError("no authenticated principal (IAP header or verified ID token required)")
