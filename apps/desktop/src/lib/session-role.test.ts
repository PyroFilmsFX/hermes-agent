import { describe, expect, it } from 'vitest'

import { effectiveSessionRole, isSessionRole, SESSION_ROLES, sessionRole } from './session-role'

describe('sessionRole', () => {
  it.each([
    // Bare manager
    ['manager', 'manager'],
    ['MANAGER', 'manager'],
    ['Manager', 'manager'],
    ['  manager  ', 'manager'],
    ['hermes:manager', 'manager'],
    ['hermes:Manager', 'manager'],
    ['HERMES:MANAGER', 'manager'],
    ['hermes: manager', 'manager'],

    // Trailing -manager
    ['lane-manager', 'manager'],
    ['hermes:lane-manager', 'manager'],
    ['hermes:task-MANAGER', 'manager'],
    ['conductor-lane-manager', 'manager'],

    // Trailing -orchestrator
    ['lane-orchestrator', 'orchestrator'],
    ['hermes:lane-orchestrator', 'orchestrator'],
    ['worker-ORCHESTRATOR', 'orchestrator'],
    ['hermes:worker-orchestrator', 'orchestrator'],

    // Trailing -stream
    ['video-stream', 'stream'],
    ['hermes:video-stream', 'stream'],
    ['lane-STREAM', 'stream'],
    ['hermes:event-stream', 'stream'],

    // Trailing -worker
    ['lane-worker', 'worker'],
    ['hermes:lane-worker', 'worker'],
    ['task-WORKER', 'worker'],
    ['manager-worker', 'worker'], // trailing suffix wins, prefix is ignored

    // Negatives
    [null, null],
    [undefined, null],
    ['', null],
    ['   ', null],
    ['hermes:', null],
    ['general chat', null],
    ['orchestrator', null], // bare orchestrator not accepted (only bare manager)
    ['stream', null],       // bare stream not accepted
    ['hermes:orchestrator', null],
    ['hermes:stream', null],
    ['worker', null], // bare worker not accepted
    ['worker-task', null],
    ['manager-lane', null], // manager in prefix, not trailing
    ['orchestrator-task', null],
    ['stream-view', null],
    ['some-other-role', null]
  ])('sessionRole(%j) -> %j', (title, expected) => {
    expect(sessionRole(title)).toBe(expected)
  })
})

describe('isSessionRole', () => {
  it('accepts exactly the four role types', () => {
    expect([...SESSION_ROLES]).toEqual(['manager', 'orchestrator', 'worker', 'stream'])

    for (const role of SESSION_ROLES) {
      expect(isSessionRole(role)).toBe(true)
    }

    expect(isSessionRole('admin')).toBe(false)
    expect(isSessionRole('')).toBe(false)
    expect(isSessionRole(null)).toBe(false)
    expect(isSessionRole(undefined)).toBe(false)
    expect(isSessionRole('Manager')).toBe(false)
  })
})

describe('effectiveSessionRole (explicit > name > none)', () => {
  it('uses the explicit role over the name-derived one', () => {
    expect(effectiveSessionRole({ session_role: 'worker', title: 'lane-manager' })).toBe('worker')
    expect(effectiveSessionRole({ session_role: 'stream', title: 'manager' })).toBe('stream')
  })

  it('uses the explicit role when the name implies none', () => {
    expect(effectiveSessionRole({ session_role: 'orchestrator', title: 'general chat' })).toBe('orchestrator')
    expect(effectiveSessionRole({ session_role: 'manager', title: null })).toBe('manager')
  })

  it('falls back to the name when no explicit role is set (Auto)', () => {
    expect(effectiveSessionRole({ session_role: null, title: 'lane-worker' })).toBe('worker')
    expect(effectiveSessionRole({ session_role: undefined, title: 'hermes:lane-orchestrator' })).toBe('orchestrator')
    expect(effectiveSessionRole({ title: 'manager' })).toBe('manager')
  })

  it('ignores an unknown explicit value and falls back to the name', () => {
    expect(effectiveSessionRole({ session_role: 'admin', title: 'video-stream' })).toBe('stream')
    expect(effectiveSessionRole({ session_role: '', title: 'lane-manager' })).toBe('manager')
  })

  it('is null when neither source names a role', () => {
    expect(effectiveSessionRole({ session_role: null, title: 'general chat' })).toBeNull()
    expect(effectiveSessionRole({})).toBeNull()
  })
})
