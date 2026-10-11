# jev-model-router

A Claude Code hook that scores every prompt with [TypeSafe Jev](https://docs.typesafe.ai) and decides which model and effort level the prompt deserves. Jev is a non-generative model: it takes a state and typed questions and returns calibrated probabilities in under a second. The hook thresholds the numbers in code. No prose gets parsed.

## What it does on each prompt

1. `UserPromptSubmit` sends the redacted prompt to Jev with two questions in one call: a four-level `score` (trivial, routine, hard, frontier) and a `noul` asking whether the prompt is a follow-up to work already in progress.
2. The level maps to a model and effort through the ladder in `router.json`.
3. If the fit differs from the session's current model and effort, the hook prints one line to the user with the exact `/model` and `/effort` commands to run.
4. The hook always adds a short context block for Claude: the level, the fit, and a depth instruction (answer directly, normal depth, think first, or treat as open-ended). That part works without any switch.
5. Every decision is logged as numbers, never prompt text. `jev_router.py report` summarizes the log.

Measured on `jev-1.13.0` with the cases in `tests/test_router.py`: 7 of 7 level cases and 4 of 4 follow-up cases land on the intended answer, median 450 ms per call.

## Why the hook does not switch the model itself

No Claude Code hook output can change the running session's model or effort. Verified on Claude Code 2.1.296:

- The hooks reference lists no output field for it. `PreModelSwitch` can only block or confirm a switch the user requested.
- Editing `model` or `effortLevel` in `settings.json` during a live interactive session changed neither. `$CLAUDE_EFFORT` stayed the same on the next turn and no `PostModelSwitch` hook fired.
- The desktop app's session tools refuse to re-price the calling session.

So the hook decides, tells you the switch in one line, and steers Claude's own depth through context. An Agent SDK host can read the decision from the per-session state file under `~/.claude/cache/jev-router/` and send `set_model` itself.

## Install

```bash
git clone https://github.com/m-esm/jev-model-router.git
cd jev-model-router
./install.sh
```

`install.sh` merges three entries into `~/.claude/settings.json`: `UserPromptSubmit` (the decision), `SessionStart` (records the session's starting model), and `PostModelSwitch` (records the model after you switch). `./install.sh uninstall` removes them. Set `CLAUDE_SETTINGS` to target another settings file.

The key goes in `TYPESAFE_API_KEY` or in `~/.config/typesafe/env` as `TYPESAFE_API_KEY=...` (mode 600).

## Configure

Copy `router.json` to `~/.config/jev-router/router.json` or point `JEV_ROUTER_CONFIG` at a file. Keys you set override the defaults.

| Key | Default | Meaning |
| --- | --- | --- |
| `ladder` | haiku/low, sonnet/medium, opus/high, opus/max | Model and effort per level. Use the names your `/model` picker accepts. |
| `min_confidence` | 0.55 | Below this the hook keeps the current setting. |
| `followup_min` | 0.70 | At or above this the prompt counts as a follow-up and nothing changes. |
| `recache_guard_bytes` | 1500000 | Transcript size above which the hook stops suggesting model switches, because a switch re-caches the whole context. Effort suggestions still go out. |
| `timeout_s` | 3.5 | Jev call timeout. On timeout the hook exits silently. |
| `jev_model` | `jev-latest` | Pin to a dated version once you tune the thresholds. |

Environment: `JEV_ROUTER=0` disables the hook, `JEV_ROUTER_STATE` moves the state and log directory, `JEV_ROUTER_URL` overrides the endpoint.

## Verify

```bash
python3 jev_router.py --test          # offline: decision table, hook round trip, redaction
python3 jev_router.py --test --live   # plus the live calibration cases against Jev
python3 jev_router.py report --days 7
```

## Privacy

The prompt leaves the machine. Before it does, the hook strips `<system-reminder>` blocks, PEM blocks, `key=value` secrets and long token-shaped strings, and clips to `max_prompt_chars`. Prompts that start with `<scheduled-task>`, `<task-notification>` or `<ci-monitor-event>` are never sent. The log holds scores, timings and a session hash, never text.

## Fail-open contract

No key, no network, a timeout, a non-200 or any exception inside the hook exits 0 with no output, so a Jev outage never blocks a prompt. The unavailable path is logged.
