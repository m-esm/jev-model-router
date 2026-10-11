export type Level = 'trivial' | 'routine' | 'hard' | 'frontier'
export type Effort = 'low' | 'medium' | 'high' | 'xhigh' | 'max'
export type Decision = {
  level: Level
  confidence: number
  followup: number
  model?: string
  minModel?: string
  effort: Effort
  apply: boolean
  reason: string
  at: number
}

declare module 'claude-code' {
  interface PluginState {
    'jev-model-router': { decision: Decision | null }
  }
}
