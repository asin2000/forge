"""Structured agent base on Google ADK: schema-constrained model output with
bounded retries (PLT-2, AGT-7, PLT-1).

Model execution runs through Google ADK — ``LlmAgent`` driven by a
``Runner`` with a fresh ``InMemorySessionService`` session per call (a fresh
session is also what the spec means by a reserve worker's empty
conversational state). Production passes a Vertex model string
(``gemini-3.5-flash``, routed to Vertex via ``GOOGLE_GENAI_USE_VERTEXAI``);
Lane 1 tests pass a ``BaseLlm`` stub — both take the identical ADK path, so
the pipeline under test is the pipeline that ships (PLT-2).

On top of ADK, :class:`StructuredAgent` enforces AGT-7: the final response
text is parsed as JSON, wrapped in an ICD-4 envelope, and contract-validated
in full — there is no raw-model-output-to-tool path. A malformed output is
retried at most ``MAX_RETRIES`` times (2), then the base returns a
contract-valid ``agent_failure_event.v2`` instead. Prompts are versioned
files in ``/prompts`` (PLT-1). ADK calls happen in bus handlers under the
claim/lease — never inside a transaction callback.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from forge_common.audit import now_iso
from forge_common.contracts import ContractViolation, validate_message
from forge_common.messages import build_envelope, deterministic_event_id

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

#: AGT-7: initial attempt + at most this many retries, then a failure event.
MAX_RETRIES = 2

_APP_NAME = "forge"
_INSTRUCTION = (
    "You are a FORGE agent operating on synthetic data. Follow the user "
    "message exactly. Respond with ONLY the requested JSON object — no "
    "prose, no code fences."
)


def load_prompt(name: str, variables: dict[str, Any]) -> str:
    """Load a versioned prompt file and substitute ``{{variable}}`` slots."""
    text = (PROMPTS_DIR / name).read_text()
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


class AdkTextRunner:
    """One ADK ``LlmAgent`` + ``Runner``; each call is a fresh session.

    ``model`` is a Vertex model string in production or a ``BaseLlm``
    instance (stub) in tests — either way execution goes through ADK.
    """

    def __init__(self, *, name: str, model: Any):
        self._agent = LlmAgent(
            name=name.replace("-", "_"),
            model=model,
            instruction=_INSTRUCTION,
        )
        self._sessions = InMemorySessionService()
        self._runner = Runner(app_name=_APP_NAME, agent=self._agent, session_service=self._sessions)

    def __call__(self, prompt: str) -> str:
        user_id = f"u-{uuid.uuid4().hex[:8]}"
        session = self._sessions.create_session_sync(app_name=_APP_NAME, user_id=user_id)
        final = ""
        for event in self._runner.run(
            user_id=user_id,
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(part.text or "" for part in event.content.parts)
        return final


class AgentOutputMalformed(Exception):
    """Model output failed its schema after all retries (AGT-7)."""

    def __init__(self, attempts: int, detail: str):
        self.attempts = attempts
        self.detail = detail
        super().__init__(f"malformed after {attempts} attempts: {detail}")


def constrained_json(
    runner: AdkTextRunner,
    prompt: str,
    validator: Any,
    *,
    max_retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    """Schema-constrained ADK call for INTERNAL (non-bus) outputs (AGT-7).

    Used by the Orchestrator, whose reasoning output is an internal
    decomposition object, not a bus message (its bus outputs — the work
    package assignments — are built from it and contract-validated
    separately). Retries ≤ ``max_retries``; raises AgentOutputMalformed on
    exhaustion — never returns unvalidated model text.
    """
    attempts = 0
    last_error = ""
    while attempts <= max_retries:
        attempts += 1
        raw = runner(prompt)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = f"unparseable: {exc}"
            continue
        errors = sorted(e.message for e in validator.iter_errors(parsed))
        if errors:
            last_error = "; ".join(errors[:3])
            continue
        return parsed
    raise AgentOutputMalformed(attempts, last_error)


class StructuredAgent:
    """One specialist role behind ADK execution + contract validation."""

    def __init__(self, *, agent_id: str, role: str, model: Any):
        self.agent_id = agent_id
        self.role = role
        self._runner = AdkTextRunner(name=agent_id, model=model)

    def run(
        self,
        *,
        prompt_file: str,
        variables: dict[str, Any],
        schema_version: str,
        workflow_id: str,
        work_package_id: str,
        trace_id: str,
        payload_check: Any = None,
    ) -> dict[str, Any]:
        """Produce ONE contract-valid bus message (or a failure event).

        Returns the validated domain message on success. After
        ``1 + MAX_RETRIES`` malformed outputs, returns a validated
        ``agent_failure_event.v2`` (failure_kind ``malformed_after_retries``)
        — the caller can trust that whatever comes back already passed
        contract validation.
        """
        prompt = load_prompt(prompt_file, variables)
        attempts = 0
        last_error = ""
        failure_kind = "malformed_after_retries"
        while attempts <= MAX_RETRIES:
            attempts += 1
            raw = self._runner(prompt)
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("model output is not a JSON object")
                message = self._wrap(
                    payload,
                    schema_version=schema_version,
                    workflow_id=workflow_id,
                    work_package_id=work_package_id,
                    trace_id=trace_id,
                    attempt=attempts,
                )
                validate_message(message)
                if payload_check is not None:
                    # Data-backed constraints (approved parts, qualification
                    # records, procedure rules): a model output contradicting
                    # the synthetic data sources is a violation, never
                    # published (AGT-2/AGT-3/AGT-4 boundaries).
                    data_errors = payload_check(payload)
                    if data_errors:
                        failure_kind = "contract_violation"
                        last_error = "data check failed: " + "; ".join(data_errors[:3])
                        continue
                return message
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = f"unparseable model output: {exc}"
                failure_kind = "malformed_after_retries"
            except ContractViolation as exc:
                last_error = f"contract violation: {exc}"
                failure_kind = "malformed_after_retries"
        failure = self._wrap(
            {
                "role": self.role,
                "agent_id": self.agent_id,
                "failure_kind": failure_kind,
                "attempts": attempts,
                "detail": last_error[:1000],
                "detected_at": now_iso(),
            },
            schema_version="agent_failure_event.v2",
            workflow_id=workflow_id,
            work_package_id=work_package_id,
            trace_id=trace_id,
            attempt=attempts,
        )
        validate_message(failure)
        return failure

    def _wrap(
        self,
        payload: dict[str, Any],
        *,
        schema_version: str,
        workflow_id: str,
        work_package_id: str,
        trace_id: str,
        attempt: int,
    ) -> dict[str, Any]:
        event_id = deterministic_event_id(
            "agent-out",
            workflow_id,
            work_package_id,
            self.agent_id,
            schema_version,
            str(attempt),
        )
        return {
            "envelope": build_envelope(
                workflow_id=workflow_id,
                work_package_id=work_package_id,
                schema_version=schema_version,
                event_id=event_id,
                trace_id=trace_id,
                idempotency_key=f"idem-{self.agent_id}-{event_id[:13]}",
            ),
            "payload": payload,
        }
