"""Pub/Sub transport adapter: ordered publisher, DLQ-configured
subscriptions, and DAT-1 regional residency (ICD-5, DAT-1).

Outside the emulator, clients pin the regional endpoint
(``pubsub.<region>.rep.googleapis.com``) and topics carry the regional
message-storage policy with ``enforce_in_transit`` — the DAT-1 map's
Pub/Sub half. CI-9 validates the deploy config against the map.

``OrderedPublisher`` publishes bus messages with message ordering enabled and
the ordering key supplied by the caller (``drain_outbox`` passes the
workflow_id). ``ensure_topic_and_subscription`` provisions the topic, its
dead-letter topic, and a subscription carrying ``dead_letter_policy`` with
``max_delivery_attempts=5`` and ordering enabled — the ICD-5 DLQ policy lives
here (and in the deploy config), not in consumer code.

Works against the Pub/Sub emulator (``PUBSUB_EMULATOR_HOST``) and production
identically; the emulator accepts and stores the dead-letter policy, though it
does not enforce forwarding — forwarding is verified in Lane 2 against real
infrastructure (CI-6).
"""

from __future__ import annotations

import json
import os
from typing import Any

from google.api_core.client_options import ClientOptions
from google.cloud import pubsub_v1

from forge_common import otel

DEFAULT_REGION = "us-central1"


def _regional_client_options(region: str) -> ClientOptions | None:
    """DAT-1: use the regional endpoint in production; the emulator env has
    no regional endpoints, so emulator runs use the default channel."""
    if os.environ.get("PUBSUB_EMULATOR_HOST"):
        return None
    return ClientOptions(api_endpoint=f"pubsub.{region}.rep.googleapis.com")


#: schema -> the roles whose subscriptions receive it. THE canonical bus
#: routing map: publish stamps a compact ``to_<role>`` attribute per target
#: (Pub/Sub filter expressions cap at 256 chars — discovered live, so role
#: filters are a single existence check: ``attributes:to_<role>``). CI-9
#: cross-checks this map against agents/registry.yaml contract declarations.
#: work_package_assignment routes DYNAMICALLY to the assigned role.
ROUTING: dict[str, tuple[str, ...]] = {
    "nmc_event.v2": ("orchestrator",),
    "maintenance_action_plan.v2": ("orchestrator", "safety"),
    "sourcing_report.v3": ("orchestrator", "safety"),
    "roster_assignment.v2": ("safety",),
    "quarantine_verdict.v3": ("safety",),
    "validation_verdict.v2": ("orchestrator",),
    "approval_decision.v2": ("orchestrator",),
    "due_event.v2": ("orchestrator",),
    "agent_failure_event.v2": ("orchestrator",),
    "approval_request.v2": (),  # consumed by the human surface, not the bus
}


def routing_attributes(message: dict[str, Any]) -> dict[str, str]:
    """Transport attributes for one message: schema + to_<role> stamps."""
    schema = message["envelope"]["schema_version"]
    attributes = {"schema_version": schema}
    if schema == "work_package_assignment.v2":
        attributes[f"to_{message['payload']['role']}"] = "1"
    else:
        for role in ROUTING.get(schema, ()):
            attributes[f"to_{role}"] = "1"
    return attributes


class OrderedPublisher:
    """Synchronous ordered publisher; demo-scale (one confirm per publish)."""

    def __init__(self, project_id: str, *, region: str = DEFAULT_REGION):
        self.project_id = project_id
        kwargs: dict[str, Any] = {}
        options = _regional_client_options(region)
        if options is not None:
            kwargs["client_options"] = options
        self._client = pubsub_v1.PublisherClient(
            publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True),
            **kwargs,
        )

    def topic_path(self, topic: str) -> str:
        return self._client.topic_path(self.project_id, topic)

    def publish(self, topic: str, message: dict[str, Any], ordering_key: str) -> str:
        """Publish one bus message; blocks until the transport confirms.

        W3C trace context rides the MESSAGE ATTRIBUTES (OBS-1): consumers
        extract it there; the envelope trace_id stays a read-only mirror.
        """
        attributes = routing_attributes(message)
        otel.inject_trace_attributes(attributes, message["envelope"])
        future = self._client.publish(
            self.topic_path(topic),
            json.dumps(message, sort_keys=True).encode("utf-8"),
            ordering_key=ordering_key,
            **attributes,
        )
        try:
            return future.result(timeout=30)
        except Exception:
            # one failed ordered publish PAUSES the key client-side; without
            # resume every later publish for this workflow from this process
            # fails instantly and the outbox never drains
            if ordering_key:
                self.resume(topic, ordering_key)
            raise

    def resume(self, topic: str, ordering_key: str) -> None:
        """Resume an ordering key paused by a publish failure."""
        self._client.resume_publish(self.topic_path(topic), ordering_key)


