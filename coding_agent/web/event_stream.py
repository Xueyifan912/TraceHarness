"""In-process SSE fan-out for Web run events."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator

from ..runtime.events import register_event_listener

TERMINAL_EVENT_TYPES = {"run_completed", "run_failed", "run_cancelled"}
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
DEFAULT_HEARTBEAT_SECONDS = 15.0


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _normalise_event(event_id: str, record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return {
        "event_id": str(record.get("event_id") or event_id),
        "ts": record.get("ts") or _utc_timestamp(),
        "type": str(record.get("type") or "event"),
        "session_id": record.get("session_id"),
        "run_id": record.get("run_id"),
        "source": record.get("source"),
        "payload": payload,
    }


def _dedupe_key(event: dict[str, Any]) -> str:
    event_id = event.get("event_id")
    if event_id:
        return str(event_id)
    return json.dumps(
        {
            "ts": event.get("ts"),
            "type": event.get("type"),
            "session_id": event.get("session_id"),
            "run_id": event.get("run_id"),
            "payload": event.get("payload"),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def format_sse(event: dict[str, Any]) -> str:
    data = json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":"))
    event_type = str(event.get("type") or "event")
    event_id = str(event.get("event_id") or "").replace("\r", "").replace("\n", "")
    id_line = (
        f"id: {event_id}\n"
        if event_id and event_type not in {"heartbeat", "stream_gap"}
        else ""
    )
    return id_line + f"event: {event_type}\ndata: {data}\n\n"


@dataclass(eq=False)
class EventSubscriber:
    session_id: str
    run_id: str
    max_queue_size: int
    queue: queue.Queue[dict[str, Any]] = field(init=False)
    dropped_events: int = 0
    reported_dropped_events: int = 0
    closed: bool = False

    def __post_init__(self) -> None:
        self.queue = queue.Queue(maxsize=self.max_queue_size)


class EventStreamHub:
    def __init__(
        self,
        *,
        history_size: int = 500,
        subscriber_queue_size: int = 100,
    ):
        self._guard = threading.Lock()
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._subscribers: set[EventSubscriber] = set()
        self._next_event_number = 0
        self.subscriber_queue_size = subscriber_queue_size

    def publish(self, record: dict[str, Any]) -> None:
        with self._guard:
            self._next_event_number += 1
            event = _normalise_event(f"live_{self._next_event_number}", record)
            self._history.append(event)
            subscribers = [
                subscriber
                for subscriber in self._subscribers
                if self._matches(subscriber, event)
            ]

        for subscriber in subscribers:
            self._enqueue_bounded(subscriber, event)

    def subscribe(self, session_id: str, run_id: str) -> EventSubscriber:
        subscriber = EventSubscriber(
            session_id=session_id,
            run_id=run_id,
            max_queue_size=self.subscriber_queue_size,
        )
        with self._guard:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        subscriber.closed = True
        with self._guard:
            self._subscribers.discard(subscriber)

    def iter_sse(
        self,
        *,
        session_id: str,
        run_id: str,
        replay_loader: Callable[[], Iterable[dict[str, Any]]] | None = None,
        run_lookup: Callable[[], Any] | None = None,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> Iterator[str]:
        subscriber = self.subscribe(session_id, run_id)
        seen_keys: set[str] = set()
        terminal_seen = False

        try:
            if replay_loader is not None:
                for event in replay_loader():
                    normalised = _normalise_event(
                        str(event.get("event_id") or "replay"),
                        event,
                    )
                    if normalised.get("type") in TERMINAL_EVENT_TYPES:
                        terminal_seen = True
                    seen_keys.add(_dedupe_key(normalised))
                    yield format_sse(normalised)

            terminal_from_live = yield from self._drain_live(
                subscriber,
                seen_keys,
            )
            terminal_seen = terminal_seen or terminal_from_live
            if self._run_is_terminal(run_lookup):
                if not terminal_seen:
                    yield format_sse(self._terminal_event_from_run(run_lookup))
                return

            while True:
                try:
                    event = subscriber.queue.get(timeout=heartbeat_seconds)
                except queue.Empty:
                    gap_event = self._pending_gap_event(subscriber)
                    if gap_event is not None:
                        yield format_sse(gap_event)
                    if self._run_is_terminal(run_lookup):
                        yield format_sse(self._terminal_event_from_run(run_lookup))
                        return
                    yield format_sse(self._heartbeat(session_id, run_id))
                    continue

                key = _dedupe_key(event)
                gap_event = self._pending_gap_event(subscriber)
                if gap_event is not None:
                    yield format_sse(gap_event)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                yield format_sse(event)
                if event.get("type") in TERMINAL_EVENT_TYPES:
                    return
        finally:
            self.unsubscribe(subscriber)

    def _drain_live(
        self,
        subscriber: EventSubscriber,
        seen_keys: set[str],
    ) -> Iterator[str]:
        terminal_seen = False
        while True:
            try:
                event = subscriber.queue.get_nowait()
            except queue.Empty:
                break
            key = _dedupe_key(event)
            gap_event = self._pending_gap_event(subscriber)
            if gap_event is not None:
                yield format_sse(gap_event)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if event.get("type") in TERMINAL_EVENT_TYPES:
                terminal_seen = True
            yield format_sse(event)
        return terminal_seen

    @staticmethod
    def _pending_gap_event(
        subscriber: EventSubscriber,
    ) -> dict[str, Any] | None:
        if subscriber.dropped_events <= subscriber.reported_dropped_events:
            return None
        dropped = (
            subscriber.dropped_events
            - subscriber.reported_dropped_events
        )
        subscriber.reported_dropped_events = subscriber.dropped_events
        return {
            "event_id": "",
            "ts": _utc_timestamp(),
            "type": "stream_gap",
            "session_id": subscriber.session_id,
            "run_id": subscriber.run_id,
            "source": "web",
            "payload": {
                "dropped_events": dropped,
                "resync_required": True,
            },
        }

    @staticmethod
    def _matches(subscriber: EventSubscriber, event: dict[str, Any]) -> bool:
        return (
            event.get("session_id") == subscriber.session_id
            and event.get("run_id") == subscriber.run_id
        )

    @staticmethod
    def _enqueue_bounded(
        subscriber: EventSubscriber,
        event: dict[str, Any],
    ) -> None:
        try:
            subscriber.queue.put_nowait(event)
            return
        except queue.Full:
            subscriber.dropped_events += 1
        try:
            subscriber.queue.get_nowait()
        except queue.Empty:
            pass
        try:
            subscriber.queue.put_nowait(event)
        except queue.Full:
            subscriber.dropped_events += 1

    @staticmethod
    def _run_status(run: Any) -> str | None:
        if run is None:
            return None
        if isinstance(run, dict):
            status = run.get("status")
        else:
            status = getattr(run, "status", None)
        return str(status) if status is not None else None

    def _run_is_terminal(self, run_lookup: Callable[[], Any] | None) -> bool:
        if run_lookup is None:
            return False
        try:
            return self._run_status(run_lookup()) in TERMINAL_RUN_STATUSES
        except Exception:
            return False

    def _terminal_event_from_run(
        self,
        run_lookup: Callable[[], Any] | None,
    ) -> dict[str, Any]:
        run = run_lookup() if run_lookup is not None else None
        if isinstance(run, dict):
            run_payload = dict(run)
        elif hasattr(run, "to_dict"):
            run_payload = run.to_dict()
        else:
            run_payload = {}
        status = str(run_payload.get("status") or "completed")
        event_type = {
            "failed": "run_failed",
            "cancelled": "run_cancelled",
        }.get(status, "run_completed")
        return {
            "event_id": f"synthetic_{event_type}_{run_payload.get('run_id')}",
            "ts": run_payload.get("ended_at") or _utc_timestamp(),
            "type": event_type,
            "session_id": run_payload.get("session_id"),
            "run_id": run_payload.get("run_id"),
            "source": "web",
            "payload": {"run": run_payload},
        }

    @staticmethod
    def _heartbeat(session_id: str, run_id: str) -> dict[str, Any]:
        timestamp = _utc_timestamp()
        return {
            "event_id": f"heartbeat_{timestamp}",
            "ts": timestamp,
            "type": "heartbeat",
            "session_id": session_id,
            "run_id": run_id,
            "source": "web",
            "payload": {},
        }


EVENT_HUB = EventStreamHub()
_UNSUBSCRIBE_RUNTIME_EVENTS = register_event_listener(EVENT_HUB.publish)
