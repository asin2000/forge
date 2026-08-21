"""Structured agent base: schema-constrained model output with bounded
retries (AGT-7, PLT-1).

Every specialist and the Orchestrator reason through :class:`StructuredAgent`.
The model callable returns text; the base parses it as JSON, wraps it in an
ICD-4 envelope, and contract-validates the FULL message — there is no
raw-model-output-to-tool path. A malformed output is retried at most
``MAX_RETRIES`` times (2), then the base returns a contract-valid
``agent_failure_event.v2`` instead (AGT-7); the caller publishes it and never
sees unvalidated model text.

Prompts are versioned files in ``/prompts`` (PLT-1); the model callable is
injected — stubbed in Lane 1 tests, live ``gemini-3.5-flash`` on Vertex in
the deployed services. Model calls happen in bus handlers under the
claim/lease — never inside a transaction callback.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forge_common.audit import now_iso
from forge_common.contracts import ContractViolation, validate_message
from forge_common.messages import build_envelope, deterministic_event_id

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

#: AGT-7: initial attempt + at most this many retries, then a failure event.
MAX_RETRIES = 2


def load_prompt(name: str, variables: dict[str, Any]) -> str:
    """Load a versioned prompt file and substitute ``{{variable}}`` slots."""
    text = (PROMPTS_DIR / name).read_text()
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


class AgentOutputMalformed(Exception):
    """Model output failed its schema after all retries (AGT-7)."""

    def __init__(self, attempts: int, detail: str):
        self.attempts = attempts
        self.detail = detail
        super().__init__(f"malformed after {attempts} attempts: {detail}")


def constrained_json(
    model: Callable[[str], str],
    prompt: str,
    validator: Any,
    *,
    max_retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    """Schema-constrained model call for INTERNAL (non-bus) outputs (AGT-7).

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
        raw = model(prompt)
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
    """Wraps one model callable behind contract validation (AGT-7)."""

    def __init__(
        self,
        *,
        agent_id: str,
        role: str,
        model: Callable[[str], str],
    ):
        self.agent_id = agent_id
        self.role = role
        self._model = model

    def run(
        self,
        *,
        prompt_file: str,
        variables: dict[str, Any],
        schema_version: str,
        workflow_id: str,
        work_package_id: str,
        trace_id: str,
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
        while attempts <= MAX_RETRIES:
            attempts += 1
            raw = self._model(prompt)
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
                return message
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = f"unparseable model output: {exc}"
            except ContractViolation as exc:
                last_error = f"contract violation: {exc}"
        failure = self._wrap(
            {
                "role": self.role,
                "agent_id": self.agent_id,
                "failure_kind": "malformed_after_retries",
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
