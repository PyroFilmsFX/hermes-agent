/**
 * #60 owner-forward `/to` grammar (design §4.2): `/to <target>[,<target>…] <text>`.
 * A target is `hermes:<title>`, a bare title token, a quoted title, or an id prefix of 8+ characters.
 * Recognized ONLY by the composer's `submitDraft` (a typed draft); `submitText` refuses it.
 */
export const MAX_FORWARD_TARGETS = 5

const TO_RE = /^\s*\/to(?=\s|$)/

export interface ForwardCandidate {
  profile: string
  session_id: string
  title: string | null
}

export type ToToken = { kind: 'bare' | 'peer' | 'quoted'; value: string }

export type ParsedTo = { ok: true; tokens: ToToken[]; body: string } | { ok: false; error: 'grammar' | 'too_many' }

export function isForwardCommandText(text: string): boolean {
  return TO_RE.test(text)
}

export function parseToDraft(text: string): ParsedTo | null {
  const head = TO_RE.exec(text)

  if (!head) {
    return null
  }

  const rest = text.slice(head[0].length)
  let i = 0

  const skipSpace = () => {
    while (i < rest.length && /\s/.test(rest[i])) {
      i += 1
    }
  }

  skipSpace()

  if (i === 0 || i >= rest.length) {
    return { ok: false, error: 'grammar' }
  }

  const tokens: ToToken[] = []

  for (;;) {
    if (rest.startsWith('hermes:"', i)) {
      const valueStart = i + 'hermes:"'.length
      const close = rest.indexOf('"', valueStart)

      if (close < 0 || close === valueStart) {
        return { ok: false, error: 'grammar' }
      }

      tokens.push({ kind: 'peer', value: rest.slice(valueStart, close) })
      i = close + 1
    } else if (rest[i] === '"') {
      const close = rest.indexOf('"', i + 1)

      if (close < 0 || close === i + 1) {
        return { ok: false, error: 'grammar' }
      }

      tokens.push({ kind: 'quoted', value: rest.slice(i + 1, close) })
      i = close + 1
    } else {
      const start = i

      while (i < rest.length && !/[\s,]/.test(rest[i])) {
        i += 1
      }

      const raw = rest.slice(start, i)

      if (!raw) {
        return { ok: false, error: 'grammar' }
      }

      if (raw.startsWith('hermes:')) {
        const value = raw.slice('hermes:'.length)

        if (!value) {
          return { ok: false, error: 'grammar' }
        }

        tokens.push({ kind: 'peer', value })
      } else {
        tokens.push({ kind: 'bare', value: raw })
      }
    }

    if (rest[i] === ',') {
      i += 1

      continue
    }

    break
  }

  if (i >= rest.length || !/\s/.test(rest[i])) {
    return { ok: false, error: 'grammar' }
  }

  const body = rest.slice(i).trim()

  if (!body) {
    return { ok: false, error: 'grammar' }
  }

  if (tokens.length > MAX_FORWARD_TARGETS) {
    return { ok: false, error: 'too_many' }
  }

  return { ok: true, tokens, body }
}

function matchesFor(token: ToToken, candidates: readonly ForwardCandidate[]): ForwardCandidate[] {
  const byTitle = candidates.filter(c => c.title === token.value)
  const titles = byTitle.length ? byTitle : candidates.filter(c => c.title?.toLowerCase() === token.value.toLowerCase())

  if (token.kind !== 'bare') {
    return titles
  }

  const ids =
    token.value.length >= 8 && /^[A-Za-z0-9._:-]+$/.test(token.value)
      ? candidates.filter(c => c.session_id.startsWith(token.value))
      : []

  const seen = new Set<string>()

  return [...titles, ...ids].filter(c => {
    const key = `${c.profile}:${c.session_id}`

    return seen.has(key) ? false : (seen.add(key), true)
  })
}

/** Exact only when every token matches exactly one candidate; otherwise the unique matches are
 *  preselected and the caller opens the sheet. */
export function resolveToTokens(
  tokens: readonly ToToken[],
  candidates: readonly ForwardCandidate[]
): { status: 'ambiguous' | 'exact'; targets: ForwardCandidate[] } {
  const targets: ForwardCandidate[] = []
  const seen = new Set<string>()
  let exact = tokens.length > 0

  for (const token of tokens) {
    const matches = matchesFor(token, candidates)

    if (matches.length !== 1) {
      exact = false

      continue
    }

    const key = `${matches[0].profile}:${matches[0].session_id}`

    if (!seen.has(key)) {
      seen.add(key)
      targets.push(matches[0])
    }
  }

  return { status: exact ? 'exact' : 'ambiguous', targets }
}
