"""Model Armor screening adapter (PLT-4, SEC-1).

Explicit sanitize call against the regional Model Armor endpoint (DAT-1
map), mapping the response to the quarantine_verdict screening shape:
``{"verdict": "clean"|"flagged", "categories": [...]}``. Uses stdlib +
gcloud token like the Day-1 smoke; any transport or shape surprise raises,
which the pipeline treats as fail-closed (SEC-2). Unit and emulator tests
stub this adapter; the live call is exercised by scripts/smoke_model_armor
and the Lane 2 staging smoke (CI-6).
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from typing import Any


class ModelArmorScreen:
    def __init__(
        self, *, project_id: str, region: str = "us-central1", template_id: str = "forge-screening"
    ):
        self._url = (
            f"https://modelarmor.{region}.rep.googleapis.com/v1/projects/"
            f"{project_id}/locations/{region}/templates/{template_id}:sanitizeUserPrompt"
        )

    def __call__(self, text: str) -> dict[str, Any]:
        token = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        req = urllib.request.Request(
            self._url,
            data=json.dumps({"userPromptData": {"text": text}}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        result = body["sanitizationResult"]
        flagged = result.get("filterMatchState") == "MATCH_FOUND"
        categories = sorted(
            name
            for name, entry in result.get("filterResults", {}).items()
            if any(
                v == "MATCH_FOUND"
                for v in json.loads(json.dumps(entry)).values()
                if isinstance(v, str)
            )
            or json.dumps(entry).count("MATCH_FOUND") > 0
        )
        return {"verdict": "flagged" if flagged else "clean", "categories": categories}
