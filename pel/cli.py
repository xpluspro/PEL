from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .adapters import normalize_file
from .engine import ExperienceEngine
from .integrations import claude_hook, install_claude_hooks, run_agent
from .models import LIFECYCLES, SCOPES, TaskContext
from .repository import SQLiteRepository


def default_db() -> str:
    return str(Path(os.environ.get("PEL_HOME", str(Path.home() / ".pel"))) / "experience.sqlite3")


def _common(parser):
    parser.add_argument("--db", default=argparse.SUPPRESS, help="SQLite path (default: $PEL_HOME/experience.sqlite3 or ~/.pel/experience.sqlite3)")


def _context(parser):
    parser.add_argument("--task", "-t", required=True)
    parser.add_argument("--project", "-C", default=os.getcwd())
    parser.add_argument("--domain", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--phase", choices=("plan", "act", "verify"), default="act")
    parser.add_argument("--budget", type=int, default=4096, help="Conservative token upper bound, 256..65536")


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="pel", description="Turn prior agent work into scoped, evidence-linked experience.")
    cli.add_argument("--version", action="version", version=__version__)
    cli.add_argument("--db", default=default_db())
    sub = cli.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create the local experience repository")
    _common(init)
    ingest = sub.add_parser("ingest", help="Ingest native JSONL or a normalized Episode")
    _common(ingest)
    ingest.add_argument("path")
    ingest.add_argument("--source", choices=("auto", "codex", "claude", "episode"), default="auto")
    ingest.add_argument("--project")
    ingest.add_argument("--domain", default="")
    watcher = sub.add_parser("watch", help="Passively observe native session files")
    _common(watcher)
    watcher.add_argument("directory")
    watcher.add_argument("--source", choices=("auto", "codex", "claude"), default="auto")
    watcher.add_argument("--project")
    watcher.add_argument("--domain", default="")
    watcher.add_argument("--interval", type=float, default=3)
    watcher.add_argument("--once", action="store_true")
    brief = sub.add_parser("brief", help="Retrieve and compile minimum sufficient context")
    _common(brief)
    _context(brief)
    brief.add_argument("--agent", default="any")
    brief.add_argument("--format", choices=("brief", "rule", "skill", "evaluator"), default="brief")
    brief.add_argument("--json", action="store_true")
    brief.add_argument("--output")
    listing = sub.add_parser("list", help="Inspect stored experiences")
    _common(listing)
    listing.add_argument("--status", choices=LIFECYCLES)
    listing.add_argument("--json", action="store_true")
    inspect = sub.add_parser("inspect", help="Inspect evidence and lifecycle for an experience")
    _common(inspect)
    inspect.add_argument("id")
    feedback = sub.add_parser("feedback", help="Correct, strengthen, weaken or retire experience")
    _common(feedback)
    feedback.add_argument("id")
    feedback.add_argument("--action", required=True, choices=("strengthen", "weaken", "reject", "archive", "promote", "pin", "unpin", "correct", "narrow", "supersede"))
    feedback.add_argument("--reason", required=True)
    feedback.add_argument("--statement", default="")
    feedback.add_argument("--scope", choices=SCOPES, default="")
    feedback.add_argument("--scope-key", default="")
    run = sub.add_parser("run", help="Inject context into an agent, then automatically ingest its result")
    _common(run)
    run.add_argument("agent", choices=("codex", "claude"))
    _context(run)
    run.add_argument("--executable", default="")
    run.add_argument("--dry-run", action="store_true")
    install = sub.add_parser("install-claude", help="Install project-local automatic capture/context hooks")
    _common(install)
    install.add_argument("--project", "-C", default=os.getcwd())
    install.add_argument("--domain", default="")
    hook = sub.add_parser("hook", help="Claude hook handler; receives JSON on stdin")
    _common(hook)
    hook.add_argument("--domain", default="")
    serve = sub.add_parser("serve", help="Open the local lifecycle inspection surface")
    _common(serve)
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true")
    demo = sub.add_parser("demo", help="Run the offline cross-agent experience loop")
    _common(demo)
    export = sub.add_parser("export", help="Export a portable JSON snapshot with provenance")
    _common(export)
    export.add_argument("--output", required=True)
    verify = sub.add_parser("verify", help="Verify the lifecycle audit hash chain")
    _common(verify)
    return cli


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra_args: list[str] = []
    if "--" in argv:
        marker = argv.index("--")
        extra_args, argv = argv[marker + 1:], argv[:marker]
    args = parser().parse_args(argv)
    if extra_args and args.command != "run":
        parser().error("Arguments after -- are only supported by pel run")
    db = str(Path(args.db).expanduser().resolve()) if args.db != ":memory:" else args.db
    try:
        if args.command == "serve":
            from .server import serve
            serve(db, args.port, args.open)
            return 0
        with SQLiteRepository(db) as repository:
            engine = ExperienceEngine(repository)
            if args.command == "init":
                _emit({"database": db, "version": __version__, "storage": "local", "extractor": "offline_rules"})
            elif args.command == "ingest":
                _emit(engine.ingest(normalize_file(args.path, source=args.source, project=args.project, domain=args.domain)))
            elif args.command == "watch":
                from .watcher import watch
                result = watch(engine, args.directory, source=args.source, project=args.project, domain=args.domain, interval=args.interval, once=args.once)
                _emit(result)
                return 1 if result["errors"] else 0
            elif args.command in ("brief", "run"):
                context = TaskContext(args.task, args.project, args.domain, args.agent, args.task_id, args.phase, args.budget)
                if args.command == "run":
                    result = run_agent(engine, context, executable=args.executable, extra_args=extra_args, dry_run=args.dry_run)
                    _emit(result)
                    return result.get("exit_code", 0)
                result = engine.compile(context, args.format)
                output = json.dumps(result, ensure_ascii=False, indent=2) if args.json else result["text"]
                if args.output:
                    target = Path(args.output).expanduser().resolve()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(output + "\n", encoding="utf-8")
                else:
                    print(output)
            elif args.command == "list":
                experiences = [e for e in repository.experiences() if not args.status or e.lifecycle_state == args.status]
                if args.json:
                    _emit([e.to_dict() for e in experiences])
                else:
                    for experience in experiences:
                        print(f"{experience.id}  {experience.type:9}  {experience.lifecycle_state:11}  {experience.confidence:.2f}  {experience.statement}")
                    if not experiences:
                        print("No experiences yet. Use pel demo or pel ingest.")
            elif args.command == "inspect":
                _emit(engine.inspect(args.id))
            elif args.command == "feedback":
                _emit(engine.feedback(args.id, args.action, reason=args.reason, statement=args.statement, scope=args.scope, scope_key=args.scope_key).to_dict())
            elif args.command == "install-claude":
                _emit(install_claude_hooks(db, args.project, args.domain))
            elif args.command == "hook":
                try:
                    payload = json.load(sys.stdin)
                    if not isinstance(payload, dict):
                        raise ValueError("Hook input must be an object")
                    result = claude_hook(engine, payload, domain=args.domain)
                    if result:
                        _emit(result)
                except (OSError, ValueError, KeyError) as exc:
                    # Capture errors must never block normal agent work.
                    print(f"PEL hook: {exc}", file=sys.stderr)
            elif args.command == "demo":
                from .demo import demo
                _emit(demo(engine))
            elif args.command == "export":
                target = Path(args.output).expanduser().resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(repository.export(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                _emit({"output": str(target), "experiences": len(repository.experiences())})
            elif args.command == "verify":
                result = repository.verify_history()
                _emit(result)
                return 0 if result["valid"] else 1
        return 0
    except KeyboardInterrupt:
        print("PEL stopped.", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f"PEL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

