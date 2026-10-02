"""Normalize native Codex rollouts/exec events, Claude transcripts, or Episodes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import Episode, Event, now, project_key, reconstruct_tasks, timestamp, uid
from .safety import redact


_WRAPPER_TEXT = re.compile(
    r"^\s*(?:<local-command-(?:caveat|stdout)>|<command-(?:name|message|args)>|"
    r"Script (?:failed|completed)|The supplied client history contains this tool call|"
    r"Tool result replayed under run_officejs|"
    r"(?:我先|接下来我会|下一步我会|已完成|真实样本回放|回放后发现|隐私检查通过|I will|Next,? I(?:’|\')ll|Completed|I\'ll ))",
    re.IGNORECASE,
)

MAX_SOURCE_BYTES = 32 * 1024 * 1024


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) for item in content
            if isinstance(item, dict) and item.get("type") in ("text", "input_text", "output_text")
        )
    return ""


def _records(content: str, allow_partial: bool) -> tuple[list[tuple[int, dict]], list[str]]:
    warnings: list[str] = []
    cleaned = content.removeprefix("\ufeff")
    stripped = cleaned.lstrip()
    try:
        document = json.loads(stripped)
        if isinstance(document, dict):
            return [(1, document)], warnings
        if isinstance(document, list) and all(isinstance(r, dict) for r in document):
            return list(enumerate(document, 1)), warnings
    except json.JSONDecodeError:
        pass
    rows: list[tuple[int, dict]] = []
    lines = cleaned.splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            if allow_partial and number == len(lines) and not content.endswith("\n"):
                warnings.append(f"Deferred incomplete last record on line {number}")
                break
            raise ValueError(f"Invalid JSON on line {number}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Record on line {number} must be an object")
        rows.append((number, row))
    return rows, warnings


def detect_source(rows: list[tuple[int, dict]]) -> str:
    for _, row in rows:
        if "events" in row and "objective" in row:
            return "episode"
        if row.get("type") in ("session_meta", "response_item", "event_msg", "thread.started", "item.completed"):
            return "codex"
        if row.get("type") in ("assistant", "user", "result") or "sessionId" in row:
            return "claude"
    raise ValueError("Unrecognized source; select codex, claude or episode explicitly")


def normalize(
    content: str, *, source: str = "auto", source_uri: str = "inline",
    project: str | None = None, domain: str = "", allow_partial: bool = False,
) -> Episode:
    if len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("Session exceeds the 32 MiB import limit")
    rows, warnings = _records(content, allow_partial)
    if not rows:
        raise ValueError("No complete JSON records")
    source = detect_source(rows) if source == "auto" else source
    if source not in ("codex", "claude", "episode"):
        raise ValueError("source must be auto, codex, claude or episode")
    session_id = ""
    inferred_project = ""
    objective = ""
    events: list[Event] = []
    seen: set[tuple[str, str, str]] = set()
    calls: dict[str, dict] = {}
    fallback_time = now()

    def add(role: str, kind: str, text: str, line: int, row: dict, metadata: dict | None = None, event_id: str = "") -> None:
        text = redact(str(text)).strip()
        if not text:
            return
        key = (role, kind, text)
        if key in seen:
            return
        seen.add(key)
        observed = timestamp(row.get("timestamp") or row.get("observed_at") or fallback_time)
        event_metadata = dict(metadata or {})
        if _WRAPPER_TEXT.search(text) and "noise" not in event_metadata:
            event_metadata["noise"] = "harness_wrapper"
        events.append(Event(event_id or uid("evt", role, kind, text), role, kind, text, observed, line, event_metadata))

    if source == "episode":
        if len(rows) != 1:
            raise ValueError("An Episode import must contain one object")
        doc = rows[0][1]
        session_id = str(doc.get("session_id") or doc.get("id") or "")
        inferred_project = str(doc.get("project") or "")
        domain = domain or str(doc.get("domain") or "")
        objective = redact(str(doc.get("objective") or ""))
        fallback_time = timestamp(doc.get("created_at"))
        if not isinstance(doc.get("events"), list):
            raise ValueError("Episode.events must be an array")
        for line, row in enumerate(doc["events"], 1):
            if not isinstance(row, dict):
                raise ValueError("Episode events must be objects")
            role = row.get("role", "assistant")
            if role not in ("user", "assistant", "tool"):
                continue
            metadata = row.get("metadata") or {}
            if not isinstance(metadata, dict):
                raise ValueError("event metadata must be an object")
            # Never accept arbitrary confidence, trust or lifecycle overrides.
            allowed = {k: v for k, v in metadata.items() if k in (
                "experience_type", "statement", "key", "constraints", "tags", "artifact_uri",
                "valid_until", "command", "exit_code", "tool_name", "scope", "task_id",
            )}
            allowed = json.loads(redact(json.dumps(allowed, ensure_ascii=False)))
            add(role, str(row.get("kind", "message")), row.get("text", ""), line, row, allowed, str(row.get("id") or ""))
    else:
        for line, row in rows:
            kind = row.get("type", "")
            payload = row.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            if source == "codex":
                if kind == "session_meta":
                    session_id = str(payload.get("id") or session_id)
                    inferred_project = str(payload.get("cwd") or inferred_project)
                elif kind == "thread.started":
                    session_id = str(row.get("thread_id") or session_id)
                elif kind == "turn_context":
                    inferred_project = str(payload.get("cwd") or inferred_project)
                elif kind == "event_msg":
                    if payload.get("type") == "user_message":
                        add("user", "message", payload.get("message", ""), line, row)
                    elif payload.get("type") == "agent_message":
                        add("assistant", "message", payload.get("message") or payload.get("text", ""), line, row)
                elif kind == "response_item":
                    ptype = payload.get("type")
                    if ptype == "message" and payload.get("role") in ("user", "assistant"):
                        add(payload["role"], "message", _text(payload.get("content")), line, row)
                    elif ptype in ("function_call", "custom_tool_call"):
                        args = payload.get("arguments") or payload.get("input") or "{}"
                        try:
                            args = json.loads(args) if isinstance(args, str) else args
                        except json.JSONDecodeError:
                            args = {}
                        calls[str(payload.get("call_id"))] = {
                            "tool_name": payload.get("name", ""),
                            "command": redact(str(args.get("cmd") or args.get("command") or "")) if isinstance(args, dict) else "",
                        }
                    elif ptype in ("function_call_output", "custom_tool_call_output"):
                        output = payload.get("output", "")
                        metadata = dict(calls.get(str(payload.get("call_id")), {}))
                        if metadata.get("tool_name") in {"exec", "functions.exec", "run_officejs"}:
                            metadata["noise"] = "harness_tool_result"
                        if isinstance(output, str) and output.lstrip().startswith("{"):
                            try:
                                decoded = json.loads(output)
                                if isinstance(decoded, dict) and ("output" in decoded or "exit_code" in decoded):
                                    output = decoded
                            except json.JSONDecodeError:
                                pass
                        if isinstance(output, dict):
                            metadata["exit_code"] = output.get("exit_code")
                            output = output.get("output", json.dumps(output))
                        else:
                            import re
                            match = re.search(r"(?:Process exited with code|Exit code:)\s*(-?\d+)", str(output))
                            if match:
                                metadata["exit_code"] = int(match[1])
                        add("tool", "tool_result", str(output), line, row, metadata, str(payload.get("call_id") or ""))
                elif kind == "item.completed":
                    item = row.get("item") or {}
                    if item.get("type") == "agent_message":
                        add("assistant", "message", item.get("text", ""), line, row, event_id=str(item.get("id", "")))
                    elif item.get("type") == "command_execution":
                        add("tool", "tool_result", item.get("aggregated_output", "") or f"Command exited with code {item.get('exit_code')}", line, row,
                            {"command": redact(str(item.get("command", ""))), "exit_code": item.get("exit_code"), "tool_name": "shell"}, str(item.get("id", "")))
            else:
                # Skip sidechains; a subagent's task must not replace the user's objective.
                if row.get("isSidechain") or row.get("parent_tool_use_id"):
                    continue
                session_id = str(row.get("sessionId") or row.get("session_id") or session_id)
                inferred_project = str(row.get("cwd") or inferred_project)
                message = row.get("message") or {}
                if not isinstance(message, dict):
                    continue
                if kind in ("user", "assistant"):
                    add(kind, "message", _text(message.get("content")), line, row, event_id=str(row.get("uuid") or message.get("id") or ""))
                    content_blocks = message.get("content")
                    for block in content_blocks if isinstance(content_blocks, list) else []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            tool_input = block.get("input") or {}
                            calls[str(block.get("id"))] = {"tool_name": block.get("name", ""), "command": redact(str(tool_input.get("command", "")))}
                        elif block.get("type") == "tool_result":
                            metadata = dict(calls.get(str(block.get("tool_use_id")), {}))
                            metadata["is_error"] = bool(block.get("is_error"))
                            add("tool", "tool_result", _text(block.get("content")), line, row, metadata, str(block.get("tool_use_id") or ""))
                elif kind == "result" and row.get("result"):
                    add("assistant", "message", row["result"], line, row)

    selected_project = project or inferred_project
    if not selected_project:
        raise ValueError("Source has no project directory; pass --project")
    selected_project = project_key(selected_project)
    if not objective:
        objective = next((e.text for e in events if e.role == "user" and e.kind == "message" and not e.metadata.get("noise")), "Imported agent session")
    # A file without session metadata retains its identity across appends and retries.
    session_id = session_id or uid("session", source_uri, selected_project)
    episode_id = uid("ep", source, session_id, selected_project)
    created = min((e.observed_at for e in events), default=fallback_time)
    latest = max((e.observed_at for e in events), default=fallback_time)
    digest = uid("digest", [(e.id, e.role, e.kind, e.text, e.metadata) for e in events], objective, domain)
    tasks = reconstruct_tasks(events, objective)
    return Episode(episode_id, source, session_id, source_uri, selected_project, domain, objective, events, created, latest, digest, warnings, tasks)


def normalize_file(path: str | Path, **kwargs: Any) -> Episode:
    path = Path(path).expanduser().resolve()
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Session exceeds the 32 MiB import limit")
    return normalize(path.read_text(encoding="utf-8-sig"), source_uri=str(path), **kwargs)


def normalize_stream(stream: Any, **kwargs: Any) -> Episode:
    """Normalize a text stream with bounded chunk reads.

    JSONL sessions are read incrementally so callers can feed a growing log or
    a pipe without first materializing it themselves.  ``normalize`` remains
    the canonical parser for a single document and partial trailing records.
    """
    chunks: list[str] = []
    total = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), ""):
        total += len(chunk.encode("utf-8"))
        if total > MAX_SOURCE_BYTES:
            raise ValueError("Session exceeds the 32 MiB import limit")
        chunks.append(chunk)
    return normalize("".join(chunks), **kwargs)
