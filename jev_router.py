#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
KEY_ENV = "TYPESAFE_API_KEY"
HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("JEV_ROUTER_STATE", Path.home() / ".claude" / "cache" / "jev-router"))
LOG = STATE_DIR / "log.jsonl"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
LEVELS = ("trivial", "routine", "hard", "frontier")
FAMILIES = ("haiku", "sonnet", "opus", "fable")
OFF_VALUES = ("0", "false", "off", "no")

TOKENISH = re.compile(r"(?<![\w/.-])(?=[\w+/=-]*\d)(?=[\w+/=-]*[A-Za-z])[\w+/=-]{24,}")
SECRET_KV = re.compile(
    r"(?i)\b([\w-]*?(?:api_key|apikey|secret|password|passwd|token|authorization))\b"
    r"([\"']?[ \t]*[:=][ \t]*)(?:bearer[ \t]+)?(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)"
)
PEM_BLOCK = re.compile(r"-----BEGIN [^\n]*?-----.*?-----END [^\n]*?-----", re.S)
SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
AUTOMATED_PROMPT = re.compile(r"\A\s*<(scheduled-task|task-notification|ci-monitor-event)\b")

QUESTIONS = {
    "level": {
        "type": "score",
        "instructions": (
            "How much reasoning does the request in `prompt` need from an AI coding assistant "
            "before it can answer well? Judge the request itself, not its length."
        ),
        "criteria": [
            "trivial: a greeting, an acknowledgement, a short factual answer, a one-line edit, running one command, or a yes/no question",
            "routine: ordinary engineering with a known approach, such as a typical feature, a test to an existing pattern, a bug with a clear cause, or a focused document",
            "hard: needs careful reasoning where a known technique applies but must be applied with care, such as concurrency, subtle invariants, a bug with no obvious cause, a tricky algorithm, or a risky refactor",
            "frontier: the approach itself must be invented or chosen between competing architectures, such as designing a new system, a distributed-systems or consistency guarantee, a security-critical design, or research where correctness cannot be checked by a test",
        ],
    },
    "followup": {
        "type": "noul",
        "instructions": (
            "Is `prompt` a continuation of work already in progress, so that its real difficulty "
            "lives in the earlier conversation rather than in this text?"
        ),
        "criteria": {
            "true": "A short go-ahead, confirmation, correction or next-step nudge such as ok, yes, continue, do it, now the tests too, or try again",
            "false": "A self-contained request that states what to do without depending on earlier turns",
        },
    },
}


class JevUnavailable(Exception):
    pass


def enabled() -> bool:
    return os.environ.get("JEV_ROUTER", "1").lower() not in OFF_VALUES


def key_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "typesafe" / "env"


def load_key() -> str | None:
    key = (os.environ.get(KEY_ENV) or "").strip()
    if key:
        return key
    try:
        for line in key_file().read_text().splitlines():
            m = re.match(rf"\s*(?:export\s+)?{KEY_ENV}\s*=\s*(.+?)\s*$", line)
            if m:
                return m.group(1).strip("'\"") or None
    except OSError:
        return None
    return None


def load_config() -> dict:
    candidates = [
        os.environ.get("JEV_ROUTER_CONFIG"),
        Path.home() / ".config" / "jev-router" / "router.json",
        HERE / "router.json",
    ]
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if p.is_file():
            base = json.loads((HERE / "router.json").read_text())
            base.update(json.loads(p.read_text()))
            return base
    raise FileNotFoundError("router.json not found")


