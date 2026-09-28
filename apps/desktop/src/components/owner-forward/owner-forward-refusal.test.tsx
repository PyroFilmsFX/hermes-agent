/**
 * Live RCA 2026-09-28 (#60): a forward main refused before its native confirm left no trace. The sheet
 * must show the refusal as a visible alert with main's code, and the renderer logs the code (never the
 * text) so desktop console captures it.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $forwardSheet, sendOwnerForward, setForwardGatewayRequestForTests } from '@/lib/owner-forward/client'
import { $activeGatewayProfile } from '@/store/profile'

import { ForwardSheet } from './forward-sheet'

const trust = vi.hoisted(() => ({ on: true }))

vi.mock('@/lib/owner-forward/trusted', () => ({
  isTrustedGesture: (event: { isTrusted?: boolean } | null | undefined) => trust.on || event?.isTrusted === true
}))

const TEXT = 'approve: merge w6/wd-ci-hold (both commits)'
const origin = { session_id: '20260909_193713_ce3d96', message_id: null, role: 'assistant' as const }
const target = { profile: 'thinkbot', session_id: '20260924_200237_d5276f', title: 'cntrl-core-worker' }
const confirm = vi.fn()
const gatewayRequest = vi.fn()
let warn: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  confirm.mockReset()
  gatewayRequest.mockReset()
  gatewayRequest.mockImplementation(async (method: string, params: { text?: string }) =>
    method === 'secrets.mask' ? { text: params.text } : { results: [] }
  )
  ;(window as any).hermesDesktop = { ownerForward: { confirm } }
  setForwardGatewayRequestForTests(gatewayRequest)
  $activeGatewayProfile.set('thinkbot')
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  cleanup()
  warn.mockRestore()
  setForwardGatewayRequestForTests(null)
  $forwardSheet.set(null)
  delete (window as any).hermesDesktop
})

describe('a refused forward is visible and logged', () => {
  it('sendOwnerForward returns main’s code and logs it without the text', async () => {
    confirm.mockResolvedValue({ ok: false, code: 'no_backend', error: 'the thinkbot backend was not started by the app' })

    const outcome = await sendOwnerForward({ text: TEXT, gesture: 'selection', origin, targets: [target], scope: [] })

    expect(outcome).toEqual({
      kind: 'error',
      code: 'no_backend',
      message: 'the thinkbot backend was not started by the app'
    })
    expect(confirm.mock.calls[0][0]).toMatchObject({ profile: 'thinkbot', targets: [{ profile: 'thinkbot', session_id: target.session_id }] })
    expect(warn).toHaveBeenCalledWith('[owner-forward] confirm failed: no_backend')
    expect(warn.mock.calls.flat().join('\n')).not.toContain('approve')
    expect(gatewayRequest).not.toHaveBeenCalledWith('owner.forward', expect.anything(), expect.anything())
  })

  it('a delivery error from owner.forward carries the gateway code', async () => {
    confirm.mockResolvedValue({
      ok: true,
      decisionId: 'od',
      grantId: 'og',
      envelope: { format: 'hermes-owner-grant/v1', kid: 'k', payload: 'p', sig: 's' },
      targets: [`thinkbot:${target.session_id}`]
    })
    gatewayRequest.mockImplementation(async (method: string, params: { text?: string }) => {
      if (method === 'secrets.mask') {
        return { text: params.text }
      }

      throw Object.assign(new Error('owner grant refused (backend); send again'), { code: 4127 })
    })

    const outcome = await sendOwnerForward({ text: TEXT, gesture: 'selection', origin, targets: [target], scope: [] })

    expect(outcome).toMatchObject({ kind: 'error', code: '4127' })
    expect(warn).toHaveBeenCalledWith('[owner-forward] deliver failed: 4127')
  })

  it('the sheet shows the refusal as an alert with the code', async () => {
    confirm.mockResolvedValue({ ok: false, code: 'source_missing', error: 'no session 20260909_193713_ce3d96 in profile thinkbot' })
    $forwardSheet.set({ text: TEXT, gesture: 'selection', origin, targets: [target], scope: [], subject: '', ttlMs: 604_800_000 })
    render(<ForwardSheet />)

    fireEvent.click(screen.getByRole('button', { name: /^send/i }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('no session 20260909_193713_ce3d96 in profile thinkbot')
    expect(alert.textContent).toContain('(source_missing)')
  })
})
