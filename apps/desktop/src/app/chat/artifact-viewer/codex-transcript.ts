/**
 * Pure parser for a codex worker log window (JSON lines from `codex exec --json`). Everything
 * here is agent-written data: it is returned as plain strings for text nodes, never markup.
 */
export type TranscriptEntry =
  | { key: string; kind: 'command'; command: string; exitCode: number | null; output: string }
  | { key: string; kind: 'error'; message: string }
  | { key: string; kind: 'files'; paths: string[] }
  | { key: string; kind: 'message'; text: string }
  | { key: string; kind: 'raw'; text: string }
  | { key: string; kind: 'running'; command: string }

export interface ParsedTranscript {
  /** At least half the window's lines are codex events. */
  codex: boolean
  entries: TranscriptEntry[]
  /** The last agent message in the window, when there is one. */
  finalReport: string | null
}

const OUTPUT_PREVIEW_CHARS = 2048
const SHELL_WRAPPER = /^(?:\/(?:usr\/)?bin\/)?(?:ba|z)?sh\s+-l?c\s+(['"])([\s\S]*)\1$/

type Json = Record<string, unknown>

const isObject = (value: unknown): value is Json => typeof value === 'object' && value !== null && !Array.isArray(value)

const str = (value: unknown) => (typeof value === 'string' ? value : '')

/** `/bin/zsh -lc 'git diff --stat'` → `git diff --stat`. */
export const unwrapShell = (command: string) => command.trim().match(SHELL_WRAPPER)?.[2] ?? command.trim()

const basename = (path: string) => path.replace(/\/+$/, '').split('/').pop() || path

const isCodexEvent = (value: unknown): value is Json =>
  isObject(value) && typeof value.type === 'string' && /^(item|thread|turn)\./.test(value.type)

export function parseCodexTranscript(text: string, bof: boolean): ParsedTranscript {
  const lines = text.split('\n')

  if (!bof) {
    lines.shift() // the tail window may start mid-line
  }

  const entries: TranscriptEntry[] = []
  const pending = new Map<string, { command: string; index: number }>()
  let nonBlank = 0
  let codexLines = 0
  let finalReport: null | string = null

  lines.forEach((raw, index) => {
    const line = raw.replace(/\r$/, '')

    if (!line.trim()) {
      return
    }

    nonBlank++
    let event: unknown

    try {
      event = JSON.parse(line)
    } catch {
      event = undefined
    }

    if (!isCodexEvent(event)) {
      entries.push({ key: `raw-${index}`, kind: 'raw', text: line })

      return
    }

    codexLines++
    const item = isObject(event.item) ? event.item : null
    const itemType = str(item?.type)
    const id = str(item?.id) || `line-${index}`

    if (event.type === 'item.started' && itemType === 'command_execution') {
      pending.set(id, { command: unwrapShell(str(item?.command)), index })

      return
    }

    if (event.type !== 'item.completed' || !item) {
      return // thread.*, turn.*, other item.started: lifecycle noise
    }

    if (itemType === 'agent_message') {
      const message = str(item.text)
      entries.push({ key: `msg-${index}`, kind: 'message', text: message })
      finalReport = message
    } else if (itemType === 'command_execution') {
      pending.delete(id)
      const exitCode = typeof item.exit_code === 'number' ? item.exit_code : null
      entries.push({
        command: unwrapShell(str(item.command)),
        exitCode,
        key: `cmd-${index}`,
        kind: 'command',
        output: exitCode !== null && exitCode !== 0 ? str(item.aggregated_output).slice(0, OUTPUT_PREVIEW_CHARS) : ''
      })
    } else if (itemType === 'file_change') {
      const changes = Array.isArray(item.changes) ? item.changes : []
      const paths = changes.flatMap(change => (isObject(change) && str(change.path) ? [basename(str(change.path))] : []))
      entries.push({ key: `files-${index}`, kind: 'files', paths })
    } else if (itemType === 'error') {
      entries.push({ key: `err-${index}`, kind: 'error', message: str(item.message) })
    }
  })

  const trailing = [...pending.values()].sort((a, b) => b.index - a.index)[0]

  if (trailing) {
    entries.push({ command: trailing.command, key: `running-${trailing.index}`, kind: 'running' })
  }

  const codex = nonBlank > 0 && codexLines * 2 >= nonBlank

  return { codex, entries: codex ? entries : [], finalReport: codex ? finalReport : null }
}