def ask(state: dict, questions: dict, timeout: float, model: str) -> dict:
    if not enabled():
        raise JevUnavailable("disabled by JEV_ROUTER=0")
    key = load_key()
    if not key:
        raise JevUnavailable(f"no {KEY_ENV} in the environment or {key_file()}")
    body = json.dumps({"state": state, "model": model, "questions": questions}).encode()
    req = urllib.request.Request(
        os.environ.get("JEV_ROUTER_URL", API_URL),
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "jev-model-router/1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise JevUnavailable(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise JevUnavailable(f"{type(exc).__name__}: {exc}") from exc


def redact(text: str) -> str:
    out = SYSTEM_REMINDER.sub("", text or "")
    out = PEM_BLOCK.sub("[redacted]", out)
    out = SECRET_KV.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", out)
    return TOKENISH.sub("[redacted]", out)


def family(model_id: str | None) -> str | None:
    low = (model_id or "").lower()
    for f in FAMILIES:
        if f in low:
            return f
    return None


def session_key(session_id: str) -> str:
    return hashlib.sha256((session_id or "unknown").encode()).hexdigest()[:16]


def state_path(session_id: str) -> Path:
    return STATE_DIR / f"{session_key(session_id)}.json"


def read_state(session_id: str) -> dict:
    try:
        return json.loads(state_path(session_id).read_text())
    except (OSError, ValueError):
        return {}


def write_state(session_id: str, data: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_path(session_id).write_text(json.dumps(data))
    except OSError:
        pass


def log(row: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def argmax_level(answer: dict) -> tuple[str, float]:
    probs = answer.get("probabilities") or {}
    if probs:
        idx, p = max(probs.items(), key=lambda kv: kv[1])
        return LEVELS[int(idx)], float(p)
    idx = int(round(float(answer.get("score", 1))))
    return LEVELS[max(0, min(idx, len(LEVELS) - 1))], float(answer.get("confidence", 0.0))


def decide(answers: dict, cfg: dict, current_model: str | None, current_effort: str | None, transcript_bytes: int) -> dict:
    level, confidence = argmax_level(answers.get("level", {}))
    followup = float(answers.get("followup", {}).get("noul", 0.0))
    target = cfg["ladder"][level]
    out = {
        "level": level,
        "confidence": round(confidence, 3),
        "followup": round(followup, 3),
        "model": target["model"],
        "effort": target["effort"],
        "current_model": current_model,
        "current_effort": current_effort,
        "action": "none",
        "reason": "",
    }
    if followup >= cfg["followup_min"]:
        out["reason"] = "follow-up to work in progress, keep the current setting"
        return out
    if confidence < cfg["min_confidence"]:
        out["reason"] = f"confidence {confidence:.2f} under {cfg['min_confidence']}"
        return out
    model_change = current_model is not None and family(target["model"]) != current_model
    effort_change = current_effort is not None and target["effort"] != current_effort
    if model_change and transcript_bytes >= cfg["recache_guard_bytes"]:
        model_change = False
        out["reason"] = "context is large, a model switch would re-cache it"
    if current_model is None and current_effort is None:
        out["action"] = "suggest"
    elif model_change and effort_change:
        out["action"] = "model+effort"
    elif model_change:
        out["action"] = "model"
    elif effort_change:
        out["action"] = "effort"
    return out


def user_line(d: dict) -> str:
    parts = []
    if d["action"] in ("model", "model+effort", "suggest"):
        parts.append(f"/model {d['model']}")
    if d["action"] in ("effort", "model+effort", "suggest"):
        parts.append(f"/effort {d['effort']}")
    now = f"{d['current_model'] or '?'}/{d['current_effort'] or '?'}"
    return f"jev-router: {d['level']} prompt (p={d['confidence']:.2f}) wants {family(d['model'])}/{d['effort']}, now {now}. Run: " + " then ".join(parts)


def context_block(d: dict) -> str:
    depth = {
        "trivial": "Answer directly with the fewest tool calls that give a verified answer. No exploration, no plan, no subagents.",
        "routine": "Work at normal depth. Read what the task needs and no more.",
        "hard": "Think before acting. Verify assumptions against the code and test the result.",
        "frontier": "Treat this as open-ended. Survey the option space, state assumptions, and verify every claim.",
    }[d["level"]]
    switch = ""
    if d["action"] != "none":
        switch = f" The router asked the user to run /effort {d['effort']} before the next turn. Do not try to change the model or effort yourself."

    return (
        f"JEV ROUTER: this prompt scored `{d['level']}` (p={d['confidence']:.2f}), "
        f"fit {d['model']}/{d['effort']}. {depth}{switch}"
    )


def transcript_size(path: str | None) -> int:
    try:
        return Path(path).stat().st_size if path else 0
    except OSError:
        return 0


def on_prompt(data: dict) -> int:
    prompt = str(data.get("prompt") or "")
    session_id = str(data.get("session_id") or "")
    if AUTOMATED_PROMPT.match(prompt) or os.environ.get("JEV_ROUTER_PLUGIN"):
        return 0
    cfg = load_config()
    clean = redact(prompt)[: cfg["max_prompt_chars"]].strip()
    if len(clean) < cfg["min_prompt_chars"]:
        return 0
    state = read_state(session_id)
    current_model = family(state.get("model"))
    current_effort = (os.environ.get("CLAUDE_EFFORT") or state.get("effort") or "").lower() or None
    if current_effort not in EFFORTS:
        current_effort = None
    t0 = time.monotonic()
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "session": session_key(session_id), "chars": len(clean)}
    try:
        resp = ask({"prompt": clean}, QUESTIONS, cfg["timeout_s"], cfg["jev_model"])
    except JevUnavailable as exc:
        log({**row, "event": "unavailable", "error": str(exc)[:120], "ms": int((time.monotonic() - t0) * 1000)})
        return 0
    answers = resp.get("answers") or {}
    d = decide(answers, cfg, current_model, current_effort, transcript_size(data.get("transcript_path")))
    log({**row, "event": "decision", "jev_model": resp.get("model"), "ms": int((time.monotonic() - t0) * 1000), **{k: d[k] for k in ("level", "confidence", "followup", "model", "effort", "current_model", "current_effort", "action")}})
    write_state(session_id, {**state, "last": d, "effort": current_effort or state.get("effort")})
    if d["action"] in ("model", "model+effort") and cfg.get("block_on_model_mismatch"):
        print(json.dumps({"decision": "block", "reason": user_line(d) + ". Then send the prompt again. Set block_on_model_mismatch to false in router.json to get a notice instead."}))
        return 0
    out: dict = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context_block(d)}}
    if d["action"] != "none":
        out["systemMessage"] = user_line(d)
    print(json.dumps(out))
    return 0


