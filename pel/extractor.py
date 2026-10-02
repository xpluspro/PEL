"""Conservative, offline extraction with a replaceable Extractor protocol.

Repeated agent assertions are evidence of repetition, not evidence of success.
"""

from __future__ import annotations

import re
from typing import Protocol

from .models import Candidate, Episode, TYPES, Event
from .safety import clean_statement, suspicious


class Extractor(Protocol):
    def extract(self, episode: Episode) -> list[Candidate]: ...


_LABEL = re.compile(
    r"^\s*(?:[-*]\s*|\d+[.)]\s*)?(?:\*\*)?"
    r"(state|decision|outcome|failure|trap|heuristic|procedure|evaluator|artifact(?: reference)?"
    r"|状态|决策|结果|失败|陷阱|经验|启发式|流程|步骤|评估器|验证|产物)"
    r"(?:\*\*)?\s*[:：]\s*(.+)$", re.IGNORECASE,
)
_ALIASES = {
    "trap": "failure", "artifact reference": "artifact", "状态": "state", "决策": "decision",
    "结果": "outcome", "失败": "failure", "陷阱": "failure", "经验": "heuristic", "启发式": "heuristic",
    "流程": "procedure", "步骤": "procedure", "评估器": "evaluator", "验证": "evaluator", "产物": "artifact",
}
_PATTERNS = (
    ("failure", r"\b(?:failed|regressed|caused (?:flaky|incorrect)|does not work|didn't work|avoid repeating|do not repeat)\b|(?:导致.*(?:失败|回退|错误)|不要再|失败原因|性能回退)"),
    ("decision", r"\b(?:decided|chose|selected|use .+ instead of|keep .+ because)\b|(?:决定|选用|选择.*因为|采用.*而非)"),
    ("evaluator", r"\b(?:validate|verify|check|test)\b.+\b(?:before|against|using|with|correctness|oracle|reproducibility)\b|(?:验证正确性|评估标准|验收标准|先.*验证.*再)"),
    ("procedure", r"(?:→|->).*(?:→|->)|\b(?:workflow|procedure):|(?:流程|步骤)[:：]"),
    ("outcome", r"\b(?:improved|reduced|increased|achieved|passed)\b.+(?:\d|latency|tests|benchmark)|(?:提升|降低|通过).*(?:\d|测试|基准)"),
    ("artifact", r"\b(?:script|dataset|implementation|artifact|configuration)\b.+(?:/|\\|\.py|\.csv|\.json)|(?:脚本|数据集|产物).*(?:/|\\)"),
    ("heuristic", r"\b(?:for .+, (?:prefer|test|consider)|rule of thumb|usually|under .+,)\b|(?:对于.*(?:优先|尝试|考虑)|通常.*|在.*条件下)"),
    ("state", r"\b(?:currently|already|is installed|is available|has been (?:run|completed))\b|(?:目前|已完成|已安装|当前状态)"),
)
_COMPILED = [(kind, re.compile(pattern, re.IGNORECASE)) for kind, pattern in _PATTERNS]
_CHECK_COMMAND = re.compile(r"(?:pytest|unittest|(?:npm|pnpm|yarn|cargo|go)\s+test|ctest|benchmark|bench[._/]|correctness|验证|基准)", re.IGNORECASE)
_TEST_OUTPUT = re.compile(r"(?:\d+\s+passed|\bOK\b|\bPASS\b|tests? passed|correctness.*passed)", re.IGNORECASE)
_NARRATIVE = re.compile(r"(?:我会|我先|接下来|下一步|已完成|回放|测试结果|隐私|包装文本|真实样本|I will|I’ll|Next,? I|I(?:'|’)ll)", re.IGNORECASE)


def _compact_event_text(event: Event, pattern: re.Pattern[str], limit: int = 350) -> str:
    """Keep a short, evidence-shaped summary while retaining full source events."""
    lines = [line.strip() for line in event.text.splitlines() if line.strip()]
    matching = [line for line in lines if pattern.search(line)]
    chosen = matching[:3] or lines[:3] or [event.text.strip()]
    text = " ".join(chosen)
    return text[:limit]


