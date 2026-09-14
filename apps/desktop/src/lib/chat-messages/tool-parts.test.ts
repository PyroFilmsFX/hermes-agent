import { describe, expect, it } from 'vitest'

import { toolResult, upsertToolPart } from './tool-parts'
import type { ChatMessagePart } from './types'

describe('toolResult and upsertToolPart (U3.4)', () => {
  it('preserves plain-text SDK tool result as output and renders it', () => {
    const stdout = 'total 4\n-rw-r--r--  1 user  staff  12 Sep 13 21:00 hello.txt'
    const parts = upsertToolPart([], { name: 'terminal', result: stdout }, 'complete')

    const part = parts[0] as Extract<ChatMessagePart, { type: 'tool-call' }>
    expect(part).toBeDefined()
    expect(part.result).toEqual({ output: stdout })
    expect(part.isError).toBe(false)
  })

  it('preserves array result on the output key', () => {
    const arrayResult = ['item-alpha', 'item-beta', 'item-gamma']
    const parts = upsertToolPart([], { name: 'list_items', result: arrayResult }, 'complete')

    const part = parts[0] as Extract<ChatMessagePart, { type: 'tool-call' }>
    expect(part).toBeDefined()
    expect(part.result).toEqual({ output: arrayResult })
  })

  it('marks isError true and surfaces message when is_error:true without error field', () => {
    const errorMessage = 'bash: command not found: unknown-cmd'

    const parts = upsertToolPart(
      [],
      { is_error: true, name: 'terminal', result: errorMessage } as never,
      'complete'
    )

    const part = parts[0] as Extract<ChatMessagePart, { type: 'tool-call' }>
    expect(part).toBeDefined()
    expect(part.isError).toBe(true)
    expect((part.result as { error?: string })?.error).toBe(errorMessage)
    expect((part.result as { output?: string })?.output).toBe(errorMessage)
  })

  it('marks isError true when is_error:true and error is empty string', () => {
    const errorMessage = 'fatal: destination path already exists'

    const parts = upsertToolPart(
      [],
      { error: '', is_error: true, name: 'terminal', result: errorMessage } as never,
      'complete'
    )

    const part = parts[0] as Extract<ChatMessagePart, { type: 'tool-call' }>
    expect(part).toBeDefined()
    expect(part.isError).toBe(true)
    expect((part.result as { error?: string })?.error).toBe(errorMessage)
  })

  it('preserves truncated metadata on result record', () => {
    const parts = upsertToolPart(
      [],
      {
        name: 'read_file',
        result: 'partial content',
        truncated: { shown: 50, total: 200 }
      } as never,
      'complete'
    )

    const part = parts[0] as Extract<ChatMessagePart, { type: 'tool-call' }>
    expect(part).toBeDefined()
    expect((part.result as { truncated?: { shown: number; total: number } })?.truncated).toEqual({
      shown: 50,
      total: 200
    })
  })

  it('carries tool_use_result metadata into result record for expanded view only', () => {
    const sdkMeta = { id: 'call_123', internal_status: 'ok', tokens: 42 }

    const parts = upsertToolPart(
      [],
      {
        name: 'execute_code',
        result: 'done',
        tool_use_result: sdkMeta
      } as never,
      'complete'
    )

    const part = parts[0] as Extract<ChatMessagePart, { type: 'tool-call' }>
    expect(part).toBeDefined()
    expect((part.result as { tool_use_result?: typeof sdkMeta })?.tool_use_result).toEqual(sdkMeta)
  })

  it('keeps JSON object result unchanged as regression guard', () => {
    const jsonResult = { exit_code: 0, output: 'success', summary: 'ok' }
    const parts = upsertToolPart([], { name: 'terminal', result: jsonResult }, 'complete')

    const part = parts[0] as Extract<ChatMessagePart, { type: 'tool-call' }>
    expect(part).toBeDefined()
    expect(part.result).toEqual(jsonResult)
    expect(part.isError).toBe(false)
  })

  it('parses valid JSON string representing an object without collapsing to empty', () => {
    const rawJsonString = JSON.stringify({ line_count: 42, path: '/test/foo.ts' })
    const res = toolResult({ name: 'stat', result: rawJsonString })

    expect(res).toEqual({ line_count: 42, path: '/test/foo.ts' })
  })
})
