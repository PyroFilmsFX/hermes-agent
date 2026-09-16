import { describe, expect, it } from 'vitest'

import { envelopeErrorText, toolResultRecord } from '@/lib/tool-result-metadata'

import { upsertToolPart } from './tool-parts'
import type { ChatMessagePart } from './types'

type ToolPart = Extract<ChatMessagePart, { type: 'tool-call' }>

const complete = (payload: Record<string, unknown>) =>
  upsertToolPart([], payload as never, 'complete')[0] as ToolPart

// SDK-lane tool cards (W3) on upstream's result model: the raw result is kept verbatim on
// the part; gateway hints (error flag, truncation, SDK metadata) live in toolResultMetadata.
describe('upsertToolPart — SDK tool-card fidelity', () => {
  it('keeps a plain-text result verbatim', () => {
    const stdout = 'total 4\n-rw-r--r--  1 user  staff  12 Sep 13 21:00 hello.txt'
    const part = complete({ name: 'terminal', result: stdout })

    expect(part.result).toBe(stdout)
    expect(part.isError).toBe(false)
  })

  it('keeps an array result verbatim', () => {
    const items = ['item-alpha', 'item-beta']

    expect(complete({ name: 'list_items', result: items }).result).toEqual(items)
  })

  it('marks the card failed on is_error even without error text', () => {
    const message = 'bash: command not found: unknown-cmd'
    const part = complete({ is_error: true, name: 'terminal', result: message })

    expect(part.isError).toBe(true)
    expect(part.result).toBe(message)
  })

  it('uses the error text when the gateway sends one', () => {
    const part = complete({ error: 'fatal: exists', is_error: true, name: 'terminal', result: 'fatal: exists' })

    expect(part.isError).toBe(true)
    expect(envelopeErrorText(part.toolResultMetadata)).toBe('fatal: exists')
  })

  it('carries truncation and SDK metadata as display hints, not as the result', () => {
    const meta = { duration_ms: 42 }

    const part = complete({
      name: 'read_file',
      result: 'partial content',
      tool_use_result: meta,
      truncated: { shown: 50, total: 200 }
    })

    expect(part.result).toBe('partial content')
    expect(part.toolResultMetadata?.truncated).toEqual({ shown: 50, total: 200 })
    expect(part.toolResultMetadata?.tool_use_result).toEqual(meta)
    expect(toolResultRecord(part).truncated).toEqual({ shown: 50, total: 200 })
  })

  it('keeps a JSON object result unchanged', () => {
    const json = { exit_code: 0, output: 'success', summary: 'ok' }
    const part = complete({ name: 'terminal', result: json })

    expect(part.result).toEqual(json)
    expect(part.isError).toBe(false)
  })
})
