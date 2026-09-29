/**
 * b9 §5 `:::stage-question` container directive: a conductor asks the owner to pick a stage variant,
 * and the desktop renders the question with one answer button per option.
 *
 *   :::stage-question{question_sha256="<64 hex>" scope="conductor:answer:stage-variant"}
 *   Which variant should wave 3 ship?
 *   1. Keep the flag off
 *   2. Ship behind the flag
 *   :::
 *
 * A click signs ONLY through the existing composer_signed self-target path (sendOwnerForward → main's
 * native confirm); the grant carries `conductor:answer:stage-variant` with the subject
 * `<question_sha256>:<option_index>` (1-based). Nothing here signs.
 *
 * Hash (shared with hermes_owner_grant and tests/fixtures/owner_grant_subject_vectors.json):
 *   1. the message text has every CRLF replaced by LF;
 *   2. BODY = the lines strictly between the opening `:::stage-question{…}` line and the closing
 *      `:::` line, joined with LF (question text, then the numbered option lines, exactly as written);
 *   3. BODY is trimmed at both ends of U+0020, U+0009 and U+000A only (no other whitespace, no
 *      Unicode normalisation);
 *   4. question_sha256 = lowercase hex SHA-256 of BODY's UTF-8 bytes.
 * The renderer recomputes it and keeps the buttons disabled unless it equals the attribute.
 *
 * Rendering path mirrors `:::send-to`: `stageQuestionPlaceholders` runs on the text AFTER
 * `sendToPlaceholders` (and before `preprocessMarkdown`); `stageQuestionFences` turns each token into a
 * fenced block whose language carries the per-load nonce and the index; the card re-parses the RAW
 * text (through the same send-to pass, so the indices line up) and takes the directive at that index.
 */
import { DIRECTIVE_NONCE } from './directive-nonce'
import { normalizeStageQuestionBody, stageQuestionSha256, stageVariantSubject } from './scopes'
import { fenceStep, sendToPlaceholders } from './send-to-directive'

export const STAGE_QUESTION_DIRECTIVE = 'stage-question'
export const STAGE_QUESTION_SCOPE = 'conductor:answer:stage-variant'
export const STAGE_QUESTION_MAX_OPTIONS = 20
export const STAGE_QUESTION_MAX_BODY_CHARS = 8000

export type StageQuestionProblem =
  | 'bad_hash_attr'
  | 'bad_scope'
  | 'duplicate_attr'
  | 'no_options'
  | 'no_question'
  | 'option_numbering'
  | 'text_after_options'
  | 'too_long'
  | 'too_many_options'

export interface StageQuestionOption {
  /** 1-based, and equal to the number written on the line. */
  index: number
  label: string
}

export interface StageQuestionDirective {
  /** Order in the message (after the send-to pass), from 0. */
  index: number
  /** The `question_sha256` attribute as written (untrusted), or null when absent. */
  claimedSha256: null | string
  /** The `scope` attribute as written (untrusted), or null when absent. */
  scope: null | string
  /** The hashed body (see the header): exactly what the conductor hashed. */
  body: string
  /** sha256 of `body`, recomputed here. */
  sha256: string
  /** True only when the attribute is 64 lowercase hex and equals `sha256`. */
  hashMatches: boolean
  question: string
  options: StageQuestionOption[]
  /** The first structural problem, or null when the block is well formed. */
  problem: null | StageQuestionProblem
  /** False when the closing `:::` line hasn't arrived (streaming) or was never written. */
  closed: boolean
}

const OPEN_RE = /^ {0,3}(:{3,})stage-question\{([^{}\n]{0,512})\}[ \t]*$/
const CLOSE_RE = /^ {0,3}(:{3,})[ \t]*$/
const ATTR_RE = /([a-z][\w-]{0,63})=(?:"([^"]*)"|'([^']*)')/gi
const OPTION_RE = /^ {0,3}([1-9]\d{0,2})[.)][ \t]+(\S.*)$/
const SHA256_RE = /^[0-9a-f]{64}$/

// The hash and subject helpers live with the scope catalog (scopes.ts, lane B) so the verifier
// fixture pins one implementation; re-exported here for the directive's callers.
export { normalizeStageQuestionBody, stageQuestionSha256 }

/** The grant subject for option `optionIndex` (1-based): `<question_sha256>:<option_index>`. */
export const stageQuestionSubject = stageVariantSubject

function readAttrs(attrs: string): { values: Map<string, string>; duplicate: boolean } {
  const values = new Map<string, string>()
  let duplicate = false

  for (const pair of attrs.matchAll(ATTR_RE)) {
    const key = pair[1].toLowerCase()

    if (values.has(key)) {
      duplicate = true
    }

    values.set(key, pair[2] ?? pair[3] ?? '')
  }

  return { values, duplicate }
}

function splitBody(body: string): {
  question: string
  options: StageQuestionOption[]
  problem: null | StageQuestionProblem
} {
  const lines = body.split('\n')
  const first = lines.findIndex(line => OPTION_RE.test(line))
  const question = normalizeStageQuestionBody((first === -1 ? lines : lines.slice(0, first)).join('\n'))

  if (first === -1) {
    return { question, options: [], problem: question ? 'no_options' : 'no_question' }
  }

  const options: StageQuestionOption[] = []
  let problem: null | StageQuestionProblem = question ? null : 'no_question'

  for (const line of lines.slice(first)) {
    if (!line.trim()) {
      continue
    }

    const match = OPTION_RE.exec(line)

    if (!match) {
      problem ??= 'text_after_options'

      continue
    }

    const written = Number(match[1])

    if (written !== options.length + 1) {
      problem ??= 'option_numbering'
    }

    options.push({ index: options.length + 1, label: match[2].trim() })
  }

  if (options.length > STAGE_QUESTION_MAX_OPTIONS) {
    problem ??= 'too_many_options'
  }

  return { question, options, problem }
}

