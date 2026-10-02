from __future__ import annotations

import sys
import time
from pathlib import Path

from .adapters import normalize_file
from .engine import ExperienceEngine


def watch(engine: ExperienceEngine, directory: str, *, source: str = "auto", project: str | None = None,
          domain: str = "", interval: float = 3, once: bool = False) -> dict:
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Session directory does not exist: {root}")
    if interval < 0.2:
        raise ValueError("Watch interval must be at least 0.2 seconds")
    fingerprints: dict[Path, tuple[int, int]] = {}
    imported = unchanged = errors = 0
    while True:
        for path in sorted(root.rglob("*.jsonl")):
            try:
                stat = path.stat()
                fingerprint = (stat.st_mtime_ns, stat.st_size)
                if fingerprints.get(path) == fingerprint:
                    continue
                # Retry incomplete files on the next scan even if their size is unchanged.
                result = engine.ingest(normalize_file(path, source=source, project=project, domain=domain, allow_partial=True))
                if not result["warnings"]:
                    fingerprints[path] = fingerprint
                if result["unchanged"]:
                    unchanged += 1
                else:
                    imported += 1
                    print(f"Observed {path.name}: {result['created']} new, {result['merged']} merged", file=sys.stderr, flush=True)
            except (OSError, ValueError) as exc:
                errors += 1
                print(f"Deferred {path.name}: {exc}", file=sys.stderr, flush=True)
        if once:
            return {"imported": imported, "unchanged": unchanged, "errors": errors}
        time.sleep(interval)

