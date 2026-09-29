/**
 * D33 `:::send-to` container directive: a session writes a line the owner should deliver to another
 * session, and the desktop renders it as a quote with a "Send to <session>" button.
 *
 *   :::send-to{session="cntrl-core-worker"}
 *   approve: merge w6/wd-ci-hold
 *   :::
 *
 * The body is agent-authored text. Nothing here sends: the button only OPENS the existing Forward
 * sheet on a trusted click (`openForwardSheet`), and the sheet's Send… plus main's native confirm are
 * the owner's two deliberate acts. Masking and signing stay in `sendOwnerForward` (D25).
 *
 * Rendering path: `sendToPlaceholders` runs on the raw message text BEFORE `preprocessMarkdown` (so
 * the prose rewrites never touch a directive), swapping each block for an inert token line; after
 * preprocessing, `sendToFences` turns each token into a fenced block whose language names the
 * directive's index (behind the per-load nonce, directive-nonce.ts). The card re-parses the RAW text and takes the directive at that index, so the
 * body it shows and sends is exactly what the model wrote (trimmed), never rendered markdown.
 */
import { DIRECTIVE_NONCE } from './directive-nonce'
import type { ForwardCandidate } from './parse-to'
import { matchesFor } from './parse-to'

export const SEND_TO_DIRECTIVE = 'send-to'

export interface SendToDirective {
  /** Order in the message, from 0. */
  index: number
  /** The `session` attribute as written (untrusted), or null when absent/empty. */
  session: null | string
  /** The body between the fences, trimmed. Exactly what a send carries. */
  body: string
  /** False when the closing `:::` line hasn't arrived (streaming) or was never written. */
  closed: boolean
}

