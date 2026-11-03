from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jev_router as jr

CFG = json.loads((Path(__file__).resolve().parents[1] / "router.json").read_text())

LIVE_CASES = [
    ("hi", "trivial"),
    ("thanks, that works", "trivial"),
    ("what does git stash pop do?", "trivial"),
    ("add a --verbose flag to the CLI that prints each processed file path, with a test", "routine"),
    ("write a unit test for parse_duration following the pattern in tests/test_units.py", "routine"),
    (
        "the worker pool deadlocks about once an hour under load; the stack traces show two threads in acquire(). "
        "find the cause and fix it without changing the public API",
        "hard",
    ),
    (
        "design a sharding scheme for the event store that keeps ordering guarantees per aggregate while allowing "
        "online rebalancing with no downtime; there is no prior art in this codebase",
        "frontier",
    ),
]

FOLLOWUP_CASES = [("ok do it", True), ("yes", True), ("continue", True), ("rename the User class to Account across the repo", False)]


def fake_answers(level: str, p: float = 0.9, followup: float = 0.05) -> dict:
    idx = jr.LEVELS.index(level)
    probs = {str(i): (p if i == idx else (1 - p) / 3) for i in range(4)}
    return {"level": {"score": idx, "confidence": p, "probabilities": probs}, "followup": {"noul": followup}}


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(("PASS " if cond else "FAIL ") + name + (f": {detail}" if detail else ""))
    return cond


def offline() -> list[bool]:
    r = []
    d = jr.decide(fake_answers("trivial"), CFG, "opus", "high", 0)
    r.append(check("trivial on opus/high asks for effort only, never a downgrade", d["action"] == "effort" and d["effort"] == "low", d["action"]))
    d = jr.decide(fake_answers("hard"), CFG, "opus", "high", 0)
    r.append(check("hard on opus/high changes nothing", d["action"] == "none", d["action"]))
    d = jr.decide(fake_answers("hard"), CFG, "sonnet", "high", 0)
    r.append(check("hard on sonnet upgrades to opus", d["action"] == "model" and jr.family(d["model"]) == "opus", d["action"]))
    d = jr.decide(fake_answers("frontier"), CFG, "sonnet", "medium", 0)
    r.append(check("frontier folds into hard", d["action"] == "model+effort" and d["effort"] == "high", d["action"]))
    d = jr.decide(fake_answers("routine"), CFG, "opus", "high", 0)
    r.append(check("routine on opus/high asks for medium effort", d["action"] == "effort" and d["effort"] == "medium", d["action"]))
    down = {**CFG, "ladder": {**CFG["ladder"], "trivial": {"model": "claude-haiku-5-5", "effort": "low"}}}
    d = jr.decide(fake_answers("trivial"), down, "opus", "high", 0)
    r.append(check("explicit downgrade rung is honored with a warning", d["action"] == "model+effort" and "downgrade" in d["reason"], d["reason"]))
    d = jr.decide(fake_answers("trivial", followup=0.9), CFG, "opus", "high", 0)
    r.append(check("follow-up keeps current setting", d["action"] == "none", d["reason"]))
    d = jr.decide(fake_answers("trivial", p=0.4), CFG, "opus", "high", 0)
    r.append(check("low confidence keeps current setting", d["action"] == "none", d["reason"]))
    d = jr.decide(fake_answers("hard"), CFG, None, None, 0)
    r.append(check("unknown current state only suggests", d["action"] == "suggest", d["action"]))
    r.append(check("family parses model ids", jr.family("claude-opus-5-5") == "opus" and jr.family("claude-haiku-5-5[1m]") == "haiku" and jr.family(None) is None))
    red = jr.redact("key TYPESAFE_API_KEY=abc123def456ghi789jkl012mno345 and token: sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123")
    r.append(check("redaction strips secrets", "abc123" not in red and "ABCDEFGHIJ" not in red, red))
    r.append(check("automated prompts are skipped", bool(jr.AUTOMATED_PROMPT.match("<scheduled-task id=1>")) and not jr.AUTOMATED_PROMPT.match("fix it")))
    line = jr.user_line(jr.decide(fake_answers("hard"), CFG, "sonnet", "medium", 0))
    r.append(check("user line names both commands", "/model claude-opus-5-5" in line and "/effort high" in line, line))
    return r


