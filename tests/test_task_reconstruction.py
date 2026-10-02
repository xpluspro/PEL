import io
import json
import tempfile
import unittest

from pel.adapters import normalize, normalize_stream
from pel.engine import ExperienceEngine
from pel.repository import SQLiteRepository


class TaskReconstructionTests(unittest.TestCase):
    def test_complete_attempt_keeps_support_and_refutation_evidence(self):
        document = {"session_id": "real-like", "project": ".", "objective": "Improve benchmark",
                    "events": [
                        {"role": "user", "text": "Improve benchmark", "metadata": {"task_id": "bench-1"}},
                        {"role": "assistant", "text": "Try A"},
                        {"role": "tool", "text": "regression", "metadata": {"command": "bench.py", "exit_code": 1}},
                        {"role": "user", "text": "Use B and verify", "metadata": {"task_id": "bench-1"}},
                        {"role": "assistant", "text": "B improves latency"},
                        {"role": "tool", "text": "12 passed", "metadata": {"command": "pytest", "exit_code": 0}},
                    ]}
        episode = normalize(json.dumps(document))
        self.assertEqual(episode.tasks[0]["task_id"], "bench-1")
        self.assertEqual(len(episode.tasks[0]["attempts"]), 2)
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteRepository(directory + "/experience.sqlite3")
            engine = ExperienceEngine(repository)
            engine.ingest(episode)
            lesson = next(item for item in repository.experiences() if item.type == "heuristic")
            self.assertGreaterEqual(len(lesson.evidence_ids), 3)
            self.assertTrue(lesson.attributes["refuting_evidence_ids"])
            self.assertTrue(lesson.attributes["supporting_evidence_ids"])
            self.assertTrue(engine.validate_experience(lesson)["valid"])
            repository.close()

    def test_stream_parser_handles_jsonl_chunks(self):
        content = "\n".join(json.dumps(row) for row in [
            {"type": "thread.started", "thread_id": "stream"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Failure: baseline regressed."}},
        ]) + "\n"
        episode = normalize_stream(io.StringIO(content), project=".")
        self.assertEqual(episode.source, "codex")
        self.assertTrue(episode.events)


if __name__ == "__main__":
    unittest.main()
