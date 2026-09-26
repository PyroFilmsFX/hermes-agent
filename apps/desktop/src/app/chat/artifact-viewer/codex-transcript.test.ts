import { describe, expect, it } from 'vitest'

import { parseCodexTranscript } from './codex-transcript'

const line = (value: unknown) => JSON.stringify(value)

// Cut from a real codex worker log (w_20260926T202751Z_0e14), paths and text shortened.
const FIXTURE = [
  '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"half a line cut by the tail wi',
  line({ type: 'thread.started', thread_id: '0199' }),
  line({ type: 'turn.started' }),
  line({ type: 'item.completed', item: { id: 'item_1', type: 'agent_message', text: 'Reading the review findings first.' } }),
  line({ type: 'item.started', item: { id: 'item_2', type: 'command_execution', command: "/bin/zsh -lc 'git diff --stat'", aggregated_output: '', exit_code: null, status: 'in_progress' } }),
  line({ type: 'item.completed', item: { id: 'item_2', type: 'command_execution', command: "/bin/zsh -lc 'git diff --stat'", aggregated_output: ' 3 files changed\n', exit_code: 0, status: 'completed' } }),
  line({ type: 'item.started', item: { id: 'item_3', type: 'file_change', changes: [{ path: '/private/tmp/lane-g9/a.ts', kind: 'update' }], status: 'in_progress' } }),
  line({ type: 'item.completed', item: { id: 'item_3', type: 'file_change', changes: [{ path: '/private/tmp/lane-g9/src/a.ts', kind: 'update' }, { path: '/private/tmp/lane-g9/src/b.ts', kind: 'add' }, { path: '/private/tmp/lane-g9/tests/c.py', kind: 'update' }], status: 'completed' } }),
  line({ type: 'item.completed', item: { id: 'item_4', type: 'command_execution', command: 'scripts/run_tests.sh tests/tui_gateway/test_conductor_artifacts.py', aggregated_output: 'FAILED test_x\n' + 'x'.repeat(5000), exit_code: 1, status: 'completed' } }),
  line({ type: 'item.completed', item: { id: 'item_5', type: 'error', message: 'stream disconnected before completion' } }),
  '{"type":"item.completed","item":{"id":"item_6","type":"agent_message","text":"token sk-pro...1Ab1 "broke" the JSON"}}',
  'Reading prompt from stdin...',
  line({ type: 'item.completed', item: { id: 'item_7', type: 'agent_message', text: 'Fixed P0-1: the tail now polls once per 2 s.\n\nTests: 12 passed.' } }),
  line({ type: 'turn.completed', usage: { input_tokens: 1 } }),
  line({ type: 'item.started', item: { id: 'item_8', type: 'command_execution', command: "bash -lc 'npm test'", aggregated_output: '', exit_code: null, status: 'in_progress' } }),
  ''
].join('\n')

describe('parseCodexTranscript', () => {
  const parsed = parseCodexTranscript(FIXTURE, false)

  it('recognises a codex log and drops the partial first line of a tail window', () => {
    expect(parsed.codex).toBe(true)
    expect(parsed.entries[0]).toMatchObject({ kind: 'message', text: 'Reading the review findings first.' })
    expect(JSON.stringify(parsed.entries)).not.toContain('half a line')
  })

  it('keeps agent messages as text, commands without the shell wrapper, and hides lifecycle events', () => {
    const kinds = parsed.entries.map(entry => entry.kind)

    expect(kinds).toEqual(['message', 'command', 'files', 'command', 'error', 'raw', 'raw', 'message', 'running'])
    expect(parsed.entries[1]).toMatchObject({ command: 'git diff --stat', exitCode: 0, kind: 'command' })
  })

  it('reports failing commands with their exit code and the first 2 KB of output', () => {
    const failing = parsed.entries[3]

    expect(failing).toMatchObject({ exitCode: 1, kind: 'command' })
    expect(failing.kind === 'command' && failing.output.startsWith('FAILED test_x')).toBe(true)
    expect(failing.kind === 'command' && failing.output.length).toBeLessThanOrEqual(2048)
  })

  it('reduces file changes to basenames', () => {
    expect(parsed.entries[2]).toEqual({ key: expect.any(String), kind: 'files', paths: ['a.ts', 'b.ts', 'c.py'] })
  })

  it('shows lines whose JSON broke under redaction, and plain lines, as-is', () => {
    expect(parsed.entries[5]).toMatchObject({ kind: 'raw', text: expect.stringContaining('sk-pro...1Ab1') })
    expect(parsed.entries[6]).toMatchObject({ kind: 'raw', text: 'Reading prompt from stdin...' })
  })

  it('surfaces a trailing started command as the one running entry', () => {
    expect(parsed.entries.at(-1)).toMatchObject({ command: 'npm test', kind: 'running' })
  })

  it('pins the last agent message as the final report', () => {
    expect(parsed.finalReport).toBe('Fixed P0-1: the tail now polls once per 2 s.\n\nTests: 12 passed.')
  })

  it('keeps the first line when the window starts at the beginning of the file', () => {
    const whole = parseCodexTranscript([line({ type: 'item.completed', item: { id: 'a', type: 'agent_message', text: 'first' } }), ''].join('\n'), true)

    expect(whole.entries).toEqual([{ key: expect.any(String), kind: 'message', text: 'first' }])
  })

  it('treats a mostly plain log (agy, grok) as plain text', () => {
    const plain = parseCodexTranscript('# Report\n\nAll done.\nChanged files: a.ts\n{"type":"turn.completed"}\n', true)

    expect(plain.codex).toBe(false)
    expect(plain.finalReport).toBeNull()
  })
})
