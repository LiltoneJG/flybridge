from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import asdict
from math import isfinite

from flybridge_application import QueueLeaseNotifier, WorkflowService
from flybridge_core import ResourceQueue, WorkflowStore

from ..runtime import _adapter, _config, _running_owner

_WATCH_ERRORS = (OSError, RuntimeError, ValueError, sqlite3.Error)


def _refresh_activation(store: WorkflowStore, workflow_id: str | None) -> None:
    if not workflow_id:
        return
    try:
        store.mark_resumed(str(workflow_id))
    except ValueError:
        return


def _promoted_owner(queue: ResourceQueue, lease_id: str | None) -> str | None:
    if not lease_id:
        return None
    try:
        return str(queue.inspect(lease_id)["owner"])
    except ValueError:
        return None


def handle(args: argparse.Namespace) -> int:
    config = _config(args)
    queue = ResourceQueue(config.state_dir)
    store = WorkflowStore(config.state_dir)
    try:
        WorkflowService(store, queue)
    except sqlite3.Error as exc:
        if getattr(args, "queue_command", None) != "watch":
            raise
        print(json.dumps({"event": "workflow_reconcile_failed", "error": str(exc)}), flush=True)
    if args.queue_command == "acquire":
        owner = _running_owner(store, args.owner)
        existing = next(
            (item for item in queue.owner_requests(owner) if item["resource"] == args.resource),
            None,
        )
        result = queue.acquire(args.resource, owner)
        try:
            _running_owner(store, owner)
        except ValueError as exc:
            queue.cancel_owner(owner)
            raise ValueError("owner workflow stopped during resource acquisition") from exc
        if result.granted and (existing is None or existing["status"] != "leased"):
            _refresh_activation(store, owner)
        print(json.dumps(asdict(result)))
    elif args.queue_command == "release":
        next_lease_id = queue.release(args.lease, resource=args.resource, owner=args.owner)
        _refresh_activation(store, args.owner)
        _refresh_activation(store, _promoted_owner(queue, next_lease_id))
        print(json.dumps({"next_lease_id": next_lease_id}))
    elif args.queue_command == "cancel":
        next_lease_id = queue.cancel(args.request)
        _refresh_activation(store, _promoted_owner(queue, next_lease_id))
        print(json.dumps({"next_lease_id": next_lease_id}))
    elif args.queue_command == "recover":
        promoted = queue.recover_stale(args.older_than_seconds)
        for lease_id in promoted:
            _refresh_activation(store, _promoted_owner(queue, lease_id))
        print(json.dumps({"promoted_lease_ids": promoted}))
    elif args.queue_command == "status":
        summary = queue.status(args.resource)
        if args.details:
            requests = queue.active_requests(
                resource=args.resource,
                attention_after_seconds=config.queue_lease_timeout_seconds,
            )
            print(
                json.dumps(
                    {
                        "summary": summary,
                        "requests": requests,
                        "attention_after_seconds": config.queue_lease_timeout_seconds,
                    },
                    indent=2,
                )
            )
        else:
            print(json.dumps(summary, indent=2))
    elif args.queue_command == "inspect":
        print(json.dumps(queue.inspect(args.request_id, owner=args.owner), indent=2))
    else:
        if (
            isinstance(args.interval, bool)
            or not isinstance(args.interval, (int, float))
            or not isfinite(args.interval)
            or args.interval <= 0
        ):
            raise ValueError("queue watch interval must be a finite positive number")
        notify_workflow = getattr(args, "notify_workflow", None)
        notifier = None
        if notify_workflow and not args.once:
            store.get(notify_workflow)
            notifier = QueueLeaseNotifier(
                queue,
                store,
                _adapter(config),
                notify_workflow,
                config_path=config.path,
            )
        cursor = 0
        last_owner_check_error: str | None = None
        print(json.dumps({"event": "snapshot", "queues": queue.status()}), flush=True)
        while True:
            try:
                if notifier is not None:
                    try:
                        owner_available = notifier.owner_terminal_is_available()
                    except _WATCH_ERRORS as exc:
                        error = str(exc)
                        if error != last_owner_check_error:
                            print(
                                json.dumps(
                                    {
                                        "event": "owner_terminal_check_failed",
                                        "workflow_id": notify_workflow,
                                        "error": error,
                                    }
                                ),
                                flush=True,
                            )
                        last_owner_check_error = error
                        owner_available = True
                    else:
                        last_owner_check_error = None
                    if not owner_available:
                        print(
                            json.dumps(
                                {
                                    "event": "owner_terminal_unavailable",
                                    "workflow_id": notify_workflow,
                                }
                            ),
                            flush=True,
                        )
                        handles: list[str] = []
                        try:
                            handles = notifier.detach_if_owner_terminal_unavailable()
                        except _WATCH_ERRORS as exc:
                            print(
                                json.dumps(
                                    {
                                        "event": "observer_detach_failed",
                                        "workflow_id": notify_workflow,
                                        "error": str(exc),
                                    }
                                ),
                                flush=True,
                            )
                        try:
                            close_errors = notifier.close_observers(handles)
                        except _WATCH_ERRORS as exc:
                            close_errors = [str(exc)]
                        if close_errors:
                            print(
                                json.dumps(
                                    {
                                        "event": "observer_terminal_close_failed",
                                        "workflow_id": notify_workflow,
                                        "errors": close_errors,
                                    }
                                ),
                                flush=True,
                            )
                        return 0
                events = queue.events(cursor)
                for event in events:
                    cursor = int(event["sequence"])
                    print(json.dumps(event), flush=True)
                    if notifier is not None:
                        notifier.note_event(event)
                if notifier is not None:
                    notification = notifier.notify_due()
                    if notification is not None:
                        print(json.dumps(notification), flush=True)
            except _WATCH_ERRORS as exc:
                print(
                    json.dumps({"event": "watch_iteration_failed", "error": str(exc)}),
                    flush=True,
                )
                events = []
            if len(events) == 1000:
                continue
            if args.once:
                return 0
            time.sleep(args.interval)
    return 0