def ensure_topic_and_subscription(
    project_id: str,
    topic: str,
    subscription: str,
    *,
    dead_letter_topic: str | None = None,
    max_delivery_attempts: int = 5,
    region: str = DEFAULT_REGION,
) -> None:
    """Idempotently provision topic, DLQ topic, and an ordered subscription.

    The subscription carries ``dead_letter_policy`` (5 attempts per ICD-5)
    and ``enable_message_ordering=True``.
    """
    import contextlib

    from google.api_core.exceptions import AlreadyExists

    kwargs: dict[str, Any] = {}
    options = _regional_client_options(region)
    if options is not None:
        kwargs["client_options"] = options
    publisher = pubsub_v1.PublisherClient(**kwargs)
    subscriber = pubsub_v1.SubscriberClient(**kwargs)
    topic_path = publisher.topic_path(project_id, topic)
    for path in filter(
        None,
        [topic_path, dead_letter_topic and publisher.topic_path(project_id, dead_letter_topic)],
    ):
        request: dict[str, Any] = {"name": path}
        if options is not None:
            # DAT-1: regional message storage policy on every topic.
            request["message_storage_policy"] = {
                "allowed_persistence_regions": [region],
                "enforce_in_transit": True,
            }
        with contextlib.suppress(AlreadyExists):
            publisher.create_topic(request=request)
    sub_request: dict[str, Any] = {
        "name": subscriber.subscription_path(project_id, subscription),
        "topic": topic_path,
        "enable_message_ordering": True,
    }
    if dead_letter_topic is not None:
        sub_request["dead_letter_policy"] = {
            "dead_letter_topic": publisher.topic_path(project_id, dead_letter_topic),
            "max_delivery_attempts": max_delivery_attempts,
        }
    with contextlib.suppress(AlreadyExists):
        subscriber.create_subscription(request=sub_request)


def ensure_push_subscription(
    project_id: str,
    topic: str,
    subscription: str,
    *,
    push_endpoint: str,
    service_account: str,
    filter_expression: str,
    dead_letter_topic: str | None = None,
    max_delivery_attempts: int = 5,
    region: str = DEFAULT_REGION,
) -> None:
    """Idempotently provision one role's FILTERED, ORDERED push subscription.

    Push auth is the role's own service account via OIDC (AGT-6) — the
    worker deploys --no-allow-unauthenticated, so the platform admits only
    this subscription's pushes. Dead-letter after 5 attempts (ICD-5).
    Filters route each schema (and, for assignments, the wp_role attribute)
    to exactly the roles that consume it.
    """

    from google.api_core.exceptions import AlreadyExists

    kwargs: dict[str, Any] = {}
    options = _regional_client_options(region)
    if options is not None:
        kwargs["client_options"] = options
    publisher = pubsub_v1.PublisherClient(**kwargs)
    subscriber = pubsub_v1.SubscriberClient(**kwargs)
    request: dict[str, Any] = {
        "name": subscriber.subscription_path(project_id, subscription),
        "topic": publisher.topic_path(project_id, topic),
        "enable_message_ordering": True,
        "filter": filter_expression,
        "push_config": {
            "push_endpoint": push_endpoint,
            "oidc_token": {"service_account_email": service_account},
        },
        "ack_deadline_seconds": 120,
        # Redelivery credit must OUTLIVE the 120s claim lease: without a
        # retry backoff, a crashed holder's message burns all 5 attempts
        # against the unexpired lease (429s) within seconds and dead-letters
        # before the lease can expire — the documented lease-expiry recovery
        # would be unreachable.
        "retry_policy": {
            "minimum_backoff": {"seconds": 60},
            "maximum_backoff": {"seconds": 600},
        },
    }
    if dead_letter_topic:
        request["dead_letter_policy"] = {
            "dead_letter_topic": publisher.topic_path(project_id, dead_letter_topic),
            "max_delivery_attempts": max_delivery_attempts,
        }
    try:
        subscriber.create_subscription(request=request)
    except AlreadyExists:
        # idempotent CONVERGENCE, not just existence: mutable policies
        # (retry backoff, DLQ) are updated on re-run — a subscription created
        # by an older deploy must not keep an older policy silently
        subscriber.update_subscription(
            request={
                "subscription": {
                    "name": request["name"],
                    "retry_policy": request["retry_policy"],
                    **(
                        {"dead_letter_policy": request["dead_letter_policy"]}
                        if dead_letter_topic
                        else {}
                    ),
                },
                "update_mask": {
                    "paths": ["retry_policy"]
                    + (["dead_letter_policy"] if dead_letter_topic else [])
                },
            }
        )