interface Block extends StageQuestionDirective {
  startLine: number
  endLine: number
}

function scan(text: string): Block[] {
  if (!text.includes(':::stage-question')) {
    return []
  }

  const lines = text.replace(/\r\n/g, '\n').split('\n')
  const blocks: Block[] = []
  let fence: null | string = null
  let line = 0

  while (line < lines.length) {
    const current = lines[line]
    const open = fence === null ? OPEN_RE.exec(current) : null

    if (!open) {
      fence = fenceStep(current, fence)
      line += 1

      continue
    }

    const colons = open[1].length
    const raw: string[] = []
    let bodyFence: null | string = null
    let close = -1

    for (let cursor = line + 1; cursor < lines.length; cursor += 1) {
      const candidate = lines[cursor]
      const closer = bodyFence === null ? CLOSE_RE.exec(candidate) : null

      if (closer && closer[1].length >= colons) {
        close = cursor

        break
      }

      bodyFence = fenceStep(candidate, bodyFence)
      raw.push(candidate)
    }

    const endLine = close === -1 ? lines.length : close + 1
    const body = normalizeStageQuestionBody(raw.join('\n'))
    const sha256 = stageQuestionSha256(body)
    const { values, duplicate } = readAttrs(open[2])
    const claimed = values.get('question_sha256') ?? null
    const scope = values.get('scope') ?? null
    const parts = splitBody(body)

    let problem: null | StageQuestionProblem = null

    if (duplicate) {
      problem = 'duplicate_attr'
    } else if (claimed === null || !SHA256_RE.test(claimed)) {
      problem = 'bad_hash_attr'
    } else if (scope !== STAGE_QUESTION_SCOPE) {
      problem = 'bad_scope'
    } else if (body.length > STAGE_QUESTION_MAX_BODY_CHARS) {
      problem = 'too_long'
    } else {
      problem = parts.problem
    }

    blocks.push({
      index: blocks.length,
      claimedSha256: claimed,
      scope,
      body,
      sha256,
      hashMatches: claimed !== null && SHA256_RE.test(claimed) && claimed === sha256,
      question: parts.question,
      options: parts.options,
      problem,
      closed: close !== -1,
      startLine: line,
      endLine
    })
    line = endLine
  }

  return blocks
}

function strip({ startLine: _s, endLine: _e, ...directive }: Block): StageQuestionDirective {
  return directive
}

/**
 * Every `:::stage-question` block in `text` (raw message text), in order. The send-to pass runs
 * first, exactly as in the render pipeline, so a block's index here is the index its fence names.
 * Directives inside code fences (or inside a `:::send-to` body) are not directives.
 */
export function parseStageQuestionDirectives(text: string): StageQuestionDirective[] {
  return scan(sendToPlaceholders(text)).map(strip)
}

const TOKEN_PREFIX = `hermesstageq${DIRECTIVE_NONCE}i`
const TOKEN_LINE_RE = new RegExp(`^[ \\t]*${TOKEN_PREFIX}(\\d{1,4})x[ \\t]*$`, 'gm')
const LANGUAGE_PREFIX = `hermes-stage-question-${DIRECTIVE_NONCE}-`

/** Phase 1 (after `sendToPlaceholders`, before `preprocessMarkdown`): each block becomes one inert
 *  token paragraph carrying the per-load nonce. */
export function stageQuestionPlaceholders(text: string): string {
  const source = sendToPlaceholders(text)
  const blocks = scan(source)

  if (blocks.length === 0) {
    return source
  }

  const lines = source.replace(/\r\n/g, '\n').split('\n')
  const out: string[] = []
  let cursor = 0

  for (const block of blocks) {
    out.push(...lines.slice(cursor, block.startLine), '', `${TOKEN_PREFIX}${block.index}x`, '')
    cursor = block.endLine
  }

  out.push(...lines.slice(cursor))

  return out.join('\n')
}

/** Phase 2 (after `preprocessMarkdown`): each token line becomes a fenced block whose language
 *  carries the nonce and the index. The body is a placeholder; the card reads the raw text. */
export function stageQuestionFences(text: string): string {
  if (!text.includes(TOKEN_PREFIX)) {
    return text
  }

  return text.replace(
    TOKEN_LINE_RE,
    (_line, index: string) => `\`\`\`${LANGUAGE_PREFIX}${index}\n${STAGE_QUESTION_DIRECTIVE}\n\`\`\``
  )
}

/** The directive index a fenced block's language names, or null for any other code block. Only a
 *  language carrying this load's nonce counts: a model-written ```hermes-stage-question-0 stays code. */
export function stageQuestionIndexFromLanguage(language: string | undefined): null | number {
  if (!language?.startsWith(LANGUAGE_PREFIX)) {
    return null
  }

  const raw = language.slice(LANGUAGE_PREFIX.length)

  return /^\d{1,4}$/.test(raw) ? Number(raw) : null
}
