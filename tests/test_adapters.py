import json
import unittest

from pel.adapters import normalize
from pel.extractor import RuleExtractor


class AdapterTests(unittest.TestCase):
    def normalize(self, rows, **kwargs):
        return normalize("\n".join(json.dumps(r) for r in rows), project=".", **kwargs)

    def test_codex_rollout_ignores_system_reasoning_and_mirror_duplicates(self):
        rows = [
            {"type": "session_meta", "payload": {"id": "a", "cwd": "."}},
            {"type": "response_item", "payload": {"type": "message", "role": "system", "content": [{"type": "input_text", "text": "Decision: Secret system instructions."}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "Hidden reasoning"}]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Optimize the reduction kernel."}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Optimize the reduction kernel."}]}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Decision: Use tile=128 because local memory is limited."}},
        ]
        episode = self.normalize(rows)
        self.assertEqual(episode.source, "codex")
        self.assertEqual(len(episode.events), 2)
        self.assertNotIn("Hidden reasoning", str(episode.to_dict()))
        self.assertNotIn("Secret system", str(episode.to_dict()))

    def test_codex_exec_results_preserve_real_exit_code(self):
        episode = self.normalize([
            {"type": "thread.started", "thread_id": "a"},
            {"type": "item.completed", "item": {"id": "test", "type": "command_execution", "command": "python -m pytest tests/reduction.py", "exit_code": 1, "aggregated_output": "FAILED test_empty_rows"}},
            {"type": "item.completed", "item": {"id": "msg", "type": "agent_message", "text": "Outcome: All 20 tests passed."}},
        ])
        results = RuleExtractor().extract(episode)
        failures = [c for c in results if c.type == "failure"]
        self.assertEqual(failures[0].evidence_kind, "runtime_result")
        reported = next(c for c in results if c.statement == "All 20 tests passed.")
        self.assertEqual(reported.evidence_kind, "agent_inference")
        self.assertLess(reported.confidence, failures[0].confidence)

    def test_claude_transcript_tools_and_sidechains(self):
        episode = self.normalize([
            {"type": "user", "sessionId": "a", "uuid": "u", "cwd": ".", "message": {"content": "Optimize reduction correctness."}},
            {"type": "user", "sessionId": "a", "isSidechain": True, "message": {"content": "Unrelated subagent objective"}},
            {"type": "assistant", "uuid": "a", "message": {"content": [{"type": "tool_use", "id": "tool1", "name": "Bash", "input": {"command": "python -m pytest"}}, {"type": "text", "text": "Evaluator: Verify reduction correctness against the oracle."}]}},
            {"type": "user", "uuid": "t", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool1", "is_error": True, "content": "FAILED test_reduction"}]}},
        ])
        self.assertEqual(episode.source, "claude")
        self.assertEqual(len(episode.events), 3)
        self.assertTrue(any(c.type == "failure" for c in RuleExtractor().extract(episode)))
        self.assertNotIn("Unrelated subagent", str(episode.to_dict()))

    def test_partial_last_record_is_deferred_only_when_requested(self):
        content = '{"type":"thread.started","thread_id":"a"}\n{"type":"item.completed"'
        episode = normalize(content, project=".", allow_partial=True)
        self.assertTrue(episode.warnings)
        with self.assertRaises(ValueError):
            normalize(content, project=".")
        with self.assertRaises(ValueError):
            normalize(content + '\n', project=".", allow_partial=True)

    def test_invalid_middle_record_and_non_object_are_rejected(self):
        with self.assertRaises(ValueError):
            normalize('{"type":"thread.started"}\nnot json\n{}', project=".", allow_partial=True)
        with self.assertRaises(ValueError):
            normalize('42\n', project=".")

    def test_chinese_labels_and_patterns(self):
        episode = normalize(json.dumps({"project": ".", "objective": "优化工作负载", "events": [{"role": "assistant", "text": "决策：采用基准实现，因为正确性已经验证。\n失败：增加线程数导致性能回退。\n评估器：先验证正确性，再比较延迟。\n产物：基准脚本位于 scripts/bench.py。"}]}))
        self.assertEqual({c.type for c in RuleExtractor().extract(episode)}, {"decision", "failure", "evaluator", "artifact"})

    def test_empty_and_unrecognized_inputs_have_clear_errors(self):
        for content in ("", "{}", "[]"):
            with self.assertRaises(ValueError):
                normalize(content)

    def test_input_bom_and_timezone_normalization(self):
        episode = normalize('\ufeff'+json.dumps({"project": ".", "objective": "Test workload", "created_at": "2026-10-02T08:00:00+08:00", "events": [{"role":"assistant", "text":"Decision: Keep the reference implementation.", "observed_at":"2026-10-02T08:00:00+08:00"}]}))
        self.assertTrue(episode.created_at.startswith("2026-10-02T00:00:00"))

    def test_provenance_preserves_jsonl_line_numbers_after_blank_lines(self):
        content = '\n\n'+json.dumps({"type":"thread.started","thread_id":"lines"})+'\n'+json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"Decision: Keep the reference implementation."}})+'\n'
        episode = normalize(content,project=".")
        self.assertEqual(episode.events[0].line,4)

    def test_codex_serialized_tool_result_is_decoded(self):
        episode = self.normalize([
            {"type":"session_meta","payload":{"id":"serialized","cwd":"."}},
            {"type":"response_item","payload":{"type":"function_call","call_id":"c1","name":"exec_command","arguments":json.dumps({"cmd":"python -m pytest"})}},
            {"type":"response_item","payload":{"type":"function_call_output","call_id":"c1","output":json.dumps({"output":"12 passed","exit_code":0})}},
        ])
        result = next(c for c in RuleExtractor().extract(episode) if c.type=="outcome")
        self.assertEqual(result.evidence_kind,"test")

