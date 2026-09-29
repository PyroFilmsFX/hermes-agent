import { useStore } from '@nanostores/react'
import { type MouseEvent, useContext, useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { $forwardReceipts, type ForwardOutcome, forwardProfile, sendOwnerForward } from '@/lib/owner-forward/client'
import { $ownerGrantStatus, ensureOwnerGrantStatus, forwardServiceState } from '@/lib/owner-forward/service'
import {
  parseStageQuestionDirectives,
  STAGE_QUESTION_SCOPE,
  type StageQuestionDirective,
  type StageQuestionOption,
  type StageQuestionProblem,
  stageQuestionSubject
} from '@/lib/owner-forward/stage-question-directive'
import { isTrustedGesture } from '@/lib/owner-forward/trusted'
import { $sessions } from '@/store/session'

import { type SendToOrigin, SendToOriginContext, SendToSourceContext } from './send-to-block'

/**
 * b9 §5 stage-question copy. English only for now: the i18n catalog belongs to the scope lane; move
 * these into `t.ownerForward` when that lands. Shared reasons reuse the `:::send-to` strings.
 */
export const STAGE_QUESTION_COPY = {
  hashMismatch: "This question doesn't match its question_sha256, so it can't be answered here.",
  problem: {
    bad_hash_attr: 'This question has no valid question_sha256.',
    bad_scope: `This question names an unsupported scope (only ${STAGE_QUESTION_SCOPE}).`,
    duplicate_attr: 'This question repeats an attribute.',
    no_options: 'This question lists no numbered options.',
    no_question: 'This question has no question text.',
    option_numbering: 'The options must be numbered 1, 2, 3… in order.',
    text_after_options: 'Only numbered options may follow the first option.',
    too_long: 'This question is too long.',
    too_many_options: 'This question lists too many options.'
  } satisfies Record<StageQuestionProblem, string>,
  assistantOnly: 'Only a question in the assistant’s own reply can be answered.',
  answered: (index: number) => `Answered: option ${index} ✓`,
  failed: (message: string) => `The answer wasn't sent: ${message}`,
  signedText: (index: number, label: string) => `Stage question answer: option ${index}. ${label}`
} as const

export function stageQuestionReceiptKey(
  origin: SendToOrigin,
  directive: StageQuestionDirective,
  option: number
): string {
  return `stage-question:${origin.session_id ?? ''}:${origin.messageKey}:${directive.index}:${directive.sha256}:${option}`
}

function answeredOption(
  receipts: Record<string, ForwardOutcome>,
  origin: SendToOrigin,
  directive: StageQuestionDirective
): null | number {
  for (const option of directive.options) {
    const receipt = receipts[stageQuestionReceiptKey(origin, directive, option.index)]

    if (
      receipt?.kind === 'sent' &&
      (receipt.results.length === 0 || receipt.results.some(line => !line.status.startsWith('failed')))
    ) {
      return option.index
    }
  }

  return null
}

/**
 * b9 §5: the conductor's question plus one button per option (1-based). A trusted click signs the
 * answer through the existing composer_signed self-target path, with scope
 * `conductor:answer:stage-variant` and subject `<question_sha256>:<option_index>`; main's native
 * confirm is still the owner's deliberate act. The buttons stay disabled unless the recomputed hash
 * equals the block's `question_sha256`.
 */
export function StageQuestionBlock({
  index,
  inert = false,
  previewOnly = false,
  streaming = false
}: {
  index: number
  inert?: boolean
  previewOnly?: boolean
  streaming?: boolean
}) {
  const source = useContext(SendToSourceContext)
  const origin = useContext(SendToOriginContext)
  const directive = useMemo(() => parseStageQuestionDirectives(source)[index] ?? null, [source, index])

  if (!directive) {
    return null
  }

  if (previewOnly) {
    return <p className="whitespace-pre-wrap">{directive.body}</p>
  }

  const live = origin !== null && !inert

  return (
    <div className="my-1.5 grid gap-1.5" data-slot="stage-question-directive">
      <p className="whitespace-pre-wrap wrap-anywhere text-foreground" data-slot="stage-question-text" dir="auto">
        {directive.question}
      </p>
      {live ? (
        <StageQuestionActions directive={directive} origin={origin} streaming={streaming} />
      ) : (
        <ol className="grid gap-0.5 text-sm" data-slot="stage-question-options">
          {directive.options.map(option => (
            <li dir="auto" key={option.index}>
              {option.index}. {option.label}
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

function StageQuestionActions({
  directive,
  origin,
  streaming
}: {
  directive: StageQuestionDirective
  origin: SendToOrigin
  streaming: boolean
}) {
  const { t } = useI18n()
  const copy = t.ownerForward
  const sessions = useStore($sessions)
  const receipts = useStore($forwardReceipts)
  const grant = useStore($ownerGrantStatus)
  const [pending, setPending] = useState<null | number>(null)
  const [error, setError] = useState<null | string>(null)

  useEffect(() => {
    ensureOwnerGrantStatus()
  }, [])

  const service = forwardServiceState(grant, Boolean(window.hermesDesktop?.ownerForward))
  const answered = answeredOption(receipts, origin, directive)

  let reason: null | string = null

  if (streaming) {
    reason = copy.sendToStreaming
  } else if (!directive.closed) {
    reason = copy.sendToUnclosed
  } else if (directive.problem) {
    reason = STAGE_QUESTION_COPY.problem[directive.problem]
  } else if (!directive.hashMatches) {
    reason = STAGE_QUESTION_COPY.hashMismatch
  } else if (origin.role !== 'assistant') {
    reason = STAGE_QUESTION_COPY.assistantOnly
  } else if (!origin.session_id) {
    reason = copy.sendToUnsaved
  } else if (service === 'checking') {
    reason = copy.sendToChecking
  } else if (service === 'off') {
    reason = copy.sendToServiceOff
  } else if (service === 'unavailable') {
    reason = copy.sendToServiceUnavailable
  }

  const answer = (option: StageQuestionOption, event: MouseEvent) => {
    const sessionId = origin.session_id

    if (reason || answered !== null || pending !== null || !sessionId || !isTrustedGesture(event.nativeEvent)) {
      return
    }

    const row = sessions.find(s => s.id === sessionId)

    setPending(option.index)
    setError(null)
    // The existing ⌘⇧↩ path: a composer_signed decision that targets only this chat.
    void sendOwnerForward({
      text: STAGE_QUESTION_COPY.signedText(option.index, option.label),
      gesture: 'composer_signed',
      origin: { session_id: sessionId, message_id: null, role: 'user' },
      targets: [{ profile: row?.profile || forwardProfile(), session_id: sessionId, title: row?.title ?? null }],
      scope: [STAGE_QUESTION_SCOPE],
      subject: stageQuestionSubject(directive.sha256, option.index),
      receiptKey: stageQuestionReceiptKey(origin, directive, option.index)
    })
      .then(outcome => {
        if (outcome.kind === 'error') {
          setError(STAGE_QUESTION_COPY.failed(outcome.message))
        }
      })
      .finally(() => setPending(null))
  }

  if (answered !== null) {
    return (
      <span className="text-xs font-medium text-muted-foreground" data-slot="stage-question-answered">
        {STAGE_QUESTION_COPY.answered(answered)}
      </span>
    )
  }

  return (
    <div className="grid gap-1">
      <div className="flex flex-wrap items-center gap-2" data-slot="stage-question-options">
        {directive.options.map(option => (
          <Button
            className="w-fit max-w-full whitespace-normal text-start"
            disabled={reason !== null || pending !== null}
            key={option.index}
            onClick={event => answer(option, event)}
            size="sm"
            type="button"
            variant="outline"
          >
            {option.index}. {option.label}
          </Button>
        ))}
      </div>
      {reason && (
        <span className="text-xs text-muted-foreground" data-slot="stage-question-reason">
          {reason}
        </span>
      )}
      {error && (
        <span className="text-xs text-destructive" data-slot="stage-question-error">
          {error}
        </span>
      )}
    </div>
  )
}