def _is_narrative(event: Event) -> bool:
    return event.role == "assistant" and _NARRATIVE.search(event.text) is not None


class RuleExtractor:
    def extract(self, episode: Episode) -> list[Candidate]:
        results: list[Candidate] = []
        for event in episode.events:
            metadata = event.metadata
            if metadata.get("noise") == "harness_wrapper":
                continue
            if event.role == "tool":
                command = str(metadata.get("command", ""))
                exit_code = metadata.get("exit_code")
                success = exit_code == 0 and not isinstance(exit_code, bool)
                failed = (isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0) or metadata.get("is_error") is True
                # The command is a reference only. PEL never runs it as an evaluator.
                if _CHECK_COMMAND.search(command):
                    results.append(Candidate("evaluator", f"Validation command used: {command}", event, "test" if "bench" not in command.lower() else "benchmark", 0.8,
                                             attributes={"command": command, "execution": "reference_only"}, quarantined=suspicious(command + event.text)))
                if failed and command:
                    summary = next((s.strip() for s in event.text.splitlines() if re.search(r"error|failed|失败|错误", s, re.IGNORECASE)), event.text.strip().splitlines()[0])
                    statement = f"Command failed: {command}. Result: {summary[:500]}"
                    results.append(Candidate("failure", statement, event, "runtime_result", 0.9, quarantined=suspicious(command + event.text)))
                elif success and command and (_CHECK_COMMAND.search(command) or _TEST_OUTPUT.search(event.text)):
                    summary = next((s.strip() for s in event.text.splitlines() if _TEST_OUTPUT.search(s)), event.text.strip().splitlines()[-1])
                    results.append(Candidate("outcome", f"Command succeeded: {command}. Result: {summary[:500]}", event, "test" if _TEST_OUTPUT.search(event.text) else "runtime_result", 0.9,
                                             quarantined=suspicious(command + event.text)))
                continue
            if event.role not in ("user", "assistant"):
                continue
            explicit_type = metadata.get("experience_type")
            if explicit_type and explicit_type not in TYPES:
                raise ValueError(f"Unknown experience type: {explicit_type}")
            segments = [metadata.get("statement") or event.text] if explicit_type else event.text.splitlines()
            for segment in segments:
                if not isinstance(segment, str):
                    raise ValueError("statement must be a string")
                segment = segment.strip().strip("-* ")
                if not segment or len(segment) > 2000:
                    continue
                if metadata.get("noise") == "harness_wrapper" or re.match(r"^(?:我先|接下来我会|下一步我会|已完成|真实样本回放|回放后发现|隐私检查通过|I will|Next,? I(?:’|\')ll|Completed|I\'ll )", segment, re.I):
                    continue
                if event.role == "assistant" and not explicit_type and _NARRATIVE.search(segment):
                    continue
                # Skip code/role delimiters and low-information acknowledgements.
                if segment.startswith("```") or len(segment) < 12:
                    continue
                match = _LABEL.match(segment)
                if event.role == "user" and not explicit_type and not match and segment == episode.objective:
                    continue
                if segment.startswith("{") and ('"lesson"' in segment or '"evidence"' in segment):
                    continue
                kind = str(explicit_type or "")
                statement = segment
                if match and not kind:
                    label = match[1].casefold()
                    kind = _ALIASES.get(label, label)
                    statement = match[2]
                elif not kind:
                    kind = next((t for t, regex in _COMPILED if regex.search(segment)), "")
                if not kind:
                    continue
                statement = clean_statement(statement)
                attrs = {k: v for k, v in metadata.items() if k in ("constraints", "tags", "artifact_uri", "valid_until")}
                if kind == "artifact" and not attrs.get("artifact_uri"):
                    pointer = re.search(r"(?:[A-Za-z]:[\\/]|/|[\w.-]+[\\/])[^\s`<>]+", statement)
                    if pointer:
                        attrs["artifact_uri"] = pointer[0].rstrip(".,;。；")
                if "constraints" in attrs and not isinstance(attrs["constraints"], (str, list)):
                    raise ValueError("constraints must be a string or array")
                if "tags" in attrs and (not isinstance(attrs["tags"], list) or not all(isinstance(t, str) for t in attrs["tags"])):
                    raise ValueError("tags must be an array of strings")
                if "valid_until" in attrs:
                    from .models import timestamp
                    attrs["valid_until"] = timestamp(attrs["valid_until"])
                # Source role identifies provenance, never policy authority.
                evidence_kind = "human_correction" if event.role == "user" else "agent_inference"
                confidence = 0.8 if event.role == "user" else (0.4 if kind == "heuristic" else 0.55)
                scope = metadata.get("scope", "project")
                if scope not in ("project", "task"):
                    raise ValueError("Imported experience can only start at task or project scope")
                scope_key = str(metadata.get("task_id") or "") if scope == "task" else episode.project
                if scope == "task" and not scope_key:
                    raise ValueError("Task-scoped experience needs metadata.task_id")
                attrs["authority"] = "source_data"
                results.append(Candidate(kind, statement, event, evidence_kind, confidence, scope, scope_key,
                                         str(metadata.get("key") or ""), attrs, suspicious(event.text)))
        # Reconstruct complete attempts before semantic extraction.  A useful
        # lesson often spans a failed command, a user correction and a later
        # successful verification; none of those lines is sufficient alone.
        by_id = {event.id: event for event in episode.events}
        for task in episode.tasks:
            # Corrections open a new attempt, but the reusable conclusion spans the whole task.
            events = [by_id[eid] for eid in task.get("event_ids", []) if eid in by_id]
            attempt = task.get("attempts", [{}])[-1]
            if len(events) < 2:
                continue
            text = "\n".join(e.text for e in events)
            usable = [e for e in events if not _is_narrative(e) and e.metadata.get("noise") != "harness_wrapper"]
            failures = [e for e in usable if (e.role == "tool" and (e.metadata.get("exit_code") not in (None, 0) or e.metadata.get("is_error")))
                        or re.search(r"(?:failed|regressed|does not work|失败|回退|错误)", e.text, re.I)]
            successes = [e for e in usable if (e.role == "tool" and e.metadata.get("exit_code") == 0)
                         or re.search(r"(?:passed|succeeded|improved|通过|成功|提升)", e.text, re.I)]
            corrections = [e for e in usable if e.role == "user" and e is not events[0]]
            if not failures or not successes:
                continue
            failed_text = _compact_event_text(failures[-1], re.compile(r"(?:failed|error|regress|reject|失败|错误|回退)", re.I))
            success_text = _compact_event_text(successes[-1], re.compile(r"(?:passed|success|improv|completed|通过|成功|提升)", re.I))
            correction = _compact_event_text(corrections[-1], re.compile(r".*"), 250) if corrections else ""
            statement = f"Attempt evidence: failed approach: {failed_text}; corrected by: {correction or 'a subsequent change'}; verified result: {success_text}."
            support = [*failures, *successes, *corrections]
            attrs = {"task_id": task.get("task_id", ""), "attempt_id": attempt.get("attempt_id", ""),
                         "constraints": [task.get("objective", episode.objective)] if task.get("objective") else [],
                         "extraction": "complete_attempt"}
            results.append(Candidate(
                "heuristic", statement, successes[-1], "runtime_result", 0.55,
                    scope="project", scope_key=episode.project, semantic_key=task.get("task_id", ""),
                    attributes=attrs, quarantined=any(suspicious(e.text) for e in events),
                    events=events, supporting_events=support, refuting_events=failures,
                    conclusion=statement, applicability=[task.get("objective", episode.objective)],
                    recommended_action=correction or "Prefer the corrected approach and reproduce the verification.",
                    verification_method=next((e.metadata.get("command", "") for e in successes if e.metadata.get("command")), "Repeat the recorded verification under the same conditions."),
                uncertainty=[u for a in task.get("attempts", []) for u in a.get("unresolved", [])],
            ))
        # Bounded selection prefers failures/evaluators/decisions over incidental state.
        order = {t: i for i, t in enumerate(("failure", "evaluator", "decision", "outcome", "procedure", "heuristic", "artifact", "state"))}
        results.sort(key=lambda c: order[c.type])
        # Keep the complete-attempt conclusions even when line-level extraction
        # is noisy, while retaining the bounded import contract.
        return results[:200]
