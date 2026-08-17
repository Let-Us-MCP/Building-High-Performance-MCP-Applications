"""The Tasks extension: `io.modelcontextprotocol/tasks`.

Tasks moved out of the core protocol and into an official extension in
2026-07-28, and got redesigned on the way. The blocking `tasks/result` is gone,
replaced by polling `tasks/get`. `tasks/list` is gone. `tasks/update` arrived so
a running task can ask for input without anyone opening a second channel. And
the server now decides, per request, whether to hand back a task at all; the
client opts in once through its capabilities and then handles whatever shape
turns up.

A task id is a durable handle. That is the entire value proposition. Your
laptop can close its lid mid-underwriting and pick the same task back up
twenty minutes later, because nothing about the work was tied to a connection.

The cost is a polling loop, which is not free, and Chapter 9 argues about when
the synchronous path is simply cheaper.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from . import errors, jsonrpc
from .meta import RequestContext

EXTENSION_ID = "io.modelcontextprotocol/tasks"

WORKING = "working"
INPUT_REQUIRED = "input_required"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})


@dataclass
class Task:
    task_id: str
    status: str = WORKING
    status_message: str | None = None
    result: dict | None = None
    error: dict | None = None
    input_requests: dict = field(default_factory=dict)
    request_state: str | None = None
    created_at: float = field(default_factory=time.time)
    ttl_ms: int = 3_600_000
    poll_interval_ms: int = 500
    progress: float = 0.0
    total: float | None = None

    def to_json(self) -> dict:
        out: dict[str, Any] = {
            "taskId": self.task_id,
            "status": self.status,
            "ttlMs": self.ttl_ms,
            "pollIntervalMs": self.poll_interval_ms,
        }
        if self.status_message:
            out["statusMessage"] = self.status_message
        if self.progress:
            out["progress"] = self.progress
        if self.total is not None:
            out["total"] = self.total
        if self.status == COMPLETED and self.result is not None:
            out["result"] = self.result
        if self.status == FAILED and self.error is not None:
            out["error"] = self.error
        if self.status == INPUT_REQUIRED and self.input_requests:
            out["inputRequests"] = self.input_requests
            if self.request_state:
                out["requestState"] = self.request_state
        return out

    @property
    def expired(self) -> bool:
        return time.time() > self.created_at + self.ttl_ms / 1000.0


class TaskStore:
    """Where tasks live between polls.

    In-memory here, which is a lie a book is allowed to tell once. Any real
    deployment needs this in something durable and shared, because the whole
    point is that the next poll may land on a different replica. The moment you
    put tasks in a process-local dict behind a load balancer, you have
    reinvented sticky sessions, which is the thing this revision deleted.
    """

    def __init__(self, *, default_ttl_ms: int = 3_600_000, server=None):
        self._tasks: dict[str, Task] = {}
        self._lock = threading.RLock()
        self.default_ttl_ms = default_ttl_ms
        # Set by `install`, so an update can push as well as be polled for.
        self.server = server

    def create(self, **kw) -> Task:
        task = Task(task_id="tsk_" + uuid.uuid4().hex[:16],
                    ttl_ms=kw.pop("ttl_ms", self.default_ttl_ms), **kw)
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Task:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None or task.expired:
            raise errors.InvalidParams(f"Unknown or expired task: {task_id}")
        return task

    def update(self, task_id: str, **fields) -> Task:
        """Change a task's state, and tell anybody who asked to be told.

        Every state change goes through here, which is what makes the push
        path trustworthy: there is no way to move a task to `completed` without
        the notification going out.
        """
        task = self.get(task_id)
        with self._lock:
            for key, value in fields.items():
                setattr(task, key, value)
        if self.server is not None:
            self.server.notify_task(task.to_json())
        return task

    def sweep(self) -> int:
        with self._lock:
            dead = [k for k, v in self._tasks.items() if v.expired]
            for k in dead:
                del self._tasks[k]
        return len(dead)

    def __len__(self) -> int:
        with self._lock:
            return len(self._tasks)


def install(server, store: TaskStore | None = None) -> TaskStore:
    """Add the Tasks extension to a server.

    Registers `tasks/get`, `tasks/update`, and `tasks/cancel`, and advertises
    the extension in `server/discover`.
    """
    store = store or TaskStore()
    server.declare_extension(EXTENSION_ID, {})
    store.server = server
    server.tasks = store

    @server.method("tasks/get")
    def _get(ctx: RequestContext) -> dict:
        task_id = ctx.params.get("taskId")
        if not isinstance(task_id, str):
            raise errors.InvalidParams("tasks/get requires a string `taskId`")
        return store.get(task_id).to_json()

    @server.method("tasks/update")
    def _update(ctx: RequestContext) -> dict:
        """Deliver answers to a task waiting on input.

        Unknown or already-satisfied keys are ignored rather than rejected. A
        client that retried a `tasks/update` after a timeout should not get an
        error for being careful.
        """
        task_id = ctx.params.get("taskId")
        if not isinstance(task_id, str):
            raise errors.InvalidParams("tasks/update requires a string `taskId`")
        task = store.get(task_id)
        responses = ctx.params.get("inputResponses") or {}
        handler = getattr(task, "_resume", None)
        if task.status == INPUT_REQUIRED and callable(handler):
            handler(responses)
        return {}

    @server.method("tasks/cancel")
    def _cancel(ctx: RequestContext) -> dict:
        """Cancellation is cooperative. We acknowledge the intent; the work may
        still finish, and the task may still land in `completed`."""
        task_id = ctx.params.get("taskId")
        if not isinstance(task_id, str):
            raise errors.InvalidParams("tasks/cancel requires a string `taskId`")
        task = store.get(task_id)
        if task.status not in TERMINAL:
            store.update(task_id, status=CANCELLED,
                         status_message="cancelled by client")
        return {}

    return store


def create_task_result(task: Task) -> dict:
    """The `resultType: "task"` envelope a server returns instead of an answer.

    Only ever return this to a client that declared the extension. Returning a
    shape the client cannot parse is worse than being slow.
    """
    return {"resultType": jsonrpc.RESULT_TASK, "task": task.to_json()}


def run_in_background(store: TaskStore, task: Task,
                      work: Callable[[Task], Any]) -> None:
    """Run `work` on a thread, moving the task to a terminal state when it ends."""

    def runner() -> None:
        try:
            result = work(task)
            if task.status not in TERMINAL:
                store.update(task.task_id, status=COMPLETED,
                             result=result if isinstance(result, dict) else {},
                             status_message="done")
        except errors.McpError as exc:
            store.update(task.task_id, status=FAILED, error=exc.to_json())
        except Exception as exc:
            store.update(task.task_id, status=FAILED,
                         error=errors.InternalError(str(exc)).to_json())

    threading.Thread(target=runner, daemon=True).start()


def poll_until_done(client, task_id: str, *, timeout: float = 30.0,
                    on_status: Callable[[dict], None] | None = None,
                    input_provider=None) -> dict:
    """Client-side polling loop, honouring the server's suggested cadence.

    Two things people get wrong here. First, ignoring `pollIntervalMs` and
    hammering every 50 ms, which turns one long request into four hundred short
    ones. Second, forgetting that `input_required` is not terminal: a task that
    stops to ask a question sits there until somebody answers it.
    """
    deadline = time.time() + timeout
    interval = 0.25

    while time.time() < deadline:
        task = client.call("tasks/get", {"taskId": task_id}, use_cache=False)
        state = task.get("task", task)
        status = state.get("status")
        if on_status:
            on_status(state)

        if status in TERMINAL:
            if status == FAILED:
                err = state.get("error") or {}
                raise errors.McpError(err.get("code", errors.INTERNAL_ERROR),
                                      err.get("message", "task failed"),
                                      err.get("data"))
            return state.get("result") or {}

        if status == INPUT_REQUIRED and input_provider is not None:
            answers = {}
            for key, req in (state.get("inputRequests") or {}).items():
                if req.get("method") == "elicitation/create":
                    answers[key] = input_provider.elicit(key, req.get("params") or {})
            client.call("tasks/update",
                        {"taskId": task_id, "inputResponses": answers},
                        use_cache=False)

        interval = max(0.05, state.get("pollIntervalMs", 500) / 1000.0)
        time.sleep(interval)

    raise TimeoutError(f"task {task_id} did not finish within {timeout}s")
