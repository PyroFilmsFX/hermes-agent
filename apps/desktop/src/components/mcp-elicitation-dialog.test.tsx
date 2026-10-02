import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { $gateway } from '@/store/gateway'
import {
  $mcpElicitations,
  MCP_ELICITATION_TIMEOUT_MS,
  normalizeMcpElicitation,
  receiveMcpElicitation,
  resetMcpElicitationsForTests
} from '@/store/mcp-elicitation'
import { notify } from '@/store/notifications'
import { $activeSessionId } from '@/store/session'

import { McpElicitationHost, parseElicitationUrl } from './mcp-elicitation-dialog'

vi.mock('@/lib/haptics', () => ({ triggerHaptic: vi.fn() }))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))

const FORM_SCHEMA = {
  type: 'object',
  properties: {
    name: { type: 'string', title: 'Name', description: 'As it appears on the invoice', minLength: 2 },
    email: { type: 'string', title: 'Email', format: 'email' },
    age: { type: 'integer', title: 'Age', minimum: 0 },
    ratio: { type: 'number', title: 'Ratio' },
    subscribe: { type: 'boolean', title: 'Subscribe', default: true },
    color: { type: 'string', title: 'Color', enum: ['r', 'g'], enumNames: ['Red', 'Green'] }
  },
  required: ['name', 'age', 'color']
}

let request: ReturnType<typeof vi.fn>

function push(payload: Record<string, unknown>, sessionId: null | string = null) {
  const normalized = normalizeMcpElicitation(payload, { sessionId })

  if (!normalized) {
    throw new Error('payload rejected')
  }

  act(() => receiveMcpElicitation(normalized))
}

function form(id = 'el-1', schema: Record<string, unknown> = FORM_SCHEMA) {
  return {
    request_id: id,
    server: 'billing',
    message: 'Who should the invoice go to?',
    mode: 'form',
    requestedSchema: schema
  }
}

function renderHost() {
  return render(
    <I18nProvider configClient={null}>
      <McpElicitationHost />
    </I18nProvider>
  )
}

const respondCalls = () => request.mock.calls.filter(([method]) => method === 'mcp.elicitation.respond')

beforeEach(() => {
  request = vi.fn().mockResolvedValue({ ok: true })
  $gateway.set({ request } as never)
})

