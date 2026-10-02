"""An offline CLI fixture. This is not an AI model or a live performance benchmark."""

import json
import sys
from pathlib import Path

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
prompt = sys.stdin.read()
arguments = sys.argv[1:]
tile = 128 if "256 regressed" in prompt else 256
report = f"Decision: Select tile={tile} for ragged reduction based on the inherited experience.\nEvaluator: Validate ragged reduction correctness against the oracle before comparing latency."
if "--save-prompt" in arguments:
    Path(arguments[arguments.index("--save-prompt") + 1]).write_text(prompt, encoding="utf-8")
code = int(arguments[arguments.index("--exit-code") + 1]) if "--exit-code" in arguments else 0
if "exec" in arguments:
    events = [{"type": "thread.started", "thread_id": "fixture-codex"},
              {"type": "item.completed", "item": {"id": "message", "type": "agent_message", "text": report}}]
else:
    events = [{"type": "assistant", "session_id": "fixture-claude", "message": {"content": [{"type": "text", "text": report}]}},
              {"type": "result", "session_id": "fixture-claude", "result": report}]
for event in events:
    print(json.dumps(event, ensure_ascii=False), flush=True)
raise SystemExit(code)

