# jev-model-router

A Claude Code plugin that scores every prompt with [TypeSafe Jev](https://docs.typesafe.ai) and runs the turn on the model and effort the prompt deserves. Jev is a non-generative model: it takes a state and typed questions and returns calibrated probabilities in under a second. The plugin thresholds the numbers in code and rewrites the model request itself. No prose gets parsed, nothing waits for you.

## What happens on each prompt

1. `prompt.submit`: the redacted prompt goes to Jev with two questions in one call, a four-level `score` (trivial, routine, hard, frontier) and a `noul` asking whether the prompt is a follow-up to work already in progress. The level maps to a model and effort through the ladder in `router.json`. A short context block tells Claude the level and how deep to work.
2. `turn.step`: every model request of that turn is rewritten to the chosen model and effort. A request already on that model keeps its exact id. A downgrade that would re-cache more than `max_downgrade_usd` is skipped and the status line says so. Upgrades always go through.
3. The status line under the prompt shows the decision. Scores, timings and costs go to `~/.claude/cache/jev-router/log.jsonl`, never prompt text.

Measured on Claude Code 2.1.296 with the wire probe in `scripts/`: the rewritten request reaches the API with the new `model` and `output_config.effort`, and the assistant message comes back from that model.

## What a switch costs

Prompt caches are model-scoped, and Claude Code re-sends the whole conversation on every request. Measured on a 228K-token context:

| Change | Cache written on the next request | Why |
| --- | --- | --- |
| Effort low to high, same model | 17.5K tokens | Claude Code sends effort as per-message system entries, so the prefix stays cached. |
| Opus to Haiku | 229K tokens on Haiku | The whole context is written once on the cheaper model: about 0.14 USD at Haiku's over-100K rate. |
| Haiku back to Opus | 17.9K tokens on Opus | The Opus prefix was still warm, so only the delta was written. |

So a downgrade costs one full cache write at the cheaper model's price, and the return costs nothing extra while the expensive model's cache is still warm (5 minutes by default, 1 hour on some plans). The plugin estimates the downgrade cost from the live context size and the prices in `router.json`, and refuses downgrades over `max_downgrade_usd` (default 0.25 USD). The estimate is list price. Set `cache_write_multiplier` to 2 if your sessions use the 1-hour cache.

The plugin never cost-gates an upgrade. If the hard prompt needs Opus, it gets Opus. The write it triggers is the one the session would have paid anyway once its cache expired.

## Install

From a terminal session:

```
/plugin install jev-model-router --marketplace m-esm/jev-model-router
```

Answer `y` to add the marketplace and pick the user scope. Or clone and load it from the folder:

```bash
git clone https://github.com/m-esm/jev-model-router.git
claude plugin marketplace add ./jev-model-router
claude plugin install jev-model-router@jev-model-router --scope user
```

The key goes in `TYPESAFE_API_KEY` or in `~/.config/typesafe/env` as `TYPESAFE_API_KEY=...` (mode 600).

### Command-hook fallback

`jev_router.py` is the same decision as classic settings hooks for a Claude Code without plugin support. `./install.sh` wires it into `~/.claude/settings.json`, `./install.sh uninstall` removes it. A classic hook cannot change the model, so when the prompt wants a different model it blocks the prompt with the exact `/model` command to run, and you send the prompt again. When only the effort differs it lets the prompt through with a one-line notice. Set `block_on_model_mismatch` to false to get a notice in both cases. When the plugin is loaded it sets `JEV_ROUTER_PLUGIN=1` and the command hook stands down, so both can stay installed.

## Configure

Copy `router.json` to `~/.config/jev-router/router.json` or point `JEV_ROUTER_CONFIG` at a file. Keys you set override the defaults.

| Key | Default | Meaning |
| --- | --- | --- |
| `ladder` | haiku/low, sonnet/medium, opus/high, opus/max | Full model id and effort per level. Bare aliases such as `haiku` are refused by the request rewrite, so use ids like `claude-haiku-5-5`. |
| `min_confidence` | 0.55 | Below this the turn keeps its current model and effort. |
| `followup_min` | 0.70 | At or above this the prompt counts as a follow-up and nothing changes. |
| `max_downgrade_usd` | 0.25 | Largest estimated re-cache a downgrade may cost. |
| `pricing_input_usd_per_mtok` | list prices | Input price per family, with `input_over_100k` for Haiku's tier. |
| `cache_write_multiplier` | 1.25 | 1.25 for the 5-minute cache, 2 for the 1-hour cache. |
| `recache_guard_bytes` | 1500000 | Command-hook fallback only: transcript size above which it stops asking for model switches. |
| `block_on_model_mismatch` | true | Command-hook fallback only. |
| `timeout_s` | 3.5 | Jev call timeout. On timeout the turn runs unchanged. |
| `jev_model` | `jev-latest` | Pin to a dated version once you tune the thresholds. |

Environment: `JEV_ROUTER=0` disables the command hook, `JEV_ROUTER_STATE` moves the log directory, `JEV_ROUTER_FORCE=trivial,hard` replaces Jev with a fixed sequence of levels for measurements.

## Verify

```bash
claude plugin validate .
python3 jev_router.py --test          # decision table, hook round trip, block rule, redaction
python3 jev_router.py --test --live   # plus the live calibration cases against Jev
python3 jev_router.py report --days 7
```

Wire probe: start `python3 scripts/wire-probe-proxy.py` (listens on 127.0.0.1:18791 and logs model, effort and usage per request, never message text), then run a measurement with `ANTHROPIC_BASE_URL=http://127.0.0.1:18791`:

```bash
python3 scripts/measure.py switch '{"trivial":{"model":"claude-haiku-5-5","effort":"low"},"routine":{"model":"claude-sonnet-5-5","effort":"medium"},"hard":{"model":"claude-opus-5-5","effort":"high"},"frontier":{"model":"claude-opus-5-5","effort":"max"}}' hard,trivial,hard "Reply with the single word ok" "Reply with the single word ok" "Reply with the single word ok"
```

Calibration on `jev-1.13.0` with the cases in `tests/test_router.py`: 7 of 7 level cases and 4 of 4 follow-up cases land on the intended answer, median 450 ms per call.

## Privacy

The prompt leaves the machine. Before it does, the plugin strips `<system-reminder>` blocks, PEM blocks, `key=value` secrets and long token-shaped strings, and clips to `max_prompt_chars`. Prompts that start with `<scheduled-task>`, `<task-notification>` or `<ci-monitor-event>` are never sent. Subagent requests are never rewritten.

## Fail-open contract

No key, no network, a timeout, a non-200 or any exception inside a hook leaves the turn on the session's own model and effort. The unavailable path is logged.
