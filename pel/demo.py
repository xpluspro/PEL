"""Synthetic, model-independent acceptance scenario; no live AI performance claims."""

import json
from pathlib import Path

from .adapters import normalize
from .engine import ExperienceEngine
from .models import TaskContext, now


def demo(engine: ExperienceEngine) -> dict:
    root = Path.cwd() / "examples" / "projects"
    at = now()
    codex = [
        {"type": "session_meta", "timestamp": at, "payload": {"id": "demo-codex-kernel-a", "cwd": str(root / "kernel-a")}},
        {"type": "event_msg", "timestamp": at, "payload": {"type": "user_message", "message": "Optimize ragged reduction kernel latency and validate correctness."}},
        {"type": "response_item", "timestamp": at, "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text":
            "Decision: Keep tile=128 for the ragged reduction baseline because the memory budget is limited.\n"
            "Failure: Increasing tile size to 256 regressed ragged reduction latency by 19%.\n"
            "Outcome: tile=128 improved ragged reduction latency by 14.2% on the measured shape.\n"
            "Heuristic: For ragged reduction workloads, compare the tile=128 baseline before increasing tile size.\n"
            "Procedure: Record ragged reduction baseline → validate correctness against an oracle → profile memory traffic → benchmark latency.\n"
            "Evaluator: Validate ragged reduction correctness against the oracle before comparing latency.\n"
            "Artifact: Reuse the ragged reduction benchmark script at benchmarks/ragged.py.\n"
            "State: The ragged reduction baseline is already available for the measured shape."}]}},
        {"type": "response_item", "timestamp": at, "payload": {"type": "function_call", "call_id": "demo-check", "name": "exec_command", "arguments": json.dumps({"cmd": "python -m unittest tests.test_ragged"})}},
        {"type": "response_item", "timestamp": at, "payload": {"type": "function_call_output", "call_id": "demo-check", "output": "Ran 12 tests\nOK\nProcess exited with code 0"}},
    ]
    observations = [engine.ingest(normalize("\n".join(json.dumps(r) for r in codex), source_uri="demo:codex/kernel-a", domain="performance"))]
    for name in ("kernel-b", "kernel-c"):
        claude = [
            {"type": "user", "uuid": name + "-user", "sessionId": "demo-claude-" + name, "cwd": str(root / name), "timestamp": at,
             "message": {"content": "Optimize another ragged reduction kernel and check correctness."}},
            {"type": "assistant", "uuid": name + "-assistant", "sessionId": "demo-claude-" + name, "timestamp": at,
             "message": {"content": [{"type": "text", "text":
                "Heuristic: For ragged reduction workloads, compare the tile=128 baseline before increasing tile size.\n"
                "Procedure: Record ragged reduction baseline → validate correctness against an oracle → profile memory traffic → benchmark latency.\n"
                "Evaluator: Validate ragged reduction correctness against the oracle before comparing latency."}]}},
        ]
        observations.append(engine.ingest(normalize("\n".join(json.dumps(r) for r in claude), source_uri="demo:claude/" + name, domain="performance")))
    for name, domain, objective, lessons in (
        ("orf-research", "research", "Compare ORF assignment reproducibility across batches.",
         "Decision: Use barcode-split reproducibility alongside random-cell cross-validation.\nEvaluator: Verify ORF assignment using per-cell barcode burden, unassigned fraction and batch effects.\nArtifact: The ORF analysis dataset resides at datasets/orf_assignments.csv."),
        ("os-coursework", "coursework", "Investigate false sharing in the memory hierarchy coursework.",
         "Failure: Padding every structure without measuring cache-line contention failed to improve the coursework benchmark.\nProcedure: Establish memory hierarchy baseline → identify cache misses → isolate false sharing → measure again.\nEvaluator: Verify the coursework solution against a brute-force correctness oracle."),
    ):
        document = {"session_id": "demo-" + name, "project": str(root / name), "domain": domain, "objective": objective,
                    "created_at": at, "events": [{"role": "assistant", "text": lessons, "observed_at": at}]}
        observations.append(engine.ingest(normalize(json.dumps(document), source_uri="demo:episode/" + name)))
    task = TaskContext("Optimize ragged reduction latency, reuse the benchmark and validate correctness.", str(root / "kernel-a"), "performance", "claude")
    brief = engine.compile(task)

    def synthetic_planner(context: str) -> dict:
        return {"tile": 128 if "256 regressed" in context else 256,
                "artifact": "benchmarks/ragged.py" if "benchmarks/ragged.py" in context else None,
                "oracle_first": "correctness against the oracle before comparing latency" in context}

    baseline, inherited = synthetic_planner(""), synthetic_planner(brief["text"])
    return {"mode": "synthetic_offline_acceptance", "live_agent_improvement": "not_measured",
            "observations": observations, "overview": engine.overview(), "brief": brief["text"],
            "comparison": {"without_experience": baseline, "with_experience": inherited},
            "acceptance": {"known_trap_avoided": baseline["tile"] != inherited["tile"],
                           "artifact_reused": bool(inherited["artifact"]), "evaluator_adopted": inherited["oracle_first"]},
            "next_task": {"project": task.project, "domain": task.domain, "task": task.task}}

