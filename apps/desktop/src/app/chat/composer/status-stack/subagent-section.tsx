import { useState } from 'react'

import { SubagentRow } from '@/app/agents'
import { ActivityTimerText } from '@/components/chat/activity-timer-text'
import { StatusRow } from '@/components/chat/status-row'
import { StatusSection } from '@/components/chat/status-section'
import { Codicon } from '@/components/ui/codicon'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { useViewedInterval } from '@/hooks/use-viewed-interval'
import { useI18n } from '@/i18n'
import { useSessionSlice, useStoreSelector } from '@/lib/use-session-slice'
import { $sessionStates } from '@/store/session-states'
import { $subagentsBySession, subagentIdentity, type SubagentProgress } from '@/store/subagents'

import { SubagentControls } from './subagent-controls'
import { SubagentTranscript } from './subagent-transcript'

interface SubagentSectionProps {
  sessionId: string
}

/** A composer-local roster: never borrow the global Agents panel's scope. */
export function SubagentSection({ sessionId }: SubagentSectionProps) {
  const { t } = useI18n()
  const items = useSessionSlice($subagentsBySession, sessionId)
  const turnLive = useStoreSelector($sessionStates, states => {
    const state = states[sessionId]

    return Boolean(state && (state.busy || state.awaitingResponse || state.turnLive))
  })
  const live = items.filter(item => item.status === 'running' || item.status === 'queued')
  const visible = items.filter(
    item =>
      item.status === 'running' ||
      item.status === 'queued' ||
      (turnLive && ['completed', 'failed', 'interrupted'].includes(item.status))
  )
  const [nowMs, setNowMs] = useState(Date.now)
  const [selected, setSelected] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const hasLive = live.length > 0

  useViewedInterval(() => setNowMs(Date.now()), 1000, hasLive)

  if (visible.length === 0) {
    return null
  }

  const row = (item: SubagentProgress) => (
    <StatusRow
      expanded={selected === item.id}
      key={item.id}
      leading={
        item.status === 'running' || item.status === 'queued' ? (
          <GlyphSpinner
            ariaLabel={item.status === 'queued' ? t.agents.queued : t.agents.running}
            className="text-(--ui-purple)"
            spinner="braille"
          />
        ) : (
          <Codicon
            aria-label={item.status === 'completed' ? t.agents.done : item.status}
            className={item.status === 'completed' ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-tertiary)'}
            name={item.status === 'completed' ? 'check' : 'circle-slash'}
            size="0.75rem"
          />
        )
      }
      onActivate={() => setSelected(selected === item.id ? null : item.id)}
      trailing={
        item.status === 'running' || item.status === 'queued' ? (
          <ActivityTimerText
            className="shrink-0 text-[0.65rem]"
            seconds={Math.max(0, Math.floor((nowMs - item.startedAt) / 1000))}
          />
        ) : (
          <span className="shrink-0 text-[0.65rem] text-(--ui-text-tertiary)">{t.agents.done}</span>
        )
      }
      trailingVisible
    >
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs text-(--ui-text-primary)">{item.goal}</span>
        {subagentIdentity(item) && (
          <span className="block truncate text-[0.68rem] text-(--ui-text-secondary)">{subagentIdentity(item)}</span>
        )}
        <span className="block truncate text-[0.68rem] text-(--ui-text-tertiary)">
          {item.stream.at(-1)?.text || (item.status === 'queued' ? t.agents.queued : item.status === 'running' ? t.agents.waitingActivity : t.agents.done)}
        </span>
      </span>
    </StatusRow>
  )

  const detail = live.find(item => item.id === selected)

  return (
    <div className="composer-no-drag min-w-0" data-slot="composer-subagents">
      <StatusSection
        collapsedIndicator={
          live.length > 0 ? (
            <GlyphSpinner
              ariaLabel={live.some(item => item.status === 'running') ? t.agents.running : t.agents.queued}
              className="text-(--ui-purple)"
              spinner="braille"
            />
          ) : (
            <Codicon aria-label={t.agents.done} className="text-(--ui-text-secondary)" name="check" size="0.75rem" />
          )
        }
        icon={<Codicon className="text-(--ui-purple)" name="agent" size="0.8rem" />}
        label={t.statusStack.subagents(visible.length)}
      >
        <div className="max-h-[25vh] overflow-y-auto overscroll-y-auto">{visible.map(row)}</div>
        {detail && (
          <div
            className="status-subagent-detail max-h-[25vh] overflow-y-auto overscroll-y-auto pr-3 py-2"
            data-slot="composer-subagent-detail"
          >
            <SubagentControls
              key={`${sessionId}:${detail.id}`}
              sessionId={sessionId}
              setText={text => setDrafts(previous => ({ ...previous, [detail.id]: text }))}
              subagentId={detail.id}
              text={drafts[detail.id] ?? ''}
            />
            <SubagentRow node={{ ...detail, children: [] }} nowMs={nowMs} />
            <SubagentTranscript key={`tail:${sessionId}:${detail.id}`} sessionId={sessionId} subagentId={detail.id} />
          </div>
        )}
      </StatusSection>
    </div>
  )
}
