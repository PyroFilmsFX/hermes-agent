import { describe, expect, it } from 'vitest'

import { sessionRole } from './session-role'

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
    ['manager-worker', null], // manager in prefix/middle, not trailing
    ['orchestrator-task', null],
    ['stream-view', null],
    ['some-other-role', null]
  ])('sessionRole(%j) -> %j', (title, expected) => {
    expect(sessionRole(title)).toBe(expected)
  })
})
