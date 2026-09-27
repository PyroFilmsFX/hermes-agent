import { useEffect, useState } from 'react'

import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

type ChipState = 'checking' | 'unverified' | 'verified'

/**
 * #60 U16 (V-12): the provenance chip on a delivered owner_forward row. It says "verified" only when
 * main re-verifies the stored envelope against the root-owned anchor for THIS chat and THIS text.
 * A row with no envelope (forged metadata, an old row) is unverified without asking main.
 * Rendered as React text only.
 */
export function OwnerForwardChip({
  envelope,
  fromTitle,
  sessionId,
  text
}: {
  envelope: unknown
  fromTitle: string
  sessionId: null | string
  text: string
}) {
  const { t } = useI18n()
  const copy = t.ownerForward
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
      .then(verdict => live && setState(verdict?.state === 'verified' ? 'verified' : 'unverified'))
      .catch(() => live && setState('unverified'))

    return () => {
      live = false
    }
  }, [envelope, sessionId, text])

  const label = state === 'verified' ? copy.verified : state === 'unverified' ? copy.unverified : copy.checking

  return (
    <div
      className="mb-1 flex items-center justify-end gap-1.5 text-[0.6875rem] leading-4 text-muted-foreground"
      data-slot="owner-forward-chip"
      data-state={state}
    >
      <span>↪ {copy.forwardedFrom(fromTitle || '…')}</span>
      <span
        className={cn(
          'rounded-full border px-1.5',
          state === 'verified' && 'border-emerald-500/40 text-emerald-600 dark:text-emerald-400',
          state === 'unverified' && 'border-amber-500/40 text-amber-600 dark:text-amber-400'
        )}
        title={state === 'verified' ? copy.verifiedHint : state === 'unverified' ? copy.unverifiedHint : undefined}
      >
        {label}
      </span>
    </div>
  )
}
