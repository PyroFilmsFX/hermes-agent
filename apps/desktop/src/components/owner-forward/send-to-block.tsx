import { useStore } from '@nanostores/react'
import { createContext, type MouseEvent, useContext, useEffect, useMemo } from 'react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { Send } from '@/lib/icons'
import {
  $forwardReceipts,
  describeForwardResult,
  forwardCandidates,
  type ForwardTarget,
  openForwardSheet
} from '@/lib/owner-forward/client'
import {
  parseSendToDirectives,
  resolveSendToTarget,
  type SendToDirective,
  sendToTargetName
} from '@/lib/owner-forward/send-to-directive'
import { $ownerGrantStatus, ensureOwnerGrantStatus, forwardServiceState } from '@/lib/owner-forward/service'
import { sha256Hex } from '@/lib/owner-forward/sha256'
import { $sessions } from '@/store/session'

/**
 * Where a `:::send-to` block may be live. Only message surfaces the model or a peer wrote provide it
 * (assistant text parts, peer and system results); everything else (the owner's own bubbles, bot
 * rooms, reasoning, previews) renders the quote without a button.
 */
export interface SendToOrigin {
  /** The stored chat the block sits in; null before the chat is saved. */
  session_id: null | string
  /** The durable row id when the surface has exactly one (peer rows); null for assistant turns. */
  message_id: null | string
  role: 'assistant' | 'peer' | null
  /** Identifies the rendering message, so the sent state belongs to that message only. */
  messageKey: string
}

const SendToOriginContext = createContext<null | SendToOrigin>(null)
const SendToSourceContext = createContext<string>('')

export const SendToOriginProvider = SendToOriginContext.Provider

/** The raw text of the markdown surface. The block re-parses it, so what it shows and sends is the
 *  directive body as written, never the rendered markdown. */
export const SendToSourceProvider = SendToSourceContext.Provider

export function sendToReceiptKey(origin: SendToOrigin, index: number, target: ForwardTarget, body: string): string {
  const digest = sha256Hex(`${target.profile}:${target.session_id}\n${body}`).slice(0, 16)

  return `send-to:${origin.session_id ?? ''}:${origin.messageKey}:${index}:${digest}`
}

/**
 * D33: the body as a quote, plus "Send to <session>". The button only OPENS the Forward sheet,
 * prefilled with the exact body and the one resolved target, and only on a trusted click; the
 * sheet's Send… and main's native confirm do the rest (masking before signing, TTL picker).
 */
export function SendToBlock({
  index,
  inert = false,
  previewOnly = false,
  streaming = false
}: {
  index: number
  /** Reasoning and other scratch surfaces: never live, whatever the context says. */
  inert?: boolean
  previewOnly?: boolean
  streaming?: boolean
}) {
  const { t } = useI18n()
  const source = useContext(SendToSourceContext)
  const origin = useContext(SendToOriginContext)
  const directive = useMemo(() => parseSendToDirectives(source)[index] ?? null, [source, index])

  if (!directive) {
    return null
  }

  if (previewOnly) {
    return <p className="whitespace-pre-wrap">{directive.body}</p>
  }

  const name = directive.session ? sendToTargetName(directive.session) : ''
  const live = origin !== null && !inert

  return (
    <div className="my-1.5 grid gap-1.5" data-slot="send-to-directive">
      <blockquote
        className="border-s-2 border-(--ui-stroke-tertiary) ps-3 whitespace-pre-wrap wrap-anywhere text-foreground"
        dir="auto"
      >
        {directive.body}
      </blockquote>
      {live ? (
        <SendToAction directive={directive} name={name} origin={origin} streaming={streaming} />
      ) : (
        name && <span className="text-xs text-muted-foreground">{t.ownerForward.sendToFor(name)}</span>
      )}
    </div>
  )
}

function SendToAction({
  directive,
  name,
  origin,
  streaming
}: {
  directive: SendToDirective
  name: string
  origin: SendToOrigin
  streaming: boolean
}) {
  const { t } = useI18n()
  const copy = t.ownerForward
  // Re-resolve the target when chats load or get renamed (forwardCandidates reads the store).
  useStore($sessions)
  const receipts = useStore($forwardReceipts)
  const grant = useStore($ownerGrantStatus)

  useEffect(() => {
    ensureOwnerGrantStatus()
  }, [])

  const resolution = directive.session
    ? resolveSendToTarget(directive.session, forwardCandidates(origin.session_id))
    : null

  const target = resolution?.status === 'one' ? resolution.target : null
  const receiptKey = target ? sendToReceiptKey(origin, directive.index, target, directive.body) : null
  const receipt = receiptKey ? receipts[receiptKey] : undefined
  const service = forwardServiceState(grant, Boolean(window.hermesDesktop?.ownerForward))

  const sent =
    receipt?.kind === 'sent' &&
    (receipt.results.length === 0 || receipt.results.some(line => !line.status.startsWith('failed')))

  let reason: null | string = null

  if (streaming) {
    reason = copy.sendToStreaming
  } else if (!directive.closed) {
    reason = copy.sendToUnclosed
  } else if (!directive.session || !name) {
    reason = copy.sendToNoTarget
  } else if (!directive.body) {
    reason = copy.sendToEmpty
  } else if (resolution?.status === 'none') {
    reason = copy.sendToNoSession(name)
  } else if (resolution?.status === 'many') {
    reason = copy.sendToManySessions(resolution.count, name)
  } else if (!origin.session_id) {
    reason = copy.sendToUnsaved
  } else if (service === 'checking') {
    reason = copy.sendToChecking
  } else if (service === 'off') {
    reason = copy.sendToServiceOff
  } else if (service === 'unavailable') {
    reason = copy.sendToServiceUnavailable
  }

  const onClick = (event: MouseEvent) => {
    if (reason || !target || !receiptKey || !origin.session_id) {
      return
    }

    // Opens the sheet only (trusted click, checked inside); nothing is sent from here.
    openForwardSheet(
      {
        text: directive.body,
        gesture: 'proposal',
        origin: { session_id: origin.session_id, message_id: origin.message_id, role: origin.role },
        targets: [{ profile: target.profile, session_id: target.session_id, title: target.title }],
        receiptKey
      },
      event.nativeEvent
    )
  }

  const notes = receipt?.kind === 'sent' ? receipt.results.filter(line => line.status !== 'delivered') : []

  return (
    <div className="grid gap-1">
      {sent ? (
        <span className="text-xs font-medium text-muted-foreground" data-slot="send-to-sent">
          {copy.sendToSent}
        </span>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            className="w-fit"
            disabled={reason !== null}
            onClick={onClick}
            size="sm"
            title={reason ?? undefined}
            type="button"
            variant="outline"
          >
            <Send className="size-3.5" />
            {copy.sendToButton(target?.title || name || '…')}
          </Button>
          {reason && (
            <span className="text-xs text-muted-foreground" data-slot="send-to-reason">
              {reason}
            </span>
          )}
        </div>
      )}
      {notes.length > 0 && (
        <ul className="text-xs text-muted-foreground">
          {notes.map(line => (
            <li key={`${line.target}-${line.status}`}>{describeForwardResult(line, copy)}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
