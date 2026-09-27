import { useEffect, useState } from 'react'

import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

type ChipState = 'checking' | 'copy' | 'unverified' | 'verified'

/**
 * #60 U16 (V-12) + D24: the provenance chip on a delivered owner_forward row. "Owner-signed text" only
 * when main re-verifies the stored envelope against the root-owned anchor for THIS chat and THIS text;
 * it proves the text was signed, not that this row was the delivered turn (a same-uid DB edit is R1).
 * A later row carrying the same signed payload (`copy`) reads "Copy of signed text", neutral, never
 * green. A row with no envelope (forged metadata, an old row) is "Not verified" without asking main.
 * Rendered as React text only.
 */
export function OwnerForwardChip({
  copy = false,
  envelope,
  fromTitle,
  sessionId,
  text
}: {
  copy?: boolean
  envelope: unknown
  fromTitle: string
  sessionId: null | string
  text: string
}) {
  const { t } = useI18n()
  const strings = t.ownerForward
  const [state, setState] = useState<ChipState>('checking')

  useEffect(() => {
    let live = true
    const verify = window.hermesDesktop?.ownerGrant?.verify

    if (!envelope || typeof envelope !== 'object' || !sessionId || !verify) {
      setState('unverified')

      return
    }

    setState('checking')
    verify({ envelope, sessionId, text })
      .then(verdict => live && setState(verdict?.state === 'verified' ? (copy ? 'copy' : 'verified') : 'unverified'))
      .catch(() => live && setState('unverified'))

    return () => {
      live = false
    }
  }, [copy, envelope, sessionId, text])

  const label =
    state === 'verified'
      ? strings.verified
      : state === 'copy'
        ? strings.copyOfSigned
        : state === 'unverified'
          ? strings.unverified
          : strings.checking

  const hint =
    state === 'verified'
      ? strings.verifiedHint
      : state === 'copy'
        ? strings.copyHint
        : state === 'unverified'
          ? strings.unverifiedHint
          : undefined

  return (
    <div
      className="mb-1 flex items-center justify-end gap-1.5 text-[0.6875rem] leading-4 text-muted-foreground"
      data-slot="owner-forward-chip"
      data-state={state}
    >
      <span>↪ {strings.forwardedFrom(fromTitle || '…')}</span>
      <span
        className={cn(
          'rounded-full border px-1.5',
          state === 'verified' && 'border-emerald-500/40 text-emerald-600 dark:text-emerald-400',
          state === 'unverified' && 'border-amber-500/40 text-amber-600 dark:text-amber-400'
        )}
        title={hint}
      >
        {label}
      </span>
    </div>
  )
}
