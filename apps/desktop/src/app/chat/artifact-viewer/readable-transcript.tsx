import { useState } from 'react'

import { ToneGlyph } from '@/components/chat/status-chip'
import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { OverflowTip } from '@/components/ui/tooltip'

import type { TranscriptEntry } from './codex-transcript'

/** `21:04`, or `1:10:00` past an hour. */
export function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds))
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = String(seconds % 60).padStart(2, '0')

  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`
}

/** `Done in 21:04` · `Failed after 0:45, exit 1` · `Timed out after 1:10:00`. */
export function resultLine(status: string, durationSeconds?: number, exitCode?: number): null | string {
  if (durationSeconds === undefined) {
    return null
  }

  const d = formatDuration(durationSeconds)

  if (status === 'succeeded' || status === 'done' || status === 'completed') {
    return `Done in ${d}`
  }

  if (status === 'timeout') {
    return `Timed out after ${d}`
  }

  if (status === 'failed' || status === 'error') {
    return exitCode !== undefined && exitCode !== 0 ? `Failed after ${d}, exit ${exitCode}` : `Failed after ${d}`
  }

  return null
}

export function FinalReport({ result, text }: { result: null | string; text: string }) {
  return (
    <div className="flex max-h-[40%] shrink-0 flex-col gap-1" data-slot="artifact-final-report">
      <div className="min-h-0 overflow-auto rounded-md bg-(--ui-bg-tertiary) p-2.5">
        <div className="mb-1 text-[0.68rem] text-(--ui-text-tertiary)">Final report</div>
        <p className="whitespace-pre-wrap break-words text-xs text-(--ui-text-primary)">{text}</p>
      </div>
      {result && <div className="text-[0.68rem] text-(--ui-text-tertiary)">{result}</div>}
    </div>
  )
}

function Expandable({ children, label }: { children: React.ReactNode; label: React.ReactNode }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="min-w-0">
      <button
        aria-expanded={open}
        className="flex min-w-0 max-w-full items-center gap-1 text-left"
        onClick={() => setOpen(value => !value)}
        type="button"
      >
        {label}
        <DisclosureCaret className="text-(--ui-text-quaternary)" open={open} size="0.65rem" />
      </button>
      {open && children}
    </div>
  )
}

function CommandLine({ command, exitCode }: { command: string; exitCode: null | number }) {
  return (
    <span className="flex min-w-0 items-center gap-2">
      <OverflowTip label={`$ ${command}`}>
        <span className="min-w-0 truncate font-mono text-[0.68rem] text-(--ui-text-tertiary)">{`$ ${command}`}</span>
      </OverflowTip>
      {exitCode !== null && exitCode !== 0 && (
        <span className="shrink-0 text-[0.68rem] text-(--ui-red)">{`exit ${exitCode}`}</span>
      )}
    </span>
  )
}

/** Agent messages, commands, file edits and errors as text nodes; lifecycle events hidden. */
export function ReadableTranscript({ entries, live }: { entries: TranscriptEntry[]; live: boolean }) {
  return (
    <div className="flex flex-col gap-1.5" data-slot="artifact-readable">
      {entries.map(entry => {
        switch (entry.kind) {
          case 'message':
            return (
              <p className="whitespace-pre-wrap break-words text-xs text-(--ui-text-primary)" key={entry.key}>
                {entry.text}
              </p>
            )
          case 'command':
            return entry.exitCode !== null && entry.exitCode !== 0 && entry.output ? (
              <Expandable key={entry.key} label={<CommandLine command={entry.command} exitCode={entry.exitCode} />}>
                <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-(--ui-bg-tertiary) p-2 font-mono text-[0.68rem] text-(--ui-text-secondary)">
                  {entry.output}
                </pre>
              </Expandable>
            ) : (
              <CommandLine command={entry.command} exitCode={entry.exitCode} key={entry.key} />
            )
          case 'files':
            return (
              <Expandable
                key={entry.key}
                label={
                  <span className="text-[0.68rem] text-(--ui-text-tertiary)">
                    {`Edited ${entry.paths.length} file${entry.paths.length === 1 ? '' : 's'}`}
                  </span>
                }
              >
                <ul className="mt-0.5 pl-3 text-[0.68rem] text-(--ui-text-tertiary)">
                  {entry.paths.map((path, index) => (
                    <li key={`${path}-${index}`}>{path}</li>
                  ))}
                </ul>
              </Expandable>
            )
          case 'error':
            return (
              <div className="flex items-center gap-1.5 text-xs text-(--ui-text-secondary)" key={entry.key}>
                <ToneGlyph tone="attention" />
                <span className="min-w-0 break-words">{entry.message}</span>
              </div>
            )
          case 'running':
            return live ? (
              <div className="flex min-w-0 items-center gap-1.5" key={entry.key}>
                <GlyphSpinner ariaLabel="Running" className="text-(--ui-purple)" spinner="braille" />
                <span className="min-w-0 truncate font-mono text-[0.68rem] text-(--ui-text-tertiary)">
                  {`Running $ ${entry.command}`}
                </span>
              </div>
            ) : (
              <CommandLine command={entry.command} exitCode={null} key={entry.key} />
            )
          default:
            return (
              <div className="whitespace-pre-wrap break-all font-mono text-[0.68rem] text-(--ui-text-tertiary)" key={entry.key}>
                {entry.text}
              </div>
            )
        }
      })}
    </div>
  )
}
