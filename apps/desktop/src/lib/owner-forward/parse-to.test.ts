import { describe, expect, it } from 'vitest'

import { isForwardCommandText, parseToDraft, resolveToTokens, type ForwardCandidate } from './parse-to'

const candidates: ForwardCandidate[] = [
  { profile: 'default', session_id: '20260926_aaaaaaaa11', title: 'conductor-worker' },
  { profile: 'default', session_id: '20260926_bbbbbbbb22', title: 'My Session' },
  { profile: 'default', session_id: '20260926_cccccccc33', title: 'review' },
  { profile: 'work', session_id: '20260926_dddddddd44', title: 'review' }
]

describe('/to grammar', () => {
  it('recognizes only a leading /to', () => {
    expect(isForwardCommandText('/to w hi')).toBe(true)
    expect(isForwardCommandText('  /to w hi')).toBe(true)
    expect(isForwardCommandText('/tools')).toBe(false)
    expect(isForwardCommandText('please /to w hi')).toBe(false)
  })

  it('parses targets and a multiline body', () => {
    expect(parseToDraft('/to hermes:conductor-worker,"My Session" line one\nline two')).toEqual({
      ok: true,
      tokens: [
        { kind: 'peer', value: 'conductor-worker' },
        { kind: 'quoted', value: 'My Session' }
      ],
      body: 'line one\nline two'
    })
  })

  it('refuses a draft with no text or no target', () => {
    expect(parseToDraft('/to')).toEqual({ ok: false, error: 'grammar' })
    expect(parseToDraft('/to review')).toEqual({ ok: false, error: 'grammar' })
    expect(parseToDraft('/to "unterminated text')).toEqual({ ok: false, error: 'grammar' })
  })

  it('refuses more than five targets', () => {
    expect(parseToDraft('/to a,b,c,d,e,f hi')).toEqual({ ok: false, error: 'too_many' })
  })

  it('returns null for text that is not /to', () => {
    expect(parseToDraft('hello')).toBeNull()
  })
})

describe('/to target resolution', () => {
  it('an exact unique match resolves', () => {
    const parsed = parseToDraft('/to hermes:conductor-worker go') as any
    expect(resolveToTokens(parsed.tokens, candidates)).toEqual({ status: 'exact', targets: [candidates[0]] })
  })

  it('an id prefix of 8+ characters resolves', () => {
    const parsed = parseToDraft('/to 20260926_bbbb go') as any
    expect(resolveToTokens(parsed.tokens, candidates)).toEqual({ status: 'exact', targets: [candidates[1]] })
  })

  it('an ambiguous or unknown target is not exact, and keeps the unique ones preselected', () => {
    const ambiguous = parseToDraft('/to review,hermes:conductor-worker go') as any
    expect(resolveToTokens(ambiguous.tokens, candidates)).toEqual({ status: 'ambiguous', targets: [candidates[0]] })

    const unknown = parseToDraft('/to nobody go') as any
    expect(resolveToTokens(unknown.tokens, candidates)).toEqual({ status: 'ambiguous', targets: [] })

    const shortId = parseToDraft('/to 2026 go') as any
    expect(resolveToTokens(shortId.tokens, candidates).status).toBe('ambiguous')
  })
})
