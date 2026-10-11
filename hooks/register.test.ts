import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { Decision } from '../types'
import { decide, pickModel, type Config } from './register'

const cfg: Config = {
  ladder: {
    trivial: { effort: 'low' },
    routine: { effort: 'medium' },
    hard: { min_model: 'claude-opus-5-5', effort: 'high' },
    frontier: { min_model: 'claude-opus-5-5', effort: 'high' },
  },
  min_confidence: 0.55,
  followup_min: 0.7,
  min_prompt_chars: 4,
  max_prompt_chars: 4000,
  timeout_s: 3.5,
  jev_model: 'jev-latest',
  pricing_usd_per_mtok: {
    haiku: { input: 0.1, input_over_100k: 0.5, cache_read: 0.01, cache_read_over_100k: 0.05 },
    sonnet: { input: 2.0, cache_read: 0.2 },
    opus: { input: 4.0, cache_read: 0.2 },
    fable: { input: 10.0, cache_read: 0.25 },
  },
  cache_write_multiplier: 1.25,
  sample_rate: 0,
  switch_session_model: true,
}

function decision(over: Partial<Decision>): Decision {
  return { level: 'routine', confidence: 0.9, followup: 0, effort: 'medium', apply: true, reason: '', at: 0, ...over }
}

describe('pickModel', () => {
  test('keeps the session model when the rung names none', () => {
    const pick = pickModel(cfg, decision({}), 'claude-sonnet-5-5', 40_000)
    expect(pick.kind).toBe('keep')
    expect(pick.model).toBe('claude-sonnet-5-5')
  })

  test('upgrades when min_model outranks the session model', () => {
    const pick = pickModel(cfg, decision({ minModel: 'claude-opus-5-5', effort: 'high' }), 'claude-sonnet-5-5', 40_000)
    expect(pick.kind).toBe('upgrade')
    expect(pick.model).toBe('claude-opus-5-5')
  })

  test('refuses a downgrade when writing the cache costs at least as much as reading it', () => {
    const pick = pickModel(cfg, decision({ model: 'claude-haiku-5-5', effort: 'low' }), 'claude-opus-5-5', 200_000)
    expect(pick.writeUsd).toBeGreaterThanOrEqual(pick.readUsd)
    expect(pick.kind).toBe('refused')
    expect(pick.model).toBe('claude-opus-5-5')
  })

  test('allows a downgrade when writing the cache costs less than reading it', () => {
    const pick = pickModel(cfg, decision({ model: 'claude-haiku-5-5', effort: 'low' }), 'claude-opus-5-5', 50_000)
    expect(pick.writeUsd).toBeLessThan(pick.readUsd)
    expect(pick.kind).toBe('downgrade')
    expect(pick.model).toBe('claude-haiku-5-5')
  })
})

describe('decide', () => {
  test('does not apply a follow-up at or over followup_min', () => {
    const d = decide({ level: { probabilities: { '2': 0.9 } }, followup: { noul: 0.7 } }, cfg, 1)
    expect(d.level).toBe('hard')
    expect(d.apply).toBe(false)
    expect(d.reason).toBe('follow-up to work in progress')
  })

  test('does not apply a level under min_confidence', () => {
    const d = decide({ level: { probabilities: { '0': 0.5, '1': 0.3 } }, followup: { noul: 0 } }, cfg, 1)
    expect(d.level).toBe('trivial')
    expect(d.apply).toBe(false)
    expect(d.reason).toMatch(/^confidence 0\.50 under 0\.55/)
  })

  test('applies a confident self-contained prompt', () => {
    const d = decide({ level: { probabilities: { '2': 0.8 } }, followup: { noul: 0.1 } }, cfg, 1)
    expect(d.apply).toBe(true)
    expect(d.minModel).toBe('claude-opus-5-5')
    expect(d.effort).toBe('high')
  })
})

type World = { fetches: number; writes: string[]; configSets: { key: string; value: unknown }[]; states: unknown[]; steps: Record<string, unknown>[] }

