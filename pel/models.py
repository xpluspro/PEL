from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TYPES = ("state", "decision", "outcome", "failure", "heuristic", "procedure", "evaluator", "artifact")
SCOPES = ("task", "project", "domain", "personal")
LIFECYCLES = ("candidate", "active", "disputed", "superseded", "archived", "rejected", "quarantined")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def timestamp(value: Any = None) -> str:
    if value is None:
        return now()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid timestamp: {value!r}") from exc


def uid(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return prefix + "_" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def project_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("project must be a non-empty directory path")
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def normalized(value: str) -> str:
    return re.sub(r"[\s`*。.!！]+", " ", value.casefold()).strip()


# Conservative upper bound: one token per UTF-8 byte. No model-specific tokenizer
# dependency, and non-ASCII content cannot silently overflow the context budget.
def token_cost(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass
class Event:
    id: str
    role: str
    kind: str
    text: str
    observed_at: str
    line: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Episode:
    id: str
    source: str
    session_id: str
    source_uri: str
    project: str
    domain: str
    objective: str
    events: list[Event]
    created_at: str
    last_observed_at: str
    digest: str
    warnings: list[str] = field(default_factory=list)
    # Reconstructed work graph.  Older databases may omit this field; callers
    # can still use the normalized event stream.
    tasks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    type: str
    statement: str
    event: Event
    evidence_kind: str
    confidence: float
    scope: str = "project"
    scope_key: str = ""
    semantic_key: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    quarantined: bool = False
    # A conclusion is usually supported by a complete attempt, rather than a
    # single line.  ``event`` remains for source compatibility.
    events: list[Event] = field(default_factory=list)
    supporting_events: list[Event] = field(default_factory=list)
    refuting_events: list[Event] = field(default_factory=list)
    conclusion: str = ""
    applicability: list[str] = field(default_factory=list)
    recommended_action: str = ""
    verification_method: str = ""
    uncertainty: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.events:
            self.events = [self.event]
        if not self.supporting_events:
            self.supporting_events = list(self.events)
        if not self.conclusion:
            self.conclusion = self.statement


@dataclass
class Experience:
    id: str
    type: str
    statement: str
    project: str
    domain: str
    scope: str
    scope_key: str
    confidence: float
    lifecycle_state: str
    created_at: str
    last_observed_at: str
    semantic_key: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    related_tasks: list[str] = field(default_factory=list)
    related_artifacts: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    contradicted_by: list[str] = field(default_factory=list)
    generalizes: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    pinned: bool = False
    applicability: list[str] = field(default_factory=list)
    recommended_action: str = ""
    verification_method: str = ""
    uncertainty: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskContext:
    task: str
    project: str
    domain: str = ""
    agent: str = "any"
    task_id: str = ""
    phase: str = "act"
    budget_tokens: int = 4096

    def __post_init__(self) -> None:
        if not isinstance(self.task, str) or not self.task.strip():
            raise ValueError("task must be non-empty")
        for name in ("domain", "agent", "task_id", "phase"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        if not isinstance(self.budget_tokens, int) or isinstance(self.budget_tokens, bool):
            raise ValueError("budget_tokens must be an integer")
        if not 256 <= self.budget_tokens <= 65536:
            raise ValueError("budget_tokens must be between 256 and 65536")
        if self.phase not in ("plan", "act", "verify"):
            raise ValueError("phase must be plan, act or verify")
        self.project = project_key(self.project)


def reconstruct_tasks(events: list[Event], objective: str = "") -> list[dict[str, Any]]:
    """Build a small, deterministic work graph from an event stream.

    User messages start a task or a new attempt. Tool results and assistant
    messages remain attached until the next user goal. Explicit task IDs are
    respected, so the same task can be joined across imported sessions later.
    This is deliberately source agnostic and keeps event IDs as evidence.
    """
    tasks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    attempt: dict[str, Any] | None = None
    ordinal = 0
    for event in events:
        explicit = str(event.metadata.get("task_id") or "")
        starts = event.role == "user" and event.kind in ("message", "prompt")
        if current is None or (starts and explicit and explicit != current["task_id"]):
            if current is not None:
                tasks.append(current)
            ordinal += 1
            task_id = explicit or uid("task", objective or event.text, ordinal)
            current = {"task_id": task_id, "objective": event.text if starts else objective,
                       "event_ids": [], "attempts": [], "sessions": []}
            attempt = None
        if current is None:
            continue
        current["event_ids"].append(event.id)
        current["sessions"].append(str(event.metadata.get("session_id") or "")) if event.metadata.get("session_id") else None
        if starts or attempt is None:
            attempt = {"attempt_id": uid("attempt", current["task_id"], len(current["attempts"])),
                       "event_ids": [], "start_line": event.line, "end_line": event.line,
                       "corrections": [], "results": [], "unresolved": []}
            current["attempts"].append(attempt)
        attempt["event_ids"].append(event.id)
        attempt["end_line"] = event.line
        if event.role == "user" and len(attempt["event_ids"]) > 1:
            attempt["corrections"].append(event.text)
        if event.role == "tool" and event.metadata.get("noise") not in {"harness_tool_result", "harness_wrapper"}:
            exit_code = event.metadata.get("exit_code")
            if exit_code not in (None, 0):
                attempt["results"].append({"status": "failed", "event_id": event.id})
            elif exit_code == 0:
                attempt["results"].append({"status": "passed", "event_id": event.id})
        if re.search(r"(?:未解决|unknown|uncertain|不确定|待确认)", event.text, re.I):
            attempt["unresolved"].append(event.text[:500])
    if current is not None:
        tasks.append(current)
    return tasks