def hook_roundtrip() -> list[bool]:
    r = []
    os.environ.pop("JEV_ROUTER_PLUGIN", None)
    with tempfile.TemporaryDirectory() as tmp:
        old_state, old_log = jr.STATE_DIR, jr.LOG
        jr.STATE_DIR = Path(tmp)
        jr.LOG = Path(tmp) / "log.jsonl"
        try:
            jr.on_session({"session_id": "s1", "model": "claude-opus-5-5"})
            jr.on_switched({"session_id": "s1", "to_model": "claude-sonnet-5-5", "source": "command"})
            st = jr.read_state("s1")
            r.append(check("session and switch hooks track the model", jr.family(st.get("model")) == "sonnet", str(st)))
            os.environ["JEV_ROUTER"] = "0"
            buf = io.StringIO()
            with redirect_stdout(buf):
                jr.on_prompt({"session_id": "s1", "prompt": "add a flag please"})
            r.append(check("unavailable Jev fails open with no output", buf.getvalue() == ""))
            rows = [json.loads(l) for l in jr.LOG.read_text().splitlines()]
            r.append(check("unavailable path is logged without prompt text", any(x["event"] == "unavailable" for x in rows) and "flag" not in jr.LOG.read_text()))
            os.environ["JEV_ROUTER_PLUGIN"] = "1"
            buf = io.StringIO()
            with redirect_stdout(buf):
                jr.on_prompt({"session_id": "s1", "prompt": "what does git stash pop do?"})
            os.environ.pop("JEV_ROUTER_PLUGIN", None)
            r.append(check("command hook stands down when the plugin is active", buf.getvalue() == ""))
            jr.ask_backup = jr.ask
            jr.ask = lambda *a, **k: {"answers": fake_answers("trivial"), "model": "fake"}
            try:
                jr.ask = lambda *a, **k: {"answers": fake_answers("hard"), "model": "fake"}
                jr.on_session({"session_id": "s2", "model": "claude-sonnet-5-5"})
                os.environ["CLAUDE_EFFORT"] = "medium"
                buf = io.StringIO()
                with redirect_stdout(buf):
                    jr.on_prompt({"session_id": "s2", "prompt": "what does git stash pop do?", "transcript_path": "/nonexistent"})
                out = json.loads(buf.getvalue())
                r.append(check("model mismatch blocks the prompt with the /model command", out.get("decision") == "block" and "/model claude-opus-5-5" in out.get("reason", ""), buf.getvalue()[:160]))
                jr.on_switched({"session_id": "s2", "to_model": "claude-opus-5-5", "source": "command"})
                jr.ask = lambda *a, **k: {"answers": fake_answers("trivial"), "model": "fake"}
                buf = io.StringIO()
                with redirect_stdout(buf):
                    jr.on_prompt({"session_id": "s2", "prompt": "what does git stash pop do?", "transcript_path": "/nonexistent"})
                out = json.loads(buf.getvalue())
                r.append(check("effort-only mismatch does not block", "decision" not in out and "/effort low" in out.get("systemMessage", ""), buf.getvalue()[:160]))
            finally:
                jr.ask = jr.ask_backup
                os.environ.pop("CLAUDE_EFFORT", None)
        finally:
            os.environ.pop("JEV_ROUTER", None)
            jr.STATE_DIR, jr.LOG = old_state, old_log
    return r


def report_window() -> list[bool]:
    r = []
    with tempfile.TemporaryDirectory() as tmp:
        old_log = jr.LOG
        jr.LOG = Path(tmp) / "log.jsonl"
        try:
            plugin_row = {"ts": jr.now_iso(), "source": "plugin", "event": "decision", "level": "routine", "action": "effort", "ms": 300}
            legacy_row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": "decision", "level": "hard", "action": "model", "ms": 400}
            old_row = {"ts": "2020-01-01T00:00:00Z", "event": "decision", "level": "trivial", "action": "none", "ms": 1}
            jr.LOG.write_text("\n".join(json.dumps(x) for x in (plugin_row, legacy_row, old_row)) + "\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                jr.report(1)
            out = buf.getvalue()
            r.append(check("report counts hook-style UTC Z rows and legacy local-offset rows in the window", "last 1d: 2 decisions" in out))
            r.append(check("now_iso writes UTC with a trailing Z", plugin_row["ts"].endswith("Z") and abs(jr.parse_ts(plugin_row["ts"]) - time.time()) < 5))
            r.append(check("legacy local-offset ts parses to the same instant", abs(jr.parse_ts(legacy_row["ts"]) - time.time()) < 5))
        finally:
            jr.LOG = old_log
    return r


def live() -> list[bool]:
    r = []
    for prompt, want in LIVE_CASES:
        resp = jr.ask({"prompt": prompt}, jr.QUESTIONS, 8.0, CFG["jev_model"])
        level, p = jr.argmax_level(resp["answers"]["level"])
        r.append(check(f"live level {want!r}", level == want, f"got {level} p={p:.2f} for {prompt[:50]!r}"))
    for prompt, want in FOLLOWUP_CASES:
        resp = jr.ask({"prompt": prompt}, {"followup": jr.QUESTIONS["followup"]}, 8.0, CFG["jev_model"])
        f = float(resp["answers"]["followup"]["noul"])
        r.append(check(f"live followup {want}", (f >= CFG["followup_min"]) == want, f"p={f:.2f} for {prompt!r}"))
    return r


def run(live_too: bool = False, live: bool | None = None) -> int:
    results = offline() + hook_roundtrip() + report_window()
    if live_too or live:
        try:
            results += globals()["live"]()
        except jr.JevUnavailable as exc:
            print(f"SKIP live: {exc}")
            return 2
    failed = results.count(False)
    print(f"{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(live_too="--live" in sys.argv))
