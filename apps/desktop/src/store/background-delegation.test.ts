import { beforeEach, describe, expect, it } from 'vitest'

import { $backgroundResumePendingCount } from './background-delegation'
import { $activeSessionId, $busy } from './session'
import { $subagentsBySession, type SubagentProgress } from './subagents'

const sub = (over: Partial<SubagentProgress> = {}): SubagentProgress => ({
  id: over.id ?? 'deleg:1',
  parentId: null,
  goal: 'do the thing',
  status: 'running',
  taskCount: 1,
  taskIndex: 0,
  startedAt: 0,
  updatedAt: 0,
  filesRead: [],
  filesWritten: [],
  stream: [],
  ...over
})

describe('$backgroundResumePendingCount', () => {
  beforeEach(() => {
    $busy.set(false)
    $activeSessionId.set('s1')
    $subagentsBySession.set({})
  })

  it('counts running/queued subagents for the active session while idle', () => {
    $subagentsBySession.set({ s1: [sub({ id: 'a' }), sub({ id: 'b', status: 'queued' })] })
    expect($backgroundResumePendingCount.get()).toBe(2)
  })

  it('is zero while a turn is busy (the turn owns the main loader)', () => {
    $subagentsBySession.set({ s1: [sub({ id: 'a' })] })
    $busy.set(true)
    expect($backgroundResumePendingCount.get()).toBe(0)
  })

  it('ignores terminal subagents and other sessions', () => {
    $subagentsBySession.set({
      s1: [sub({ id: 'a', status: 'completed' }), sub({ id: 'b', status: 'failed' })],
      s2: [sub({ id: 'c' })]
    })
    expect($backgroundResumePendingCount.get()).toBe(0)
  })

  it('is zero when there is no active session', () => {
    $subagentsBySession.set({ s1: [sub({ id: 'a' })] })
    $activeSessionId.set(null)
    expect($backgroundResumePendingCount.get()).toBe(0)
  })
})
