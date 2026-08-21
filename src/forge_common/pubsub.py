"""Pub/Sub transport adapter: ordered publisher and DLQ-configured
subscriptions (ICD-5).

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
from typing import Any

from google.cloud import pubsub_v1


class OrderedPublisher:
    """Synchronous ordered publisher; demo-scale (one confirm per publish)."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self._client = pubsub_v1.PublisherClient(
            publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
        )

    def topic_path(self, topic: str) -> str:
        return self._client.topic_path(self.project_id, topic)

    def publish(self, topic: str, message: dict[str, Any], ordering_key: str) -> str:
        """Publish one bus message; blocks until the transport confirms."""
        future = self._client.publish(
            self.topic_path(topic),
            json.dumps(message, sort_keys=True).encode("utf-8"),
            ordering_key=ordering_key,
            schema_version=message["envelope"]["schema_version"],
        )
        return future.result(timeout=30)

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
) -> None:
    """Idempotently provision topic, DLQ topic, and an ordered subscription.

    The subscription carries ``dead_letter_policy`` (5 attempts per ICD-5)
    and ``enable_message_ordering=True``.
    """
    import contextlib

    from google.api_core.exceptions import AlreadyExists

    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    topic_path = publisher.topic_path(project_id, topic)
    for path in filter(
        None,
        [topic_path, dead_letter_topic and publisher.topic_path(project_id, dead_letter_topic)],
    ):
        with contextlib.suppress(AlreadyExists):
            publisher.create_topic(request={"name": path})
    request: dict[str, Any] = {
        "name": subscriber.subscription_path(project_id, subscription),
        "topic": topic_path,
        "enable_message_ordering": True,
    }
    if dead_letter_topic is not None:
        request["dead_letter_policy"] = {
            "dead_letter_topic": publisher.topic_path(project_id, dead_letter_topic),
            "max_delivery_attempts": max_delivery_attempts,
        }
    with contextlib.suppress(AlreadyExists):
        subscriber.create_subscription(request=request)
