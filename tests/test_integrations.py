import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pel.adapters import normalize
from pel.engine import ExperienceEngine
from pel.integrations import agent_command, claude_hook, install_claude_hooks, run_agent
from pel.models import TaskContext
from pel.repository import SQLiteRepository
from pel.watcher import watch

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "fake_agent.py"


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="pel integration ")
        self.root = Path(self.directory.name)
        self.repo = SQLiteRepository(self.root / "pel.sqlite3")
        self.engine = ExperienceEngine(self.repo)

    def tearDown(self):
        self.repo.close()
        self.directory.cleanup()

    def seed(self):
        source = {"session_id":"seed", "project":str(self.root), "objective":"Optimize ragged reduction", "events":[{"role":"assistant", "text":"Failure: Increasing tile size to 256 regressed ragged reduction latency by 19%.\nArtifact: Reuse the benchmark script at benchmarks/ragged.py."}]}
        self.engine.ingest(normalize(json.dumps(source)))

    def test_cross_agent_wrapper_injects_and_ingests_automatically(self):
        self.seed()
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                prompt_path = self.root / (agent + " prompt.txt")
                context = TaskContext("Optimize ragged reduction; compare latency and reuse the benchmark.", str(self.root), agent=agent)
                with contextlib.redirect_stdout(io.StringIO()):
                    result = run_agent(self.engine, context, executable=str(FIXTURE), extra_args=["--save-prompt", str(prompt_path)])
                self.assertEqual(result["exit_code"], 0)
                self.assertIn("256 regressed", prompt_path.read_text(encoding="utf-8"))
                self.assertIn("# Current user task", prompt_path.read_text(encoding="utf-8"))
                self.assertTrue(any("Select tile=128" in e.statement for e in self.repo.experiences()))
                self.assertTrue(Path(result["log"]).is_file())
                self.assertGreater(result["ingestion"]["created"] + result["ingestion"]["merged"], 0)

    def test_wrapper_preserves_agent_exit_code(self):
        context = TaskContext("Inspect ragged reduction", str(self.root), agent="codex")
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_agent(self.engine, context, executable=str(FIXTURE), extra_args=["--exit-code", "7"])
        self.assertEqual(result["exit_code"], 7)

    def test_dry_run_has_no_compilation_or_agent_side_effect(self):
        self.seed()
        context = TaskContext("Optimize ragged reduction", str(self.root), agent="codex")
        before = len(self.repo.compilations())
        result = run_agent(self.engine, context, executable=str(FIXTURE), dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(len(self.repo.compilations()), before)
        self.assertFalse((self.root / "runs").exists())

    @unittest.skipUnless(os.name == "nt", "Windows npm shim")
    def test_windows_npm_shim_resolves_without_a_shell(self):
        shim = self.root / "codex.cmd"
        entry = self.root / "node_modules" / "example" / "cli.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("console.log('fixture')", encoding="utf-8")
        shim.write_text('@echo off\nnode "%dp0%\\node_modules\\example\\cli.js" %*', encoding="utf-8")
        command = agent_command("codex", str(self.root), executable=str(shim), extra_args=["--model", "name&literal"])
        self.assertTrue(command[0].endswith("node.EXE") or command[0].lower().endswith("node.exe"))
        self.assertEqual(Path(command[1]), entry)
        self.assertIn("name&literal", command)

    def test_claude_hooks_inject_context_and_capture_unflushed_final_message(self):
        self.seed()
        payload = {"hook_event_name":"UserPromptSubmit", "prompt":"Optimize ragged reduction", "cwd":str(self.root), "session_id":"live-session"}
        output = claude_hook(self.engine, payload, domain="performance")
        self.assertIn("256 regressed", output["hookSpecificOutput"]["additionalContext"])
        transcript = self.root / "session.jsonl"
        row = {"type":"user", "sessionId":"live-session", "cwd":str(self.root), "message":{"content":"Optimize ragged reduction"}}
        transcript.write_text(json.dumps(row)+"\n", encoding="utf-8")
        payload = {"hook_event_name":"Stop", "cwd":str(self.root), "transcript_path":str(transcript), "last_assistant_message":"Outcome: Ragged reduction latency improved by 12%."}
        self.assertEqual(claude_hook(self.engine, payload), {})
        self.assertTrue(any("12%" in e.statement for e in self.repo.experiences()))
        # A later transcript flush merges rather than duplicates the same statement.
        row = {"type":"assistant", "sessionId":"live-session", "message":{"content":[{"type":"text", "text":payload["last_assistant_message"]}]}}
        with transcript.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row)+"\n")
        payload["hook_event_name"] = "SessionEnd"
        del payload["last_assistant_message"]
        before = len(self.repo.experiences())
        claude_hook(self.engine, payload)
        self.assertEqual(len(self.repo.experiences()), before)

    def test_install_hooks_preserves_existing_settings_and_is_idempotent(self):
        config = self.root / ".claude" / "settings.local.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"permissions":{"allow":["Read"]}, "hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo old"}]}]}}), encoding="utf-8")
        result = install_claude_hooks(self.repo.path, str(self.root))
        self.assertEqual(result["added"], 3)
        self.assertTrue(Path(result["backup"]).is_file())
        settings = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"], {"allow":["Read"]})
        self.assertEqual(len(settings["hooks"]["Stop"]), 2)
        self.assertEqual(install_claude_hooks(self.repo.path, str(self.root))["added"], 0)
        command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn("hook_launcher.py", command)

    def test_watcher_retries_append_and_is_idempotent(self):
        log = self.root / "codex.jsonl"
        start = {"type":"thread.started", "thread_id":"watched"}
        first = {"type":"item.completed", "item":{"id":"a", "type":"agent_message", "text":"Failure: Ragged reduction tile=256 regressed latency."}}
        log.write_text(json.dumps(start)+"\n"+json.dumps(first)+"\n"+'{"type":', encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(watch(self.engine, str(self.root), source="codex", project=str(self.root), once=True)["imported"], 1)
        second = {"type":"item.completed", "item":{"id":"b", "type":"agent_message", "text":"Evaluator: Verify ragged reduction correctness against oracle."}}
        log.write_text(json.dumps(start)+"\n"+json.dumps(first)+"\n"+json.dumps(second)+"\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            watch(self.engine, str(self.root), source="codex", project=str(self.root), once=True)
            self.assertEqual(watch(self.engine, str(self.root), source="codex", project=str(self.root), once=True)["unchanged"], 1)
        self.assertEqual(len(self.repo.experiences()), 2)

    def test_cli_reports_bad_input_and_launches_from_installed_hook(self):
        result = subprocess.run([sys.executable,"-m","pel","brief","--task","Test workload","--budget","1","--db",self.repo.path], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("budget_tokens", result.stderr)
        launcher = ROOT / "pel" / "hook_launcher.py"
        result = subprocess.run([sys.executable,str(launcher),"--db",self.repo.path], input='{"cwd":"'+str(self.root).replace("\\","\\\\")+'","hook_event_name":"unknown"}', cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

