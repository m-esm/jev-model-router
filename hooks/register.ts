import type { Register, EngineInterface } from 'claude-code'
import type { Decision, Effort, Level } from '../types'

const DECISION = { plugin: 'jev-model-router', key: 'decision' } as const
const API_URL = 'https://api.typesafe.ai/v1/systemone'
const LEVELS: readonly Level[] = ['trivial', 'routine', 'hard', 'frontier']
const FAMILIES = ['haiku', 'sonnet', 'opus', 'fable'] as const
const AUTOMATED = /^\s*<(scheduled-task|task-notification|ci-monitor-event)\b/
const SYSTEM_REMINDER = /<system-reminder>[\s\S]*?<\/system-reminder>/g
const PEM = /-----BEGIN [^\n]*?-----[\s\S]*?-----END [^\n]*?-----/g
const SECRET_KV = /\b([\w-]*?(?:api_key|apikey|secret|password|passwd|token|authorization))\b(["']?[ \t]*[:=][ \t]*)(?:bearer[ \t]+)?("[^"\n]*"|'[^'\n]*'|[^\s,;]+)/gi
const TOKENISH = /(?<![\w/.-])(?=[\w+/=-]*\d)(?=[\w+/=-]*[A-Za-z])[\w+/=-]{24,}/g
const LOG_CAP_BYTES = 2_000_000

const QUESTIONS = {
  level: {
    type: 'score',
    instructions:
      'How much reasoning does the request in `prompt` need from an AI coding assistant before it can answer well? Judge the request itself, not its length.',
    criteria: [
      'trivial: a greeting, an acknowledgement, a short factual answer, a one-line edit, running one command, or a yes/no question',
      'routine: ordinary engineering with a known approach, such as a typical feature, a test to an existing pattern, a bug with a clear cause, or a focused document',
      'hard: needs careful reasoning where a known technique applies but must be applied with care, such as concurrency, subtle invariants, a bug with no obvious cause, a tricky algorithm, or a risky refactor',
      'frontier: the approach itself must be invented or chosen between competing architectures, such as designing a new system, a distributed-systems or consistency guarantee, a security-critical design, or research where correctness cannot be checked by a test',
    ],
  },
  followup: {
    type: 'noul',
    instructions:
      'Is `prompt` a continuation of work already in progress, so that its real difficulty lives in the earlier conversation rather than in this text?',
    criteria: {
      true: 'A short go-ahead, confirmation, correction or next-step nudge such as ok, yes, continue, do it, now the tests too, or try again',
      false: 'A self-contained request that states what to do without depending on earlier turns',
    },
  },
}

type Config = {
  ladder: Record<Level, { model: string; effort: Effort }>
  min_confidence: number
  followup_min: number
  min_prompt_chars: number
  max_prompt_chars: number
  timeout_s: number
  jev_model: string
  pricing_input_usd_per_mtok: Record<string, { input: number; input_over_100k?: number }>
  cache_write_multiplier: number
  max_downgrade_usd: number
}

type Answers = {
  level?: { score?: number; confidence?: number; probabilities?: Record<string, number> }
  followup?: { noul?: number }
}

let configCache: Config | undefined
let forcedIndex = 0

async function loadConfig($: EngineInterface): Promise<Config> {
  if (configCache) return configCache
  const base = JSON.parse(await $.fs.read(`${$.plugin.root}/router.json`)) as Config
  const home = await $.env.get('HOME')
  const xdg = await $.env.get('XDG_CONFIG_HOME')
  const override = (await $.env.get('JEV_ROUTER_CONFIG')) || `${xdg ?? `${home}/.config`}/jev-router/router.json`
  if (await $.fs.exists(override)) Object.assign(base, JSON.parse(await $.fs.read(override)))
  configCache = base
  return base
}

async function loadKey($: EngineInterface): Promise<string | undefined> {
  const fromEnv = (await $.env.get('TYPESAFE_API_KEY'))?.trim()
  if (fromEnv) return fromEnv
  const home = await $.env.get('HOME')
  const xdg = await $.env.get('XDG_CONFIG_HOME')
  const path = `${xdg ?? `${home}/.config`}/typesafe/env`
  if (!(await $.fs.exists(path))) return undefined
  for (const line of (await $.fs.read(path)).split('\n')) {
    const m = /^\s*(?:export\s+)?TYPESAFE_API_KEY\s*=\s*(.+?)\s*$/.exec(line)
    if (m) return m[1].replace(/^['"]|['"]$/g, '')
  }
  return undefined
}

function redact(text: string): string {
  return text
    .replace(SYSTEM_REMINDER, '')
    .replace(PEM, '[redacted]')
    .replace(SECRET_KV, (_m, k: string, sep: string) => `${k}${sep}[redacted]`)
    .replace(TOKENISH, '[redacted]')
}

function family(modelId: string | undefined): string | undefined {
  const low = (modelId ?? '').toLowerCase()
  return FAMILIES.find(f => low.includes(f))
}

async function askJev(
  $: EngineInterface,
  key: string,
  cfg: Config,
  prompt: string,
  signal: AbortSignal,
): Promise<{ answers: Answers; model?: string }> {
  const body = JSON.stringify({ state: { prompt }, model: cfg.jev_model, questions: QUESTIONS })
  const request = $.http.fetch(API_URL, {
    method: 'POST',
    headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json', 'User-Agent': 'jev-model-router/plugin' },
    body,
  })
  const timeout = $.clock.sleep(cfg.timeout_s * 1000, { signal }).then(() => {
    throw new Error(`timeout after ${cfg.timeout_s}s`)
  })
  const resp = await Promise.race([request, timeout])
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return JSON.parse(resp.text)
}

function argmaxLevel(answer: Answers['level']): { level: Level; confidence: number } {
  const probs = answer?.probabilities ?? {}
  const entries = Object.entries(probs)
  if (entries.length) {
    const [idx, p] = entries.reduce((a, b) => (b[1] > a[1] ? b : a))
    return { level: LEVELS[Number(idx)], confidence: p }
  }
  const idx = Math.max(0, Math.min(LEVELS.length - 1, Math.round(answer?.score ?? 1)))
  return { level: LEVELS[idx], confidence: answer?.confidence ?? 0 }
}

function decide(answers: Answers, cfg: Config, at: number): Decision {
  const { level, confidence } = argmaxLevel(answers.level)
  const followup = answers.followup?.noul ?? 0
  const target = cfg.ladder[level]
  const d: Decision = { level, confidence, followup, model: target.model, effort: target.effort, apply: true, reason: '', at }
  if (followup >= cfg.followup_min) return { ...d, apply: false, reason: 'follow-up to work in progress' }
  if (confidence < cfg.min_confidence) return { ...d, apply: false, reason: `confidence ${confidence.toFixed(2)} under ${cfg.min_confidence}` }
  return d
}

function inputPrice(cfg: Config, fam: string | undefined, contextTokens: number): number {
  const row = cfg.pricing_input_usd_per_mtok[fam ?? ''] ?? { input: Math.max(...Object.values(cfg.pricing_input_usd_per_mtok).map(r => r.input)) }
  return contextTokens > 100_000 && row.input_over_100k !== undefined ? row.input_over_100k : row.input
}

function rewriteCostUsd(cfg: Config, fam: string | undefined, contextTokens: number): number {
  return (contextTokens / 1_000_000) * inputPrice(cfg, fam, contextTokens) * cfg.cache_write_multiplier
}

async function forcedAnswers($: EngineInterface): Promise<Answers | undefined> {
  const forced = await $.env.get('JEV_ROUTER_FORCE')
  if (!forced) return undefined
  const steps = forced.split(',').map(s => s.trim()).filter(Boolean) as Level[]
  const level = steps[Math.min(forcedIndex++, steps.length - 1)]
  const idx = LEVELS.indexOf(level)
  if (idx < 0) return undefined
  return { level: { score: idx, confidence: 1, probabilities: { [String(idx)]: 1 } }, followup: { noul: 0 } }
}

function depth(level: Level): string {
  return {
    trivial: 'Answer directly with the fewest tool calls that give a verified answer. No exploration, no plan, no subagents.',
    routine: 'Work at normal depth. Read what the task needs and no more.',
    hard: 'Think before acting. Verify assumptions against the code and test the result.',
    frontier: 'Treat this as open-ended. Survey the option space, state assumptions, and verify every claim.',
  }[level]
}

async function log($: EngineInterface, row: Record<string, unknown>): Promise<void> {
  try {
    const home = await $.env.get('HOME')
    const dir = (await $.env.get('JEV_ROUTER_STATE')) || `${home}/.claude/cache/jev-router`
    const path = `${dir}/log.jsonl`
    let existing = (await $.fs.exists(path)) ? await $.fs.read(path) : ''
    if (existing.length > LOG_CAP_BYTES) existing = existing.slice(existing.indexOf('\n', existing.length - LOG_CAP_BYTES / 2) + 1)
    await $.fs.write(path, `${existing}${JSON.stringify({ ts: new Date().toISOString(), source: 'plugin', ...row })}\n`)
  } catch {
    return
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.env.set('JEV_ROUTER_PLUGIN', '1')
    $.ui.status(undefined)
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    if (AUTOMATED.test(e.text)) return next(e)
    const cfg = await loadConfig($)
    const clean = redact(e.text).slice(0, cfg.max_prompt_chars).trim()
    if (clean.length < cfg.min_prompt_chars) return next(e)
    const started = await $.clock.now()
    let decision: Decision
    let jevModel: string | undefined
    try {
      const forced = await forcedAnswers($)
      if (forced) {
        jevModel = 'forced'
        decision = decide(forced, cfg, started)
      } else {
        const key = await loadKey($)
        if (!key) throw new Error('no TYPESAFE_API_KEY in the environment or ~/.config/typesafe/env')
        const resp = await askJev($, key, cfg, clean, next.signal)
        jevModel = resp.model
        decision = decide(resp.answers ?? {}, cfg, started)
      }
    } catch (err) {
      await $.state.set(DECISION, null)
      await log($, { event: 'unavailable', error: String(err).slice(0, 120), ms: (await $.clock.now()) - started })
      $.ui.status(undefined)
      return next(e)
    }
    await $.state.set(DECISION, decision)
    await log($, {
      event: 'decision',
      jev_model: jevModel,
      ms: (await $.clock.now()) - started,
      chars: clean.length,
      level: decision.level,
      confidence: Number(decision.confidence.toFixed(3)),
      followup: Number(decision.followup.toFixed(3)),
      model: decision.model,
      effort: decision.effort,
      apply: decision.apply,
    })
    $.ui.status(
      decision.apply
        ? `jev: ${decision.level} (${decision.confidence.toFixed(2)}) routes to ${decision.model}/${decision.effort}`
        : `jev: ${decision.level} (${decision.confidence.toFixed(2)}), kept: ${decision.reason}`,
    )
    const block = `JEV ROUTER: this prompt scored \`${decision.level}\` (p=${decision.confidence.toFixed(2)}), ${decision.apply ? `and this turn runs on ${decision.model}/${decision.effort}` : `setting kept (${decision.reason})`}. ${depth(decision.level)}`
    return next({ ...e, context: [...(e.context ?? []), block] })
  })

  on('turn.step', async function* ($, e, next) {
    if (e.agentId !== undefined) return yield* next(e)
    const { value: decision } = await $.state.get(DECISION)
    if (!decision || !decision.apply) return yield* next(e)
    const cfg = await loadConfig($)
    const contextTokens = (await $.session.usage()).context.tokens ?? 0
    const fromFamily = family(e.model)
    const toFamily = family(decision.model)
    const isDowngrade = inputPrice(cfg, toFamily, contextTokens) < inputPrice(cfg, fromFamily, contextTokens)
    const cost = fromFamily === toFamily ? 0 : rewriteCostUsd(cfg, toFamily, contextTokens)
    const tooExpensive = isDowngrade && cost > cfg.max_downgrade_usd
    const model = fromFamily === toFamily || tooExpensive ? e.model : decision.model
    const effort = e.effort === undefined ? undefined : decision.effort
    if (model === e.model && effort === e.effort) {
      if (e.index === 0 && tooExpensive) {
        $.ui.status(`jev: ${decision.level} wants ${toFamily}/${decision.effort}, kept ${fromFamily}: re-caching ${contextTokens} tokens would cost ${cost.toFixed(2)} USD, cap ${cfg.max_downgrade_usd} USD`)
        await log($, { event: 'kept', reason: 'cost', context_tokens: contextTokens, cost_usd: Number(cost.toFixed(4)) })
      }
      return yield* next(e)
    }
    if (e.index === 0) {
      $.ui.status(`jev: ${decision.level} (${decision.confidence.toFixed(2)}) runs on ${family(model)}/${effort ?? '?'}`)
      await log($, { event: 'applied', from_model: fromFamily, to_model: family(model), from_effort: e.effort, to_effort: effort, context_tokens: contextTokens, recache_usd: Number((model === e.model ? 0 : cost).toFixed(4)) })
    }
    return yield* next(effort === undefined ? { ...e, model } : { ...e, model, effort })
  })
}
