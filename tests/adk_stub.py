"""Stub LLM for Lane 1 tests: canned replies through the REAL ADK pipeline.

The stub subclasses ``google.adk.models.BaseLlm``, so tests exercise the
actual ``LlmAgent`` + ``Runner`` execution path (PLT-2) with deterministic
text — only the model weights are faked, never the framework.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from google.adk.models import BaseLlm, LlmResponse
from google.genai import types
from pydantic import PrivateAttr


class StubLlm(BaseLlm):
    """Replies with each string in ``replies`` in turn; the last repeats."""

    model: str = "stub-model"
    replies: tuple[str, ...] = ("{}",)
    _calls: list[str] = PrivateAttr(default_factory=list)

    supported: ClassVar = None

    @classmethod
    def supported_models(cls) -> list[str]:
        return [".*"]

    @property
    def call_count(self) -> int:
        return len(self._calls)

    async def generate_content_async(self, llm_request: Any, stream: bool = False):
        index = min(len(self._calls), len(self.replies) - 1)
        self._calls.append("call")
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=self.replies[index])])
        )


def stub_json(payload: dict) -> StubLlm:
    """A stub that always answers with one JSON payload."""
    return StubLlm(replies=(json.dumps(payload),))


def stub_garbage() -> StubLlm:
    """A stub that never produces valid JSON (AGT-7 exhaustion path)."""
    return StubLlm(replies=("NOT JSON AT ALL",))
