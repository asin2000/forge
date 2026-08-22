"""Approver identity from the AUTHENTICATED principal (HUM-1).

The surface runs in exactly one explicit mode (``FORGE_AUTH_MODE``, set by
the deployment — never inferred from request contents):

- ``cloudrun-iam`` (set by infra/deploy.sh for the Cloud Run deployment):
  identity comes ONLY from the ``Authorization`` bearer token. Cloud Run's
  front end verifies the caller's Google-signed ID token BEFORE the request
  can reach the container (``--no-allow-unauthenticated``), then forwards
  it with the signature STRIPPED (observed live 2026-08-21: in-container
  ``verify_oauth2_token`` raises ``MalformedError`` on a token the platform
  had just accepted), so the claims are read with issuer / verified-email /
  expiry checks. The plain ``x-goog-authenticated-user-email`` header is
  IGNORED in this mode: Cloud Run does not strip client-sent headers, so
  any authenticated invoker could forge it (Google's IAP guidance: do not
  rely on the plain header; only the signed assertion proves identity).

- ``iap``: identity comes ONLY from the SIGNED ``x-goog-iap-jwt-assertion``
  header, verified against IAP's public keys with the configured
  ``IAP_AUDIENCE`` and the ``https://cloud.google.com/iap`` issuer. The
  plain email header is display-only and never used here either.

- ``verify`` (default — local/dev/test): the bearer token is FULLY verified
  against Google's public keys.

No principal, an unknown mode, or a token that fails verification ->
PermissionError (the surface maps it to 401; verification failures are
never 500s). Verifiers are injectable so unit tests exercise every path
without Google round-trips.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

AUTH_MODE_ENV = "FORGE_AUTH_MODE"
IAP_ASSERTION_HEADER = "x-goog-iap-jwt-assertion"
#: The PLAIN IAP header. Never an identity source in ANY mode — kept only so
#: the surface can name it in logs/errors. See module docstring.
IAP_PLAIN_HEADER = "x-goog-authenticated-user-email"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
_GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


def _google_verify(token: str) -> str:
    """Full verification of a Google-signed ID token (mode: verify)."""
    import google.auth.transport.requests
    from google.oauth2 import id_token as google_id_token

    claims = google_id_token.verify_oauth2_token(token, google.auth.transport.requests.Request())
    email = claims.get("email")
    if not email or not claims.get("email_verified", True):
        raise PermissionError("ID token carries no verified email")
    return email


def _iap_verify(assertion: str) -> str:
    """Verify the SIGNED IAP assertion (mode: iap) — audience + issuer +
    signature against IAP's public keys. Fails closed if IAP_AUDIENCE is
    not configured."""
    import google.auth.transport.requests
    from google.oauth2 import id_token as google_id_token

    audience = os.environ.get("IAP_AUDIENCE")
    if not audience:
        raise PermissionError("IAP_AUDIENCE not configured; refusing to verify the assertion")
    claims = google_id_token.verify_token(
        assertion,
        google.auth.transport.requests.Request(),
        audience=audience,
        certs_url=IAP_CERTS_URL,
    )
    if claims.get("iss") != IAP_ISSUER:
        raise PermissionError("IAP assertion has a non-IAP issuer")
    email = claims.get("email")
    if not email:
        raise PermissionError("IAP assertion carries no email")
    return email


def _decode_platform_validated(token: str) -> str:
    """Claims of a token Cloud Run's IAM layer already verified (signature
    stripped in transit). Only reachable in mode cloudrun-iam."""
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


def _bearer(headers: Any) -> str | None:
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:]
    return None


def approver_from_request(
    headers: Any,
    *,
    mode: str | None = None,
    verifier: Any = _google_verify,
    iap_verifier: Any = _iap_verify,
) -> str:
    """Resolve the authenticated approver email or raise PermissionError.

    The plain ``x-goog-authenticated-user-email`` header is never consulted:
    it is client-forgeable in every deployment shape this surface supports.
    """
    mode = mode or os.environ.get(AUTH_MODE_ENV, "verify")
    if mode == "cloudrun-iam":
        token = _bearer(headers)
        if token is None:
            raise PermissionError("no platform-validated bearer token (mode: cloudrun-iam)")
        try:
            return _decode_platform_validated(token)
        except PermissionError:
            raise
        except Exception as exc:  # any verification failure is 401, never 500
            raise PermissionError(f"platform token rejected: {exc}") from exc
    if mode == "iap":
        assertion = headers.get(IAP_ASSERTION_HEADER)
        if not assertion:
            raise PermissionError(
                "no signed IAP assertion (mode: iap; the plain "
                f"{IAP_PLAIN_HEADER} header is not accepted as identity)"
            )
        try:
            return iap_verifier(assertion)
        except PermissionError:
            raise
        except Exception as exc:  # any verification failure is 401, never 500
            raise PermissionError(f"IAP assertion verification failed: {exc}") from exc
    if mode == "verify":
        token = _bearer(headers)
        if token is None:
            raise PermissionError("no bearer ID token (mode: verify)")
        try:
            return verifier(token)
        except PermissionError:
            raise
        except Exception as exc:  # any verification failure is 401, never 500
            raise PermissionError(f"ID token verification failed: {exc}") from exc
    raise PermissionError(f"unknown auth mode {mode!r}; refusing all principals")
