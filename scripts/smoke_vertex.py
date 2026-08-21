#!/usr/bin/env python3
"""Day-1 smoke test (risk R2): gemini-3.5-flash reachable on Vertex AI (PLT-1).

Usage: PROJECT_ID=your-project python scripts/smoke_vertex.py
Requires: gcloud application-default credentials (gcloud auth application-default login).
"""

import os
import sys

from google import genai

PROJECT_ID = os.environ.get("PROJECT_ID")
REGION = os.environ.get("REGION", "us")  # gemini lives at the us multi-region (DAT-1 map)
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")


def main() -> int:
    if not PROJECT_ID:
        print("FATAL: set PROJECT_ID")
        return 2
    client = genai.Client(vertexai=True, project=PROJECT_ID, location=REGION)
    resp = client.models.generate_content(
        model=MODEL, contents="Reply with exactly: FORGE-SMOKE-OK"
    )
    text = (resp.text or "").strip()
    print(f"model={MODEL} region={REGION} response={text!r}")
    if "FORGE-SMOKE-OK" in text:
        print("VERTEX SMOKE: PASS")
        return 0
    print("VERTEX SMOKE: FAIL (unexpected response)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