afterEach(() => {
  cleanup()
  resetMcpElicitationsForTests()
  $activeSessionId.set(null)
  $gateway.set(null)
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('MCP elicitation form', () => {
  it('renders every field type with its label, description and required marker', () => {
    push(form())
    renderHost()

    expect(screen.getByRole('dialog').textContent).toContain('billing needs a few details')
    expect(screen.getByText('Who should the invoice go to?')).toBeTruthy()

    const name = screen.getByRole('textbox', { name: /^Name/ })
    expect(name.getAttribute('aria-required')).toBe('true')
    expect(name.getAttribute('aria-describedby')).toBeTruthy()
    expect(screen.getByText('As it appears on the invoice')).toBeTruthy()

    expect(screen.getByRole('textbox', { name: /^Email/ }).getAttribute('type')).toBe('email')
    expect(screen.getByRole('textbox', { name: /^Email/ }).getAttribute('aria-required')).toBeNull()
    expect(screen.getByRole('textbox', { name: /^Age/ }).getAttribute('inputmode')).toBe('numeric')
    expect(screen.getByRole('textbox', { name: /^Ratio/ }).getAttribute('inputmode')).toBe('decimal')
    expect(screen.getByRole('checkbox', { name: /^Subscribe/ }).getAttribute('data-state')).toBe('checked')
    expect(screen.getByRole('group', { name: /^Color/ })).toBeTruthy()
    expect(screen.getByRole('radio', { name: 'Red' })).toBeTruthy()
    expect(screen.getByRole('radio', { name: 'Green' })).toBeTruthy()
  })

  it('submits typed content: numbers as numbers, booleans as booleans, enum values not labels', async () => {
    push(form())
    renderHost()

    fireEvent.change(screen.getByRole('textbox', { name: /^Name/ }), { target: { value: 'Ada' } })
    fireEvent.change(screen.getByRole('textbox', { name: /^Age/ }), { target: { value: '36' } })
    fireEvent.change(screen.getByRole('textbox', { name: /^Ratio/ }), { target: { value: '0.75' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /^Subscribe/ }))
    fireEvent.click(screen.getByRole('radio', { name: 'Green' }))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(respondCalls()).toEqual([
      [
        'mcp.elicitation.respond',
        {
          request_id: 'el-1',
          action: 'accept',
          content: { name: 'Ada', age: 36, ratio: 0.75, subscribe: false, color: 'g' }
        }
      ]
    ])
    expect($mcpElicitations.get()).toEqual([])
  })

  it('enforces required fields and client-side constraints before anything is sent', () => {
    push(form())
    renderHost()

    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))

    expect(respondCalls()).toEqual([])
    expect(screen.getAllByText('Fill in this field.')).toHaveLength(2)
    expect(screen.getByText('Choose one of the options.')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: /^Name/ }).getAttribute('aria-invalid')).toBe('true')
    expect(window.document.activeElement).toBe(screen.getByRole('textbox', { name: /^Name/ }))

    fireEvent.change(screen.getByRole('textbox', { name: /^Name/ }), { target: { value: 'A' } })
    fireEvent.change(screen.getByRole('textbox', { name: /^Email/ }), { target: { value: 'not-an-email' } })
    fireEvent.change(screen.getByRole('textbox', { name: /^Age/ }), { target: { value: '2.5' } })

    expect(screen.getByText('Use at least 2 characters.')).toBeTruthy()
    expect(screen.getByText('Enter an email address.')).toBeTruthy()
    expect(screen.getByText('Enter a whole number.')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    expect(respondCalls()).toEqual([])
    expect(screen.getByRole('dialog')).toBeTruthy()
  })

  it('Decline and Cancel answer without content', async () => {
    push(form('el-a'))
    push(form('el-b'))
    renderHost()

    fireEvent.click(screen.getByRole('button', { name: 'Decline' }))
    await waitFor(() => expect(respondCalls()).toHaveLength(1))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    expect(respondCalls()).toEqual([
      ['mcp.elicitation.respond', { request_id: 'el-a', action: 'decline' }],
      ['mcp.elicitation.respond', { request_id: 'el-b', action: 'cancel' }]
    ])
  })

  it('Escape cancels a form, and an outside click does not answer anything', async () => {
    push(form())
    renderHost()

    fireEvent.pointerDown(window.document.body)
    expect(respondCalls()).toEqual([])
    expect(screen.getByRole('dialog')).toBeTruthy()

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(respondCalls()).toEqual([['mcp.elicitation.respond', { request_id: 'el-1', action: 'cancel' }]])
  })

  it('a required field the form cannot render blocks Submit (decline or cancel only)', () => {
    push(form('el-x', { properties: { tags: { type: 'array', title: 'Tags' } }, required: ['tags'] }))
    renderHost()

    expect((screen.getByRole('button', { name: 'Submit' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/cannot fill in this required field/)).toBeTruthy()
  })

  it('drops a request the backend no longer holds instead of retrying it', async () => {
    request.mockRejectedValueOnce(
      Object.assign(new Error('Unknown or expired elicitation request: el-1'), { code: 4018 })
    )
    push(form())
    renderHost()

    fireEvent.click(screen.getByRole('button', { name: 'Decline' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'billing is no longer waiting for this answer.' })
    )
  })

  it('keeps the card and shows the error when sending fails for another reason', async () => {
    request.mockRejectedValueOnce(new Error('socket closed'))
    push(form())
    renderHost()

    fireEvent.click(screen.getByRole('button', { name: 'Decline' }))

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('socket closed'))
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Decline' }) as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('MCP elicitation URL consent', () => {
  const url = (id = 'u-1', href = 'https://auth.example.com/connect?state=abc') => ({
    request_id: id,
    server: 'github',
    message: 'Sign in to link your account.',
    mode: 'url',
    url: href
  })

  it('shows the full URL with the host emphasised and opens only on the explicit click, once', async () => {
    const openExternal = vi.fn().mockResolvedValue(undefined)

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { openExternal }

    push(url())
    renderHost()

    const box = screen.getByTestId('mcp-elicitation-url')
    expect(box.textContent).toBe('https://auth.example.com/connect?state=abc')
    expect(box.querySelector('strong')?.textContent).toBe('auth.example.com')
    expect(screen.getByRole('dialog').textContent).toContain('github wants to open a link')
    expect(openExternal).not.toHaveBeenCalled()
    expect(respondCalls()).toEqual([])

    const open = screen.getByRole('button', { name: 'Open in browser' })
    fireEvent.click(open)
    fireEvent.click(open)

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(openExternal).toHaveBeenCalledTimes(1)
    expect(openExternal).toHaveBeenCalledWith('https://auth.example.com/connect?state=abc')
    expect(respondCalls()).toEqual([['mcp.elicitation.respond', { request_id: 'u-1', action: 'accept' }]])
  })

  it('Decline and Escape decline without opening anything', async () => {
    const openExternal = vi.fn().mockResolvedValue(undefined)

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { openExternal }

    push(url('u-1'))
    push(url('u-2'))
    renderHost()

    fireEvent.click(screen.getByRole('button', { name: 'Decline' }))
    await waitFor(() => expect(respondCalls()).toHaveLength(1))
    await waitFor(() => expect($mcpElicitations.get().map(entry => entry.requestId)).toEqual(['u-2']))

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    expect(openExternal).not.toHaveBeenCalled()
    expect(respondCalls()).toEqual([
      ['mcp.elicitation.respond', { request_id: 'u-1', action: 'decline' }],
      ['mcp.elicitation.respond', { request_id: 'u-2', action: 'decline' }]
    ])
  })

  it('does not accept when the browser could not be opened', async () => {
    const openExternal = vi.fn().mockRejectedValue(new Error('no handler'))

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { openExternal }

    push(url())
    renderHost()

    fireEvent.click(screen.getByRole('button', { name: 'Open in browser' }))

    await waitFor(() => expect(screen.getByText(/Could not open your browser/)).toBeTruthy())
    expect(respondCalls()).toEqual([])
    expect(screen.getByRole('dialog')).toBeTruthy()
  })

  it('refuses non-web schemes: nothing to open, decline only', () => {
    const openExternal = vi.fn()

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { openExternal }

    push(url('u-f', 'file:///etc/passwd'))
    renderHost()

    const open = screen.getByRole('button', { name: 'Open in browser' }) as HTMLButtonElement
    expect(open.disabled).toBe(true)
    fireEvent.click(open)
    expect(openExternal).not.toHaveBeenCalled()
    expect(screen.getByText(/only opens web links/)).toBeTruthy()
    expect(parseElicitationUrl('javascript:alert(1)')).toBeNull()
    expect(parseElicitationUrl('https://a.example@evil.example/x')?.host).toBe('evil.example')
  })

  it('flags plain http', () => {
    push(url('u-h', 'http://intranet.example/login'))
    renderHost()

    expect(screen.getByText('This link is not encrypted (http).')).toBeTruthy()
  })
})

describe('MCP elicitation queue', () => {
  it('shows one request at a time, oldest first, with the rest counted', async () => {
    push(form('el-1'))
    push({ ...form('el-2'), server: 'crm' })
    push(form('el-3'))
    renderHost()

    expect(screen.getAllByRole('dialog')).toHaveLength(1)
    expect(screen.getByRole('dialog').textContent).toContain('billing needs a few details')
    expect(screen.getByText('2 more requests waiting')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Decline' }))

    await waitFor(() => expect(screen.getByRole('dialog').textContent).toContain('crm needs a few details'))
    expect(screen.getAllByRole('dialog')).toHaveLength(1)
    expect(screen.getByText('1 more request waiting')).toBeTruthy()
  })

  it('ignores a duplicate delivery of the same request id', () => {
    push(form('el-1'))
    push(form('el-1'))

    expect($mcpElicitations.get()).toHaveLength(1)
  })

  it('keeps a request for a chat that is not visible queued behind a badge', () => {
    $activeSessionId.set('chat-a')
    push(form('el-b'), 'chat-b')
    renderHost()

    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('status').textContent).toContain('An MCP server is waiting for input in another chat')

    fireEvent.click(screen.getByRole('button', { name: 'Show' }))
    expect(screen.getByRole('dialog')).toBeTruthy()
  })

  it('shows it once its chat becomes visible', () => {
    $activeSessionId.set('chat-a')
    push(form('el-b'), 'chat-b')
    renderHost()

    expect(screen.queryByRole('dialog')).toBeNull()
    act(() => $activeSessionId.set('chat-b'))
    expect(screen.getByRole('dialog')).toBeTruthy()
  })

  it('drops the card at the backend timeout without answering, so it can never auto-accept', () => {
    vi.useFakeTimers()

    const openExternal = vi.fn()

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { openExternal }

    push(form('el-1', { properties: { ok: { type: 'boolean', default: true } } }))
    push({ request_id: 'u-1', server: 'github', message: '', mode: 'url', url: 'https://example.com' })
    renderHost()

    act(() => vi.advanceTimersByTime(MCP_ELICITATION_TIMEOUT_MS - 1))
    expect(screen.getByRole('dialog')).toBeTruthy()

    act(() => vi.advanceTimersByTime(1))

    expect(screen.queryByRole('dialog')).toBeNull()
    expect($mcpElicitations.get()).toEqual([])
    expect(request).not.toHaveBeenCalled()
    expect(openExternal).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'The request from billing timed out and was declined.' })
    )
  })
})

describe('normalizeMcpElicitation', () => {
  it('rejects malformed payloads', () => {
    expect(normalizeMcpElicitation(null)).toBeNull()
    expect(normalizeMcpElicitation({ server: 'x', mode: 'form' })).toBeNull()
    expect(normalizeMcpElicitation({ request_id: 'a', mode: 'url' })).toBeNull()
    expect(normalizeMcpElicitation({ request_id: 'a', mode: 'telepathy' })).toBeNull()
    expect(normalizeMcpElicitation({ request_id: 'a', server: 's', message: 'm' })).toMatchObject({
      mode: 'form',
      requestedSchema: {},
      sessionId: null
    })
  })
})
