"""Replaceable persistence boundary; the semantic engine contains no SQL."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ContextManager, Protocol

from .models import Episode, Experience, now, uid


class Repository(Protocol):
    def transaction(self) -> ContextManager: ...
    def get_episode(self, episode_id: str) -> dict | None: ...
    def save_episode(self, episode: Episode) -> None: ...
    def episodes(self) -> list[dict]: ...
    def get(self, experience_id: str) -> Experience: ...
    def experiences(self) -> list[Experience]: ...
    def save(self, experience: Experience) -> None: ...
    def save_evidence(self, evidence: dict) -> bool: ...
    def evidence(self, evidence_id: str) -> dict: ...
    def audit(self, action: str, subject_id: str, details: dict) -> None: ...
    def history(self, subject_id: str) -> list[dict]: ...
    def save_compilation(self, record: dict) -> None: ...


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _experience(value: dict) -> Experience:
    # Databases created before the task reconstruction fields are still
    # readable and are upgraded lazily on write.
    value.setdefault("applicability", [])
    value.setdefault("recommended_action", "")
    value.setdefault("verification_method", "")
    value.setdefault("uncertainty", [])
    return Experience(**value)


class SQLiteRepository:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).expanduser().resolve()) if str(path) != ":memory:" else ":memory:"
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=20000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.close()
            raise ValueError(f"Unsupported database version {version}")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS episodes (
                id TEXT PRIMARY KEY, body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiences (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, scope_key TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS experience_scope ON experiences(scope_key, lifecycle_state);
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY, episode_id TEXT, body TEXT NOT NULL,
                FOREIGN KEY(episode_id) REFERENCES episodes(id)
            );
            CREATE TABLE IF NOT EXISTS audit (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                subject_id TEXT NOT NULL, at TEXT NOT NULL, details TEXT NOT NULL,
                previous_hash TEXT NOT NULL, hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audit_subject ON audit(subject_id);
            CREATE TABLE IF NOT EXISTS compilations (
                id TEXT PRIMARY KEY, body TEXT NOT NULL
            );
            PRAGMA user_version=1;
        """)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def get_episode(self, episode_id: str) -> dict | None:
        row = self.connection.execute("SELECT body FROM episodes WHERE id=?", (episode_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_episode(self, episode: Episode) -> None:
        self.connection.execute("INSERT INTO episodes VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (episode.id, _json(episode.to_dict())))

    def episodes(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.connection.execute("SELECT body FROM episodes ORDER BY rowid DESC")]

    def get(self, experience_id: str) -> Experience:
        row = self.connection.execute("SELECT body FROM experiences WHERE id=?", (experience_id,)).fetchone()
        if not row:
            raise KeyError(f"Experience not found: {experience_id}")
        return _experience(json.loads(row[0]))

    def experiences(self) -> list[Experience]:
        return [_experience(json.loads(row[0])) for row in self.connection.execute("SELECT body FROM experiences ORDER BY rowid DESC")]

    def save(self, experience: Experience) -> None:
        self.connection.execute(
            "INSERT INTO experiences VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET type=excluded.type, scope_key=excluded.scope_key, lifecycle_state=excluded.lifecycle_state, body=excluded.body",
            (experience.id, experience.type, experience.scope_key, experience.lifecycle_state, _json(experience.to_dict())),
        )

    def save_evidence(self, evidence: dict) -> bool:
        cursor = self.connection.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?)", (evidence["id"], evidence.get("episode_id"), _json(evidence)))
        return cursor.rowcount == 1

    def evidence(self, evidence_id: str) -> dict:
        row = self.connection.execute("SELECT body FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if not row:
            raise KeyError(f"Evidence not found: {evidence_id}")
        return json.loads(row[0])

    def audit(self, action: str, subject_id: str, details: dict) -> None:
        last = self.connection.execute("SELECT hash FROM audit ORDER BY sequence DESC LIMIT 1").fetchone()
        previous_hash = last[0] if last else "genesis"
        at = now()
        digest = uid("audit", previous_hash, action, subject_id, at, details)
        self.connection.execute("INSERT INTO audit(action,subject_id,at,details,previous_hash,hash) VALUES (?,?,?,?,?,?)", (action, subject_id, at, _json(details), previous_hash, digest))

    def history(self, subject_id: str) -> list[dict]:
        return [{**dict(row), "details": json.loads(row["details"])} for row in self.connection.execute("SELECT * FROM audit WHERE subject_id=? ORDER BY sequence", (subject_id,))]

    def verify_history(self) -> dict:
        previous = "genesis"
        count = 0
        for row in self.connection.execute("SELECT * FROM audit ORDER BY sequence"):
            expected = uid("audit", previous, row["action"], row["subject_id"], row["at"], json.loads(row["details"]))
            if row["previous_hash"] != previous or row["hash"] != expected:
                return {"valid": False, "checked": count, "broken_at": row["sequence"]}
            previous = row["hash"]
            count += 1
        return {"valid": True, "checked": count, "head": previous}

    def save_compilation(self, record: dict) -> None:
        self.connection.execute("INSERT INTO compilations VALUES (?,?)", (record["id"], _json(record)))

    def compilations(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.connection.execute("SELECT body FROM compilations ORDER BY rowid DESC LIMIT 200")]

    def export(self) -> dict:
        return {
            "schema_version": 1, "exported_at": now(),
            "episodes": self.episodes(), "experiences": [e.to_dict() for e in self.experiences()],
            "evidence": [json.loads(row[0]) for row in self.connection.execute("SELECT body FROM evidence")],
            "audit": [{**dict(row), "details": json.loads(row["details"])} for row in self.connection.execute("SELECT * FROM audit ORDER BY sequence")],
            "compilations": self.compilations(),
        }