// Opening fence: 3+ colons, the name, then one `{…}` attribute list; nothing else on the line.
// Up to 3 leading spaces like any markdown fence. Length caps bound the scan on adversarial input.
const OPEN_RE = /^ {0,3}(:{3,})send-to\{([^{}\n]{0,512})\}[ \t]*$/
const CLOSE_RE = /^ {0,3}(:{3,})[ \t]*$/
const CODE_FENCE_RE = /^ {0,3}(`{3,}|~{3,})/
const ATTR_RE = /([a-z][\w-]{0,63})=(?:"([^"]*)"|'([^']*)')/gi

interface Block extends SendToDirective {
  /** First line (the opening fence) and one past the last line (the closing fence, if any). */
  startLine: number
  endLine: number
}

function sessionAttr(attrs: string): null | string {
  for (const pair of attrs.matchAll(ATTR_RE)) {
    if (pair[1].toLowerCase() === 'session') {
      const value = (pair[2] ?? pair[3] ?? '').trim()

      return value || null
    }
  }

  return null
}

/** Tracks ``` / ~~~ fences so a directive shown inside a code block stays code. */
export function fenceStep(line: string, open: null | string): null | string {
  const match = CODE_FENCE_RE.exec(line)

  if (!match) {
    return open
  }

  const marker = match[1]

  if (open === null) {
    return marker
  }

  // A closer uses the same character, is at least as long, and carries no info string.
  return marker[0] === open[0] && marker.length >= open.length && !line.trim().slice(marker.length).trim() ? null : open
}

function scan(text: string): Block[] {
  if (!text.includes(':::send-to')) {
    return []
  }

  const lines = text.split('\n')
  const blocks: Block[] = []
  let fence: null | string = null
  let line = 0

  while (line < lines.length) {
    const current = lines[line].replace(/\r$/, '')
    const open = fence === null ? OPEN_RE.exec(current) : null

    if (!open) {
      fence = fenceStep(current, fence)
      line += 1

      continue
    }

    const colons = open[1].length
    const body: string[] = []
    let bodyFence: null | string = null
    let close = -1

    for (let cursor = line + 1; cursor < lines.length; cursor += 1) {
      const candidate = lines[cursor].replace(/\r$/, '')
      const closer = bodyFence === null ? CLOSE_RE.exec(candidate) : null

      if (closer && closer[1].length >= colons) {
        close = cursor

        break
      }

      bodyFence = fenceStep(candidate, bodyFence)
      body.push(candidate)
    }

    const endLine = close === -1 ? lines.length : close + 1

    blocks.push({
      index: blocks.length,
      session: sessionAttr(open[2]),
      body: body.join('\n').trim(),
      closed: close !== -1,
      startLine: line,
      endLine
    })
    line = endLine
  }

  return blocks
}

/** Every `:::send-to` block in `text`, in order. Directives inside code fences are not directives. */
export function parseSendToDirectives(text: string): SendToDirective[] {
  return scan(text).map(({ index, session, body, closed }) => ({ index, session, body, closed }))
}

// Per-load nonce (b9 §5): a model can't write a token line the fence pass would pick up, nor a
// fence whose language the code override would claim. Only nonce-bearing fences are accepted.
const TOKEN_PREFIX = `hermessendto${DIRECTIVE_NONCE}i`
const TOKEN_LINE_RE = new RegExp(`^[ \\t]*${TOKEN_PREFIX}(\\d{1,4})x[ \\t]*$`, 'gm')
const LANGUAGE_PREFIX = `hermes-send-to-${DIRECTIVE_NONCE}-`

/** Phase 1 (raw text, before `preprocessMarkdown`): each block becomes one inert token paragraph. */
export function sendToPlaceholders(text: string): string {
  const blocks = scan(text)

  if (blocks.length === 0) {
    return text
  }

  const lines = text.split('\n')
  const out: string[] = []
  let cursor = 0

  for (const block of blocks) {
    out.push(...lines.slice(cursor, block.startLine), '', `${TOKEN_PREFIX}${block.index}x`, '')
    cursor = block.endLine
  }

  out.push(...lines.slice(cursor))

  return out.join('\n')
}

/** Phase 2 (after `preprocessMarkdown`): each token line becomes a fenced block the renderer's
 *  code override claims by language. The body is a placeholder; the card reads the raw text. */
export function sendToFences(text: string): string {
  if (!text.includes(TOKEN_PREFIX)) {
    return text
  }

  return text.replace(
    TOKEN_LINE_RE,
    (_line, index: string) => `\`\`\`${LANGUAGE_PREFIX}${index}\n${SEND_TO_DIRECTIVE}\n\`\`\``
  )
}

/** The directive index a fenced block's language names, or null for any other code block. Only a
 *  language carrying this load's nonce counts, so a model-written ```hermes-send-to-0 fence stays code. */
export function sendToIndexFromLanguage(language: string | undefined): null | number {
  if (!language?.startsWith(LANGUAGE_PREFIX)) {
    return null
  }

  const raw = language.slice(LANGUAGE_PREFIX.length)

  return /^\d{1,4}$/.test(raw) ? Number(raw) : null
}

/** The name as the owner reads it: `hermes:` is optional, and a quoted title loses its quotes. */
export function sendToTargetName(session: string): string {
  const bare = session
    .trim()
    .replace(/^hermes:/, '')
    .trim()

  return bare.length > 1 && bare.startsWith('"') && bare.endsWith('"') ? bare.slice(1, -1).trim() : bare
}

export type SendToResolution =
  | { status: 'one'; target: ForwardCandidate }
  | { status: 'none'; name: string }
  | { status: 'many'; name: string; count: number }

/**
 * Resolve `session` the way `/to` resolves a target: exact title (case-insensitive fallback) or an id
 * prefix of 8+ characters, `hermes:` optional. Only exactly one match is sendable.
 */
export function resolveSendToTarget(session: string, candidates: readonly ForwardCandidate[]): SendToResolution {
  const name = sendToTargetName(session)

  if (!name) {
    return { status: 'none', name }
  }

  const matches = matchesFor({ kind: 'bare', value: name }, candidates)

  if (matches.length === 1) {
    return { status: 'one', target: matches[0] }
  }

  return matches.length === 0 ? { status: 'none', name } : { status: 'many', name, count: matches.length }
}
