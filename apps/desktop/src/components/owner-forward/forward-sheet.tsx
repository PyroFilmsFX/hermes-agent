import { useStore } from '@nanostores/react'
import { type MouseEvent, useId, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { useI18n } from '@/i18n'
import {
  $forwardSheet,
  closeForwardSheet,
  describeForwardResult,
  FORWARD_MAX_CHARS,
  forwardCandidates,
  type ForwardOutcome,
  type ForwardTarget,
  sendOwnerForward
} from '@/lib/owner-forward/client'
import { MAX_FORWARD_TARGETS } from '@/lib/owner-forward/parse-to'
import {
  needsSubject,
  SCOPE_CATALOG,
  SCOPE_RE,
  scopeClassOf,
  scopeLabel,
  scopeTtlOptions,
  scopeTtlPolicy
} from '@/lib/owner-forward/scopes'
import { isTrustedGesture } from '@/lib/owner-forward/trusted'
import { cn } from '@/lib/utils'
import { $sessions } from '@/store/session'

const key = (t: { profile: string; session_id: string }) => `${t.profile}:${t.session_id}`

/**
 * #60 the Forward sheet (design §4.1, §7; addendum U15): app chrome, never a widget. Targets (up to 5),
 * the editable text with a counter, the scope picker with plain labels, a subject for prod, and Send….
 * Send… requires a trusted click and then asks MAIN, which shows the native "Send as you?" confirm with
 * the titles it resolved itself. The sheet shows each target's settled status afterwards.
 */
export function ForwardSheet() {
  const sheet = useStore($forwardSheet)
  useStore($sessions)
  const { t } = useI18n()
  const copy = t.ownerForward
  const ids = useId()
  const [query, setQuery] = useState('')
  const [customScope, setCustomScope] = useState('')
  const [scopeError, setScopeError] = useState(false)
  const [phase, setPhase] = useState<'done' | 'edit' | 'sending'>('edit')
  const [outcome, setOutcome] = useState<ForwardOutcome | null>(null)

  const candidates = useMemo(() => {
    if (!sheet) {
      return []
    }

    const listed = forwardCandidates(sheet.origin.session_id)
    const listedKeys = new Set(listed.map(key))

    // Preselected targets that aren't in the loaded list (a proposal naming another profile) still show.
    return [
      ...sheet.targets
        .filter(target => !listedKeys.has(key(target)))
        .map(target => ({ ...target, title: target.title ?? null })),
      ...listed
    ]
  }, [sheet])

  if (!sheet) {
    return null
  }

  const close = () => {
    closeForwardSheet()
    setPhase('edit')
    setOutcome(null)
    setQuery('')
    setCustomScope('')
    setScopeError(false)
  }

  const update = (patch: Partial<typeof sheet>) => $forwardSheet.set({ ...sheet, ...patch })
  const selected = new Set(sheet.targets.map(key))

  const toggleTarget = (target: ForwardTarget) => {
    if (selected.has(key(target))) {
      update({ targets: sheet.targets.filter(t => key(t) !== key(target)) })
    } else if (sheet.targets.length < MAX_FORWARD_TARGETS) {
      update({ targets: [...sheet.targets, target] })
    }
  }

  const toggleScope = (value: string) => {
    const scope = sheet.scope.includes(value) ? sheet.scope.filter(s => s !== value) : [...sheet.scope, value]

    update({ scope, ttlMs: scopeTtlPolicy(scope).defaultTtlMs })
  }

  const addCustomScope = () => {
    const value = customScope.trim()

    if (!SCOPE_RE.test(value)) {
      setScopeError(true)

      return
    }

    setScopeError(false)
    setCustomScope('')

    if (!sheet.scope.includes(value)) {
      const scope = [...sheet.scope, value]
      update({ scope, ttlMs: scopeTtlPolicy(scope).defaultTtlMs })
    }
  }

  const q = query.trim().toLowerCase()

  const shown = q
    ? candidates.filter(c => (c.title ?? '').toLowerCase().includes(q) || c.session_id.toLowerCase().includes(q))
    : candidates

  const subjectNeeded = needsSubject(sheet.scope)
  const textLength = sheet.text.length
  const ttlPolicy = scopeTtlPolicy(sheet.scope)
  const ttlMs = Math.min(sheet.ttlMs ?? ttlPolicy.defaultTtlMs, ttlPolicy.maxTtlMs)
  const ttlLabel = (value: number) => {
    const minutes = value / 60_000

    if (minutes < 60) {
      return copy.ttlOption(minutes, 'minutes')
    }

    const hours = minutes / 60

    if (hours < 24) {
      return copy.ttlOption(hours, 'hours')
    }

    return copy.ttlOption(hours / 24, 'days')
  }

  const canSend =
    phase === 'edit' &&
    sheet.targets.length > 0 &&
    sheet.text.trim().length > 0 &&
    textLength <= FORWARD_MAX_CHARS &&
    (!subjectNeeded || sheet.subject.trim().length > 0)

  const send = async (event: MouseEvent) => {
    if (!canSend || !isTrustedGesture(event.nativeEvent)) {
      return
    }

    setPhase('sending')
    setOutcome(null)

    const result = await sendOwnerForward({
      text: sheet.text,
      gesture: sheet.gesture,
      origin: sheet.origin,
      targets: sheet.targets,
      scope: sheet.scope,
      ttlMs,
      subject: subjectNeeded ? sheet.subject : null,
      proposalId: sheet.proposalId
    })

    setOutcome(result)
    setPhase(result.kind === 'sent' ? 'done' : 'edit')
  }

  const customScopes = sheet.scope.filter(s => !SCOPE_CATALOG.some(entry => entry.value === s))

  return (
    <Dialog onOpenChange={open => !open && close()} open>
      <DialogContent className="max-w-lg" data-slot="owner-forward-sheet">
        <DialogHeader>
          <DialogTitle>{copy.sheetTitle}</DialogTitle>
          <DialogDescription>{copy.sheetDescription}</DialogDescription>
        </DialogHeader>

        <fieldset className="grid gap-1.5" disabled={phase !== 'edit'}>
          <legend className="text-xs font-medium">
            {copy.targets} <span className="text-muted-foreground">({copy.maxTargets})</span>
          </legend>
          <Input
            aria-label={copy.searchTargets}
            onChange={event => setQuery(event.target.value)}
            placeholder={copy.searchTargets}
            value={query}
          />
          <div className="max-h-40 overflow-y-auto rounded-md border p-1">
            {shown.length === 0 ? (
              <p className="p-1.5 text-xs text-muted-foreground">{copy.noTargets}</p>
            ) : (
              shown.map(c => (
                <label className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-sm hover:bg-accent" key={key(c)}>
                  <input
                    checked={selected.has(key(c))}
                    disabled={!selected.has(key(c)) && sheet.targets.length >= MAX_FORWARD_TARGETS}
                    onChange={() => toggleTarget(c)}
                    type="checkbox"
                  />
                  <span className="truncate">{c.title || c.session_id}</span>
                  <span className="ml-auto shrink-0 font-mono text-[0.6875rem] text-muted-foreground">
                    {c.profile !== 'default' ? `${c.profile} · ` : ''}
                    {c.session_id.slice(0, 8)}
                  </span>
                </label>
              ))
            )}
          </div>
        </fieldset>

        <div className="grid gap-1.5">
          <label className="text-xs font-medium" htmlFor={`${ids}-text`}>
            {copy.text}
          </label>
          <Textarea
            className="min-h-28"
            disabled={phase !== 'edit'}
            id={`${ids}-text`}
            onChange={event => update({ text: event.target.value })}
            value={sheet.text}
          />
          <span className={cn('text-right text-[0.6875rem] tabular-nums text-muted-foreground', textLength > FORWARD_MAX_CHARS && 'text-destructive')}>
            {copy.charCount(textLength, FORWARD_MAX_CHARS)}
          </span>
        </div>

        <fieldset className="grid gap-1" disabled={phase !== 'edit'}>
          <legend className="text-xs font-medium">{copy.scopes}</legend>
          {sheet.scope.length === 0 && <p className="text-xs text-muted-foreground">{copy.scopeNone}</p>}
          {[...SCOPE_CATALOG.map(entry => entry.value), ...customScopes].map(value => {
            const scopeClass = scopeClassOf(value)

            return (
              <label className="flex items-center gap-2 text-sm" key={value}>
                <input checked={sheet.scope.includes(value)} onChange={() => toggleScope(value)} type="checkbox" />
                <span>{scopeLabel(value)}</span>
                {scopeClass && <span className="ml-auto text-[0.6875rem] text-muted-foreground">{copy.scopeClass[scopeClass]}</span>}
              </label>
            )
          })}
          <div className="flex items-center gap-1.5">
            <Input
              aria-label={copy.otherScope}
              onChange={event => {
                setCustomScope(event.target.value)
                setScopeError(false)
              }}
              placeholder="conductor:gate:…"
              value={customScope}
            />
            <Button onClick={addCustomScope} size="sm" type="button" variant="outline">
              {copy.addScope}
            </Button>
          </div>
          {scopeError && <p className="text-xs text-destructive">{copy.badScope}</p>}
        </fieldset>

        <div className="grid gap-1.5">
          <label className="text-xs font-medium" htmlFor={`${ids}-ttl`}>
            {copy.ttl}
          </label>
          <select
            className="desktop-input-chrome w-full rounded-md border px-2.5 py-1.5 text-xs"
            disabled={phase !== 'edit'}
            id={`${ids}-ttl`}
            onChange={event => update({ ttlMs: Number(event.target.value) })}
            value={String(ttlMs)}
          >
            {scopeTtlOptions(sheet.scope).map(value => (
              <option key={value} value={value}>
                {ttlLabel(value)}
              </option>
            ))}
          </select>
        </div>

        {subjectNeeded && (
          <div className="grid gap-1.5">
            <label className="text-xs font-medium" htmlFor={`${ids}-subject`}>
              {copy.subject}
            </label>
            <Input
              disabled={phase !== 'edit'}
              id={`${ids}-subject`}
              onChange={event => update({ subject: event.target.value })}
              placeholder={copy.subjectPlaceholder}
              value={sheet.subject}
            />
          </div>
        )}

        {phase === 'sending' && <p className="text-xs text-muted-foreground">{copy.sending}</p>}
        {outcome?.kind === 'cancelled' && <p className="text-xs text-muted-foreground">{copy.cancelled}</p>}
        {outcome?.kind === 'error' && (
          <p
            className="rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-1.5 text-xs font-medium text-destructive"
            data-slot="owner-forward-error"
            role="alert"
          >
            {copy.failed}: {outcome.message}
            {outcome.code && <span className="ml-1 font-mono text-[0.6875rem] opacity-80">({outcome.code})</span>}
          </p>
        )}
        {outcome?.kind === 'sent' && (
          <ul className="grid gap-0.5 text-xs" data-slot="owner-forward-results">
            {outcome.results.map(line => (
              <li className={cn(line.status.startsWith('failed') && 'text-destructive')} key={`${line.target}-${line.status}`}>
                {describeForwardResult(line, copy)}
              </li>
            ))}
          </ul>
        )}

        <DialogFooter>
          {phase === 'done' ? (
            <Button onClick={close} type="button">
              {copy.done}
            </Button>
          ) : (
            <>
              <Button onClick={close} type="button" variant="outline">
                {copy.cancel}
              </Button>
              <Button disabled={!canSend} onClick={event => void send(event)} type="button">
                {copy.send}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
