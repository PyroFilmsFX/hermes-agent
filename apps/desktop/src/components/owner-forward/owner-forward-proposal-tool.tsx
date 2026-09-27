import { useStore } from '@nanostores/react'
import { type FC, type MouseEvent, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { ToolFallback } from '@/components/assistant-ui/tool/fallback'
import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import {
  $forwardReceipts,
  describeForwardResult,
  type ForwardOrigin,
  type ForwardTarget,
  openForwardSheet
} from '@/lib/owner-forward/client'
import { MAX_FORWARD_TARGETS } from '@/lib/owner-forward/parse-to'
import { SCOPE_RE, scopeLabel } from '@/lib/owner-forward/scopes'
import { $sessions } from '@/store/session'

/** Strict names only (F7): Hermes' own tool, directly or through the hermes-tools MCP server. A
 *  third-party MCP server exposing the same bare name never renders a card. */
const PROPOSE_NAMES = new Set(['owner_forward_propose', 'mcp__hermes-tools__owner_forward_propose'])

export function isOwnerForwardProposeName(name: unknown): boolean {
  return typeof name === 'string' && PROPOSE_NAMES.has(name)
}

export interface OwnerForwardProposal {
  proposalId: string
  text: string
  targets: ForwardTarget[]
  scope: string[]
  subject: null | string
}

interface ToolPartLike {
  toolName?: unknown
  args?: unknown
  result?: unknown
  isError?: unknown
}

const SESSION_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/

/**
 * A card only for a completed, successful `owner_forward_propose` part whose result carries the
 * marker the tool itself writes, and whose args text is the text the tool validated (UTF-8 length
 * matches `text_len`). Anything else is null, and the caller renders the normal fallback.
 */
export function proposalFromToolPart(part: ToolPartLike): OwnerForwardProposal | null {
  if (!isOwnerForwardProposeName(part.toolName) || part.isError === true || part.result === undefined) {
    return null
  }

  let result: Record<string, unknown> | null = null

  try {
    result = typeof part.result === 'string' ? JSON.parse(part.result) : (part.result as Record<string, unknown>)
  } catch {
    return null
  }

  if (
    !result ||
    typeof result !== 'object' ||
    result.owner_forward_proposal !== 1 ||
    result.status !== 'awaiting_owner' ||
    typeof result.proposal_id !== 'string' ||
    !result.proposal_id
  ) {
    return null
  }

  const args = (part.args ?? {}) as Record<string, unknown>
  const text = args.text

  if (typeof text !== 'string' || !text.trim() || new TextEncoder().encode(text).length !== result.text_len) {
    return null
  }

  const rawTargets = Array.isArray(result.targets) ? (result.targets as Array<Record<string, unknown>>) : []

  const targets = rawTargets
    .filter(t => t && typeof t.profile === 'string' && PROFILE_RE.test(t.profile) && typeof t.session_id === 'string' && SESSION_RE.test(t.session_id))
    .map(t => ({ profile: t.profile as string, session_id: t.session_id as string }))

  if (targets.length === 0 || targets.length !== rawTargets.length || targets.length > MAX_FORWARD_TARGETS) {
    return null
  }

  const scope = (Array.isArray(result.scopes) ? (result.scopes as Array<Record<string, unknown>>) : [])
    .map(s => (s && typeof s.scope === 'string' ? s.scope : ''))
    .filter(s => SCOPE_RE.test(s))

  return {
    proposalId: result.proposal_id,
    text,
    targets,
    scope,
    subject: typeof result.subject === 'string' && result.subject.trim() ? result.subject : null
  }
}

/**
 * #60 manager proposal card (design §4.3): app chrome, never an iframe, never styled by model markup.
 * It NEVER sends: "Review & send…" opens the Forward sheet prefilled (trusted click only), and the
 * sheet's Send… plus main's native confirm are the two deliberate acts.
 */
export function OwnerForwardProposalCard({ origin, proposal }: { origin: ForwardOrigin | null; proposal: OwnerForwardProposal }) {
  const { t } = useI18n()
  const copy = t.ownerForward
  const sessions = useStore($sessions)
  const receipts = useStore($forwardReceipts)
  const [dismissed, setDismissed] = useState(false)
  const receipt = receipts[proposal.proposalId]

  const targets = proposal.targets.map(target => ({
    ...target,
    title: sessions.find(s => s.id === target.session_id)?.title ?? null
  }))

  const names = targets.map(target => target.title || target.session_id.slice(0, 8)).join(', ')

  const review = (event: MouseEvent) => {
    if (!origin) {
      return
    }

    openForwardSheet(
      {
        text: proposal.text,
        gesture: 'proposal',
        origin,
        targets,
        scope: proposal.scope,
        subject: proposal.subject,
        proposalId: proposal.proposalId
      },
      event.nativeEvent
    )
  }

  return (
    <div className="my-1.5 grid gap-1.5 rounded-lg border bg-card p-2.5 text-sm" data-slot="owner-forward-proposal">
      <div className="font-medium">{copy.proposalTitle(names)}</div>
      <p className="line-clamp-4 whitespace-pre-wrap text-muted-foreground">{proposal.text}</p>
      {proposal.scope.length > 0 && (
        <ul className="text-xs text-muted-foreground">
          {proposal.scope.map(scope => (
            <li key={scope}>{scopeLabel(scope)}</li>
          ))}
        </ul>
      )}
      {receipt?.kind === 'sent' ? (
        <ul className="text-xs">
          {receipt.results.map(line => (
            <li key={`${line.target}-${line.status}`}>{describeForwardResult(line, copy)}</li>
          ))}
        </ul>
      ) : dismissed ? (
        <span className="text-xs text-muted-foreground">{copy.dismissed}</span>
      ) : (
        <div className="flex gap-1.5">
          <Button disabled={!origin} onClick={review} size="sm" type="button">
            {copy.reviewSend}
          </Button>
          <Button onClick={() => setDismissed(true)} size="sm" type="button" variant="ghost">
            {copy.dismiss}
          </Button>
        </div>
      )}
    </div>
  )
}

type ToolProps = Parameters<typeof ToolFallback>[0]

export const OwnerForwardProposalTool: FC<ToolProps> = props => {
  const storedId = useStore(useSessionView().$storedId)
  const proposal = proposalFromToolPart(props as ToolPartLike)

  if (!proposal) {
    return <ToolFallback {...props} />
  }

  return (
    <OwnerForwardProposalCard
      origin={storedId ? { session_id: storedId, message_id: null, role: 'assistant' } : null}
      proposal={proposal}
    />
  )
}