def on_session(data: dict) -> int:
    session_id = str(data.get("session_id") or "")
    state = read_state(session_id)
    model = data.get("model") or state.get("model")
    write_state(session_id, {**state, "model": model, "effort": os.environ.get("CLAUDE_EFFORT") or state.get("effort")})
    return 0


def on_switched(data: dict) -> int:
    session_id = str(data.get("session_id") or "")
    state = read_state(session_id)
    write_state(session_id, {**state, "model": data.get("to_model") or state.get("model")})
    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "session": session_key(session_id), "event": "switched", "to": family(data.get("to_model")), "source": data.get("source")})
    return 0


def report(days: int) -> int:
    cutoff = time.time() - days * 86400
    rows = []
    try:
        for line in LOG.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            try:
                ts = time.mktime(time.strptime(r["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
            except (KeyError, ValueError):
                continue
            if ts >= cutoff:
                rows.append(r)
    except OSError:
        print("no log yet")
        return 0
    decisions = [r for r in rows if r.get("event") == "decision"]
    unavailable = [r for r in rows if r.get("event") == "unavailable"]
    print(f"last {days}d: {len(decisions)} decisions, {len(unavailable)} unavailable")
    for lvl in LEVELS:
        n = sum(1 for r in decisions if r.get("level") == lvl)
        if n:
            print(f"  {lvl:9s} {n:4d}")
    for act in ("none", "effort", "model", "model+effort", "suggest"):
        n = sum(1 for r in decisions if r.get("action") == act)
        if n:
            print(f"  action={act:13s} {n:4d}")
    if decisions:
        print(f"  median ms {sorted(r.get('ms', 0) for r in decisions)[len(decisions) // 2]}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: jev_router.py prompt|session|switched|report [--days N]|--test [--live]")
        return 0
    if argv[0] == "--test":
        from tests.test_router import run
        return run(live="--live" in argv)
    if argv[0] == "report":
        days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 7
        return report(days)
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    try:
        if argv[0] == "prompt":
            return on_prompt(data)
        if argv[0] == "session":
            return on_session(data)
        if argv[0] == "switched":
            return on_switched(data)
    except Exception as exc:
        log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": "error", "where": argv[0], "error": f"{type(exc).__name__}: {exc}"[:200]})
        return 0
    print(f"unknown subcommand {argv[0]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