function host(on: On, sessionModel: string, contextTokens: number): World {
  const world: World = { fetches: 0, writes: [], configSets: [], states: [], steps: [] }
  mock.clock(on, { now: 1_000 })
  on('fs.read', async (_$, e) => {
    if (e.path.endsWith('/router.json')) return { value: JSON.stringify(cfg) }
    return { deny: `unexpected read ${e.path}` }
  })
  on('fs.exists', async () => ({ value: false }))
  on('fs.write', async (_$, e) => {
    world.writes.push(e.path)
    return { value: undefined }
  })
  on('http.fetch', async () => {
    world.fetches += 1
    return { deny: 'the network is off in this test' }
  })
  on('ui.status', async () => ({ value: undefined }))
  on('session.model', async () => ({ value: sessionModel }))
  on('session.usage', async () => ({ value: { startedAt: 0, context: { tokens: contextTokens, window: 200_000 }, rateLimits: [] } }))
  on('config.list', async () => ({ value: [] }))
  on('config.set', async (_$, e) => {
    world.configSets.push({ key: e.key, value: e.value })
    return { value: e.value }
  })
  on('prompt.submit', async (_$, e) => ({ text: e.text, context: e.context }))
  on('state.set', async (_$, e, next) => {
    world.states.push(e.value)
    return next(e)
  })
  on('turn.step', async function* (_$, e) {
    world.steps.push(e as Record<string, unknown>)
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'end_turn' as const, usage: null }
  })
  return world
}

async function step($: Parameters<Parameters<typeof test>[1]>[0], e: Record<string, unknown>): Promise<void> {
  for await (const _chunk of $.turn.step(e as Parameters<typeof $.turn.step>[0])) {
  }
}

describe('prompt.submit', () => {
  test('a forced level sets state and adds a context entry without the network', async ($, on) => {
    mock.env(on, { HOME: '/home/tester', JEV_ROUTER_FORCE: 'trivial' })
    const world = host(on, 'claude-opus-5-5', 20_000)
    const result = await $.prompt.submit({ text: 'rename this variable', wait: false, origin: { kind: 'composer' } })
    expect(result.drop).toBeUndefined()
    expect(result.context).toHaveLength(1)
    expect(result.context?.[0]).toStartWith('JEV ROUTER: this prompt scored `trivial`')
    expect(result.context?.[0]).toContain('effort low')
    expect(world.fetches).toBe(0)
    expect(world.states).toHaveLength(1)
    expect(world.states[0]).toMatchObject({ level: 'trivial', effort: 'low', apply: true, confidence: 1 })
  })
})

describe('turn.step', () => {
  test('rewrites model and effort for the main loop', async ($, on) => {
    mock.env(on, { HOME: '/home/tester', JEV_ROUTER_FORCE: 'hard' })
    const world = host(on, 'claude-sonnet-5-5', 20_000)
    await $.prompt.submit({ text: 'find the race in the scheduler', wait: false, origin: { kind: 'composer' } })
    expect(world.configSets).toEqual([{ key: 'model', value: 'opus' }])
    await step($, { turnId: 't1', index: 0, model: 'claude-sonnet-5-5', effort: 'medium', messageCount: 2 })
    expect(world.steps).toHaveLength(1)
    expect(world.steps[0].model).toBe('claude-opus-5-5')
    expect(world.steps[0].effort).toBe('high')
  })

  test('leaves a subagent request untouched', async ($, on) => {
    mock.env(on, { HOME: '/home/tester', JEV_ROUTER_FORCE: 'hard' })
    const world = host(on, 'claude-sonnet-5-5', 20_000)
    await $.prompt.submit({ text: 'find the race in the scheduler', wait: false, origin: { kind: 'composer' } })
    await step($, { turnId: 't1', index: 0, model: 'claude-sonnet-5-5', effort: 'medium', messageCount: 2, agentId: 'agent-1' })
    expect(world.steps).toHaveLength(1)
    expect(world.steps[0].model).toBe('claude-sonnet-5-5')
    expect(world.steps[0].effort).toBe('medium')
    expect(world.steps[0].agentId).toBe('agent-1')
  })
})
