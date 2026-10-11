# jev-model-router

A Claude Code plugin that scores every prompt with [TypeSafe Jev](https://docs.typesafe.ai) and runs the turn on the model and effort the prompt deserves. Jev is a non-generative model: it takes a state and typed questions and returns calibrated probabilities in under a second. The plugin thresholds the numbers in code and rewrites the model request itself. No prose gets parsed, nothing waits for you.

## What happens on each prompt

1. `prompt.submit`: the redacted prompt goes to Jev with two questions in one call, a four-level `score` (trivial, routine, hard, frontier) and a `noul` asking whether the prompt is a follow-up to work already in progress. The level maps to a model and effort through the ladder in `router.json`. A short context block tells Claude the level and how deep to work.
2. `turn.step`: every model request of that turn is rewritten to the chosen effort, and to a stronger model when the rung names a `min_model` the session falls below. A request already on that model keeps its exact id. The default ladder never downgrades: a rung with an explicit `model` cheaper than the session's model is applied only when writing the context to that model's cache costs less than reading it from the current model's warm cache. The status line says when a downgrade was refused.
3. The status line under the prompt shows the decision. Scores, timings and costs go to `~/.claude/cache/jev-router/log.jsonl`, never prompt text.

Measured on Claude Code 2.1.296 with the wire probe in `scripts/`: the rewritten request reaches the API with the new `model` and `output_config.effort`, and the assistant message comes back from that model.

## Why effort first and upgrades only

Prompt caches are model-scoped, and Claude Code re-sends the whole conversation on every request. Measured on a 228K-token context:

| Change | Cache written on the next request | Why |
| --- | --- | --- |
| Effort low to high, same model | 17.5K tokens | Claude Code sends effort as per-message system entries, so the prefix stays cached. |
| Opus to Haiku | 229K tokens on Haiku | The whole context is written once on the cheaper model. |
| Haiku back to Opus | 17.9K tokens on Opus | The Opus prefix was still warm, so only the delta was written. |

So an effort change is close to free and a model change costs one full cache write at the target model's price. For a trivial prompt that produces almost no output, that write is the whole bill. Per million tokens of context, reading the warm Opus cache costs 0.20 USD, writing the Haiku cache costs 0.125 USD under 100K tokens and 0.625 USD above it. A downgrade saves a fraction of a cent on a small session and loses money on a large one. That is why the default ladder changes effort only, and why an explicit downgrade rung is applied only when the target's write cost is under the current model's read cost, computed from the live context size and the prices in `router.json`.

Upgrades are never cost-gated. A hard prompt in a Sonnet session runs on Opus, and the cache write it triggers would have happened anyway. The quality win is the point.

The `frontier` level maps to the same rung as `hard` by default. Jev cannot separate the two from prompt text alone (a judgment question about this plugin scored frontier at 0.65), and `max` effort is the one setting that gets expensive on a wrong call. Give `frontier` its own rung once an eval on your own prompts shows the level is reliable.

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
| `ladder` | trivial low, routine medium, hard and frontier high with `min_model` Opus | One rung per level. `effort` is required. `min_model` upgrades a weaker session to at least that model. `model` pins the turn to that model and allows a cost-gated downgrade. Full ids only, such as `claude-haiku-5-5`, since the request rewrite refuses aliases. |
| `min_confidence` | 0.55 | Below this the turn keeps its current model and effort. |
| `followup_min` | 0.70 | At or above this the prompt counts as a follow-up and nothing changes. |
| `pricing_usd_per_mtok` | list prices | Per family: `input`, `cache_read`, and the over-100K tiers for Haiku. |
| `cache_write_multiplier` | 1.25 | 1.25 for the 5-minute cache, 2 for the 1-hour cache. |
| `sample_rate` | 0 | Fraction of prompts to copy, redacted, into `samples.jsonl` for labeling. See Eval. |
| `block_on_model_mismatch` | true | Command-hook fallback only. |
| `timeout_s` | 3.5 | Jev call timeout. On timeout the turn runs unchanged. |
| `jev_model` | `jev-latest` | Pin to a dated version once you tune the thresholds. |

Environment: `JEV_ROUTER=0` disables the command hook, `JEV_ROUTER_STATE` moves the log directory, `JEV_ROUTER_FORCE=trivial,hard` replaces Jev with a fixed sequence of levels for measurements.

## Eval on your own prompts

The bundled calibration cases are clean prompts the author wrote. Real prompts are short and contextual, and the level Jev returns for them is the thing to measure. Set `sample_rate` to 0.2 or so, work for a few days, then open `~/.claude/cache/jev-router/samples.jsonl`: each row holds the redacted prompt, Jev's level and probabilities, and a `label` field set to null. Fill `label` with the level you would have chosen (trivial, routine, hard, frontier) and run:

```bash
python3 jev_router.py eval ~/.claude/cache/jev-router/samples.jsonl
```

It re-scores every labeled row and prints a per-row verdict and a confusion matrix. Thirty labeled prompts tell you more than the bundled eleven. Raise `min_confidence` or merge levels in the ladder according to what the matrix shows. The sample file holds prompt text, so keep it local and set `sample_rate` back to 0 when done.

## Verify

```bash
claude plugin validate .
python3 jev_router.py --test          # decision table, hook round trip, block rule, redaction
python3 jev_router.py --test --live   # plus the live calibration cases against Jev
python3 jev_router.py report --days 7
```

Wire probe: start `python3 scripts/wire-probe-proxy.py` (listens on 127.0.0.1:18791 and logs model, effort and usage per request, never message text), then run a measurement with `ANTHROPIC_BASE_URL=http://127.0.0.1:18791`. `MEASURE_MODEL` sets the session's starting model:

```bash
MEASURE_MODEL=sonnet python3 scripts/measure.py up '{"trivial":{"effort":"low"},"routine":{"effort":"medium"},"hard":{"min_model":"claude-opus-5-5","effort":"high"},"frontier":{"min_model":"claude-opus-5-5","effort":"high"}}' hard,trivial "Reply with the single word ok" "Reply with the single word ok"
```

Measured with that command: the hard turn left the Sonnet session on `claude-opus-5-5` at effort high, and the trivial turn ran on `claude-sonnet-5-5` at effort low.

Calibration on `jev-1.13.0` with the cases in `tests/test_router.py`: 7 of 7 level cases and 4 of 4 follow-up cases land on the intended answer, median 450 ms per call.

## Privacy

The prompt leaves the machine. Before it does, the plugin strips `<system-reminder>` blocks, PEM blocks, `key=value` secrets and long token-shaped strings, and clips to `max_prompt_chars`. Prompts that start with `<scheduled-task>`, `<task-notification>` or `<ci-monitor-event>` are never sent. Subagent requests are never rewritten.

## Fail-open contract

No key, no network, a timeout, a non-200 or any exception inside a hook leaves the turn on the session's own model and effort. The unavailable path is logged.
