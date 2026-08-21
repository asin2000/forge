"""Model Armor screening adapter (PLT-4, SEC-1, SEC-2).

Explicit sanitize call against the regional Model Armor endpoint (DAT-1
map). FAIL-CLOSED contract: this adapter returns
``{"verdict": "clean"|"flagged", "categories": [...]}`` ONLY for a fully
successful screening — Google's ``invocationResult`` values ``PARTIAL`` and
``FAILURE`` mean incomplete filter execution and RAISE, as do missing
fields, malformed JSON, transport errors, timeouts, and auth failures. The
pipeline maps any raise to SEC-2 quarantine.

Auth uses Application Default Credentials — on Cloud Run that is the Cyber
Trust service identity (AGT-6); no gcloud shell-out. The token provider and
HTTP transport are injectable for the response-shape test matrix.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class ModelArmorError(Exception):
    """Screening could not be completed reliably — fail closed (SEC-2)."""


def _adc_token() -> str:
    import google.auth
    import google.auth.transport.requests

    credentials, _project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


def _http_post(url: str, body: dict[str, Any], token: str, timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class ModelArmorScreen:
    def __init__(
        self,
        *,
        project_id: str,
        region: str = "us-central1",
        template_id: str = "forge-screening",
        token_provider: Any = _adc_token,
        http_post: Any = _http_post,
        timeout: int = 30,
    ):
        self._url = (
            f"https://modelarmor.{region}.rep.googleapis.com/v1/projects/"
            f"{project_id}/locations/{region}/templates/{template_id}:sanitizeUserPrompt"
        )
        self._token_provider = token_provider
        self._http_post = http_post
        self._timeout = timeout

    def __call__(self, text: str) -> dict[str, Any]:
        try:
            token = self._token_provider()
        except Exception as exc:
            raise ModelArmorError(f"auth failure: {type(exc).__name__}: {exc}") from exc
        try:
            body = self._http_post(
                self._url, {"userPromptData": {"text": text}}, token, self._timeout
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelArmorError(f"transport failure: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ModelArmorError(f"malformed response JSON: {exc}") from exc
        return interpret_sanitization(body)


def interpret_sanitization(body: dict[str, Any]) -> dict[str, Any]:
    """Map a SanitizationResult to the screening shape — strictly.

    ``invocationResult`` must be SUCCESS: PARTIAL and FAILURE mean one or
    more filters did not execute, so treating them as clean would FAIL OPEN.
    ``filterMatchState`` must be MATCH_FOUND or NO_MATCH_FOUND. Anything
    else — including missing fields — raises.
    """
    result = body.get("sanitizationResult")
    if not isinstance(result, dict):
        raise ModelArmorError(f"missing sanitizationResult in response: {body!r}")
    invocation = result.get("invocationResult", "SUCCESS")
    if invocation != "SUCCESS":
        raise ModelArmorError(
            f"incomplete filter execution: invocationResult={invocation} (fail closed)"
        )
    match_state = result.get("filterMatchState")
    if match_state not in ("MATCH_FOUND", "NO_MATCH_FOUND"):
        raise ModelArmorError(f"unusable filterMatchState: {match_state!r}")

    def _matched(entry: Any) -> bool:
        # Exact value walk — a substring test would also match NO_MATCH_FOUND.
        if isinstance(entry, dict):
            return any(_matched(v) for v in entry.values())
        return entry == "MATCH_FOUND"

    categories = sorted(
        name for name, entry in result.get("filterResults", {}).items() if _matched(entry)
    )
    # Defense in depth: flagged when the aggregate OR any individual filter
    # matched — a per-filter match with a clean aggregate still flags.
    flagged = match_state == "MATCH_FOUND" or bool(categories)
    return {"verdict": "flagged" if flagged else "clean", "categories": categories}
