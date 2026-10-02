from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .adapters import normalize_file
from .engine import ExperienceEngine
from .models import Event, TaskContext, now, uid
from .safety import redact


def agent_command(agent: str, project: str, *, executable: str = "", extra_args: list[str] | None = None) -> list[str]:
    executable = executable or shutil.which(agent + (".cmd" if os.name == "nt" else "")) or shutil.which(agent) or ""
    if not executable:
        raise ValueError(f"{agent} CLI was not found; install it or pass --executable")
    executable = str(Path(executable).resolve())
    # Python executable fixtures also allow offline verification of the complete wrapper.
    prefix = [sys.executable, executable] if executable.endswith(".py") else [executable]
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        # Resolve npm's Node entrypoint instead of passing task arguments through cmd.
        shim = Path(executable)
        match = re.search(r'"%dp0%[\\/](node_modules[\\/][^"\r\n]+\.(?:c?js))"', shim.read_text(encoding="utf-8-sig"), re.IGNORECASE)
        if not match:
            raise ValueError("Only standard npm CLI shims are supported; pass a native executable with --executable")
        entrypoint = (shim.parent / match[1]).resolve()
        npm_root = (shim.parent / "node_modules").resolve()
        if not entrypoint.is_relative_to(npm_root) or not entrypoint.is_file():
            raise ValueError("CLI shim entrypoint is not a file inside node_modules")
        node = shim.parent / "node.exe"
        node_path = str(node) if node.is_file() else shutil.which("node")
        if not node_path:
            raise ValueError("Node.js is required by this CLI installation")
        prefix = [node_path, str(entrypoint)]
    if agent == "codex":
        args = ["exec", "--json", "-C", project] + list(extra_args or []) + ["-"]
    elif agent == "claude":
        args = ["-p", "--output-format", "stream-json", "--verbose"] + list(extra_args or [])
    else:
        raise ValueError("agent must be codex or claude")
    return prefix + args


def run_agent(engine: ExperienceEngine, context: TaskContext, *, executable: str = "", extra_args: list[str] | None = None, dry_run: bool = False) -> dict:
    project = Path(context.project)
    if not project.is_dir():
        raise ValueError(f"Project directory does not exist: {project}")
    command = agent_command(context.agent, context.project, executable=executable, extra_args=extra_args)
    brief = engine.compile(context, record=not dry_run)
    prompt = brief["text"] + "\n# Current user task\n" + context.task
    if dry_run:
        return {"command": command, "cwd": context.project, "prompt": prompt, "selected_ids": brief["selected_ids"], "dry_run": True}
    db_parent = Path(getattr(engine.repo, "path", ".pel" )).expanduser().resolve().parent
    logs = db_parent / "runs"
    logs.mkdir(parents=True, exist_ok=True)
    run_id = uid("run", context.task, now())
    log_path = logs / f"{run_id}.jsonl"
    # Argument arrays and stdin keep task/source text out of shell command strings.
    process = subprocess.Popen(command, cwd=context.project, stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8", errors="replace", shell=False,
                               env={**os.environ, "PYTHONUTF8": "1"})
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(prompt)
        process.stdin.close()
        with log_path.open("w", encoding="utf-8") as output:
            for line in process.stdout:
                output.write(line)
                output.flush()
                try:
                    row = json.loads(line)
                    text = ""
                    if row.get("type") == "item.completed" and row.get("item", {}).get("type") == "agent_message":
                        text = row["item"].get("text", "")
                    elif row.get("type") == "assistant":
                        text = "\n".join(c.get("text", "") for c in row.get("message", {}).get("content", []) if c.get("type") == "text")
                    if text:
                        print(text, flush=True)
                except (json.JSONDecodeError, AttributeError):
                    print(line, end="", flush=True)
        exit_code = process.wait()
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        process.stdout.close()
    episode = normalize_file(log_path, source=context.agent, project=context.project, domain=context.domain)
    episode.objective = redact(context.task)
    episode.events = [e for e in episode.events if e.text != prompt]
    episode.events.insert(0, Event(uid("evt_task", run_id), "user", "message", redact(context.task), now(), 0))
    episode.digest = uid("digest", episode.digest, context.task, exit_code)
    result = engine.ingest(episode)
    with engine.repo.transaction():
        engine.repo.audit("agent_run", run_id, {"agent": context.agent, "episode_id": episode.id, "compilation_id": brief["id"], "selected_ids": brief["selected_ids"], "exit_code": exit_code})
    return {"run_id": run_id, "exit_code": exit_code, "log": str(log_path), "compilation_id": brief["id"], "ingestion": result}


def claude_hook(engine: ExperienceEngine, payload: dict, *, domain: str = "") -> dict:
    event_name = payload.get("hook_event_name")
    project = payload.get("cwd")
    if not isinstance(project, str) or not project:
        raise ValueError("Hook payload needs cwd")
    if event_name == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {}
        context = TaskContext(prompt, project, domain, "claude", str(payload.get("session_id") or ""))
        brief = engine.compile(context)
        return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": brief["text"]}}
    if event_name in ("Stop", "SessionEnd"):
        transcript = payload.get("transcript_path")
        if not isinstance(transcript, str) or not Path(transcript).expanduser().is_file():
            raise ValueError("Hook transcript_path is not a readable file")
        episode = normalize_file(transcript, source="claude", project=project, domain=domain, allow_partial=True)
        # Recent Claude versions may invoke Stop before flushing their last message.
        # Content-based IDs/dedup make the later SessionEnd import safe.
        final = payload.get("last_assistant_message")
        if event_name == "Stop" and isinstance(final, str) and final.strip() and not any(e.text == final.strip() for e in episode.events):
            final = redact(final.strip())
            episode.events.append(Event(uid("evt", "assistant", "message", final), "assistant", "message", final, now(), 0))
            episode.digest = uid("digest", episode.digest, final)
        engine.ingest(episode)
    return {}


def install_claude_hooks(db_path: str, project: str, domain: str = "") -> dict:
    project_path = Path(project).expanduser().resolve()
    if not project_path.is_dir():
        raise ValueError("Project directory does not exist")
    config_path = project_path / ".claude" / "settings.local.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing_text = config_path.read_text(encoding="utf-8-sig") if config_path.exists() else "{}"
    config = json.loads(existing_text)
    if not isinstance(config, dict):
        raise ValueError("Claude settings must be a JSON object")
    launcher = Path(__file__).resolve().parent / "hook_launcher.py"
    parts = [sys.executable, str(launcher), "--db", str(Path(db_path).expanduser().resolve()), "--domain", domain]
    if any("\n" in p or "\r" in p for p in parts):
        raise ValueError("Hook paths/domain cannot contain newlines")
    if os.name == "nt" and any("%" in p or "!" in p for p in parts):
        raise ValueError("Windows hook paths/domain cannot contain % or !")
    command = subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Existing hooks configuration must be an object")
    added = 0
    for event in ("UserPromptSubmit", "Stop", "SessionEnd"):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"Existing {event} hooks must be an array")
        if any(h.get("command") == command for entry in entries if isinstance(entry, dict) for h in entry.get("hooks", []) if isinstance(h, dict)):
            continue
        entries.append({"hooks": [{"type": "command", "command": command, "timeout": 20}]})
        added += 1
    backup = ""
    if added:
        if config_path.exists():
            backup_path = config_path.with_name("settings.local.pel-backup-" + uid("", now()).strip("_") + ".json")
            backup_path.write_text(existing_text, encoding="utf-8")
            backup = str(backup_path)
        descriptor, temporary = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(config, output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(temporary, config_path)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()
    return {"path": str(config_path), "added": added, "backup": backup}

