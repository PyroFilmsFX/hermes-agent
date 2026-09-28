import { useStore } from '@nanostores/react'
import { useMemo, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  dropdownMenuRow,
  DropdownMenuSearch,
  dropdownMenuSectionLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { releaseTypingFocus } from '@/components/ui/keyboard-first'
import { Tip } from '@/components/ui/tooltip'
import { type Translations, useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { ChevronDown, Send, X } from '@/lib/icons'
import { forwardCandidates } from '@/lib/owner-forward/client'
import {
  $composerForwardTargets,
  COMPOSER_FORWARD_TTL_OPTIONS,
  setComposerForwardTarget,
  setComposerForwardTtl
} from '@/lib/owner-forward/composer-target'
import { cn } from '@/lib/utils'
import { $sessions } from '@/store/session'

import { ACTIVE_ICON_BTN, GHOST_ICON_BTN } from './control-classes'

// Same chrome as the model / reasoning pills beside it.
const PILL = cn(
  'h-(--composer-control-size) min-w-0 max-w-40 shrink gap-1 rounded-md px-2 text-xs font-normal',
  'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
)

const MAX_LISTED = 30

/** THIS surface's "send to" target, or null. For the composer's placeholder. */
export function useComposerForwardTarget() {
  const storedId = useStore(useSessionView().$storedId)
  const targets = useStore($composerForwardTargets)

  return storedId ? (targets[storedId] ?? null) : null
}

function ttlLabel(value: number, copy: Translations['ownerForward']): string {
  const minutes = value / 60_000

  if (minutes < 60) {
    return copy.ttlOption(minutes, 'minutes')
  }

  const hours = minutes / 60

  return hours < 24 ? copy.ttlOption(hours, 'hours') : copy.ttlOption(hours / 24, 'days')
}

/**
 * #67 / D29 "send to: <chat>" — the composer control beside the model picker. Picks a target from
 * the `/to` list (this backend's chats minus this one); while one is set the pill wears the active
 * toggle style, names the chat, and Send forwards the draft there through the owner-forward path
 * (use-composer-submit) instead of posting a turn here. The × beside it clears the target.
 *
 * Per-chat: keyed by THIS surface's stored session, so tiles each keep their own. Hidden without the
 * desktop bridge (no native confirm) or before the chat has a stored session. With `minimal` (a
 * narrow tile) it shows only while a target is set, so a hidden target never re-routes Send.
 */
export function ForwardTargetPill({
  compact = false,
  disabled,
  minimal = false
}: {
  compact?: boolean
  disabled: boolean
  minimal?: boolean
}) {
  const { t } = useI18n()
  const copy = t.ownerForward
  const storedId = useStore(useSessionView().$storedId)
  const targets = useStore($composerForwardTargets)
  const sessions = useStore($sessions)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const target = storedId ? (targets[storedId] ?? null) : null

  // `sessions` re-derives the list when chats load or rename while the menu is open.
  const candidates = useMemo(
    () => (open && storedId && sessions ? forwardCandidates(storedId) : []),
    [open, sessions, storedId]
  )

  if (!storedId || !window.hermesDesktop?.ownerForward || (minimal && !target)) {
    return null
  }

  const title = target ? target.title || target.session_id.slice(0, 8) : ''
  const label = target ? copy.sendToTitle(title) : copy.sendTo
  const q = query.trim().toLowerCase()

  const shown = (
    q
      ? candidates.filter(c => (c.title ?? '').toLowerCase().includes(q) || c.session_id.toLowerCase().includes(q))
      : candidates
  ).slice(0, MAX_LISTED)

  const setMenuOpen = (next: boolean) => {
    setOpen(next)

    if (!next) {
      setQuery('')
      releaseTypingFocus()
    }
  }

  const clear = () => {
    triggerHaptic('close')
    setComposerForwardTarget(storedId, null)
  }

  return (
    <>
      <DropdownMenu onOpenChange={setMenuOpen} open={open}>
        <Tip label={label} side="top">
          <DropdownMenuTrigger asChild>
            <Button
              aria-label={label}
              className={cn(
                PILL,
                compact && !target && 'size-(--composer-control-size) justify-center p-0',
                target && ACTIVE_ICON_BTN
              )}
              data-testid="forward-target-pill"
              disabled={disabled}
              type="button"
              variant="ghost"
            >
              <Send className="size-3.5 shrink-0" />
              {target && <span className="truncate">{title}</span>}
              {!compact && <ChevronDown className="size-2.5 shrink-0 opacity-50" />}
            </Button>
          </DropdownMenuTrigger>
        </Tip>
        <DropdownMenuContent align="start" className="w-64 p-0" side="top" sideOffset={8}>
          <DropdownMenuSearch onValueChange={setQuery} placeholder={copy.searchTargets} value={query} />
          <DropdownMenuSeparator className="mx-0 my-0" />
          <DropdownMenuLabel className={dropdownMenuSectionLabel}>{copy.targets}</DropdownMenuLabel>
          <div className="max-h-60 overflow-y-auto pb-1">
            {shown.length === 0 ? (
              <p className="px-2.5 py-1 text-xs text-muted-foreground">{copy.noTargets}</p>
            ) : (
              shown.map(c => {
                const selected = target?.session_id === c.session_id && target.profile === c.profile

                return (
                  <DropdownMenuItem
                    className={cn(dropdownMenuRow, selected && 'text-foreground')}
                    key={`${c.profile}:${c.session_id}`}
                    onSelect={() => {
                      triggerHaptic('selection')
                      setComposerForwardTarget(storedId, c)
                    }}
                  >
                    <span className="truncate">{c.title || c.session_id}</span>
                    <span className="ml-auto shrink-0 font-mono text-[0.6875rem] text-muted-foreground">
                      {c.profile !== 'default' ? `${c.profile} · ` : ''}
                      {c.session_id.slice(0, 8)}
                    </span>
                  </DropdownMenuItem>
                )
              })
            )}
          </div>
          {target && (
            <>
              <DropdownMenuSeparator className="mx-0 my-0" />
              <DropdownMenuSub>
                <DropdownMenuSubTrigger className={dropdownMenuRow}>
                  {copy.ttlSummary(ttlLabel(target.ttlMs, copy))}
                </DropdownMenuSubTrigger>
                <DropdownMenuSubContent>
                  <DropdownMenuRadioGroup
                    onValueChange={value => setComposerForwardTtl(storedId, Number(value))}
                    value={String(target.ttlMs)}
                  >
                    {COMPOSER_FORWARD_TTL_OPTIONS.map(value => (
                      <DropdownMenuRadioItem className={dropdownMenuRow} key={value} value={String(value)}>
                        {ttlLabel(value, copy)}
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuSubContent>
              </DropdownMenuSub>
              <DropdownMenuItem className={cn(dropdownMenuRow, 'mb-1')} onSelect={clear}>
                {copy.sendHere}
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      {target && (
        <Tip label={copy.sendHere} side="top">
          <Button
            aria-label={copy.sendHere}
            className={cn(GHOST_ICON_BTN, 'p-0')}
            disabled={disabled}
            onClick={clear}
            size="icon"
            type="button"
            variant="ghost"
          >
            <X className="size-3.5" />
          </Button>
        </Tip>
      )}
    </>
  )
}
