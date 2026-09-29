import type { ReactNode } from 'react'

import type { ConductorRow as ConductorRowData } from '@/api/conductors'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { CONDUCTORS_PIECE } from './conductors-layout'
import { clockOf, dueDayOf, gateKindLabel, isoToMs } from './conductors-model'

function Section({ children, id, title }: { children: ReactNode; id: string; title: string }) {
  return (
    <section className="flex min-w-0 flex-col gap-0.5" data-detail={id}>
      <h4 className="text-[0.625rem] font-medium tracking-wide text-(--ui-text-tertiary) uppercase">{title}</h4>
      {children}
    </section>
  )
}

function Lines({ empty, lines }: { empty: string; lines: { key: string; node: ReactNode }[] }) {
  if (lines.length === 0) {
    return <span className="text-(--ui-text-quaternary)">{empty}</span>
  }

  return (
    <ul className="flex min-w-0 flex-col gap-0.5">
      {lines.map(line => (
        <li className="min-w-0 break-words" key={line.key}>
          {line.node}
        </li>
      ))}
    </ul>
  )
}

const join = (parts: (false | null | string | undefined)[]) => parts.filter(Boolean).join(' · ')

/**
 * The expanded row (§8): everything the one-line row had to truncate — the
 * current units with seat and since, the remaining waves, every gate with its
 * dates, owner blockers, refusals by seat, lanes, other builds on the same
 * project, and how the row was attributed to its session. Static text only:
 * no timers, no requests.
 */
export function ConductorRowDetail({ row }: { row: ConductorRowData }) {
  const { t } = useI18n()
  const c = t.conductors
  const { build, orchestrator, record } = row
  const none = c.detailNone

  const units = build.current_units.map((unit, index) => {
    const since = clockOf(isoToMs(unit.since))

    return {
      key: unit.id || String(index),
      node: (
        <>
          <span className="font-mono text-(--ui-text-primary)">{unit.id}</span>{' '}
          {join([unit.title, unit.seat, since && c.gateSince(since)])}
        </>
      )
    }
  })

  const waves = build.remaining_waves.map(wave => ({
    key: wave.id || String(wave.index),
    node: join([`W${wave.index}`, wave.title, c.detailUnits(wave.units)])
  }))

  const gates = build.gates.map((gate, index) => {
    const since = clockOf(isoToMs(gate.since))
    const due = dueDayOf(isoToMs(gate.due))

    return {
      key: gate.id || String(index),
      node: (
        <span className={cn(gate.kind === 'ci' && gate.state === 'failed' && 'text-(--ui-red)')}>
          {join([gateKindLabel(gate.kind), gate.label, since && c.gateSince(since), due && c.gateDue(due), gate.state])}
        </span>
      )
    }
  })

  const blockers = build.owner_blockers.map((blocker, index) => {
    const since = clockOf(isoToMs(blocker.since))

    return {
      key: blocker.id || String(index),
      node: (
        <span className="text-(--ui-yellow)">
          {join([blocker.label || blocker.action, since && c.gateSince(since)])}
        </span>
      )
    }
  })

  const refusalsBySeat = new Map<string, string[]>()

  for (const refusal of build.refusals) {
    const lines = refusalsBySeat.get(refusal.seat) ?? []
    lines.push(`${refusal.code} × ${refusal.count}${refusal.note ? ` — ${refusal.note}` : ''}`)
    refusalsBySeat.set(refusal.seat, lines)
  }

  const refusals = [...refusalsBySeat].map(([seat, lines]) => ({
    key: seat,
    node: (
      <span>
        <span className="font-mono text-(--ui-yellow)">{seat}</span> {lines.join('; ')}
      </span>
    )
  }))

  const others = row.other_builds.map(other => ({
    key: other.run_id,
    node: join([
      other.plan_title || other.run_id,
      other.waves.total > 0 ? `W${other.waves.current}/${other.waves.total}` : null,
      c.liveness[other.liveness]
    ])
  }))

  const hint = record.reason === 'mismatched' || record.reason === 'newer_schema' ? c.recordHint[record.reason] : null

  return (
    <div
      className="flex flex-col gap-2 py-2 pr-3 pl-8 text-[0.7rem] text-(--ui-text-secondary)"
      data-slot="conductor-row-detail"
    >
      {hint && (
        <p className="text-(--ui-yellow)" data-detail="record-hint">
          {hint}
        </p>
      )}
      <div className={cn('grid gap-x-6 gap-y-3', CONDUCTORS_PIECE.detailGrid)}>
        <Section id="units" title={c.detail.currentUnits}>
          <Lines empty={build.phase || none} lines={units} />
        </Section>
        <Section id="waves" title={c.detail.remainingWaves}>
          <Lines empty={none} lines={waves} />
        </Section>
        <Section id="gates" title={c.detail.gates}>
          <Lines empty={none} lines={gates} />
        </Section>
        <Section id="blockers" title={c.detail.blockers}>
          <Lines empty={none} lines={blockers} />
        </Section>
        <Section id="refusals" title={c.detail.refusals}>
          <Lines empty={none} lines={refusals} />
        </Section>
        <Section id="lanes" title={c.detail.lanes}>
          <span>{c.detailLanes(build.lanes.running, build.lanes.stale, build.lanes.cap)}</span>
        </Section>
        <Section id="other-builds" title={c.detail.otherBuilds}>
          <Lines empty={none} lines={others} />
        </Section>
        <Section id="attribution" title={c.detail.attribution}>
          <span>
            {join([
              c.attribution[orchestrator.attribution],
              orchestrator.profile && orchestrator.profile !== 'default' ? orchestrator.profile : null,
              c.ownerLive[orchestrator.live]
            ])}
          </span>
          <span className="font-mono text-[0.65rem] text-(--ui-text-tertiary)">
            {join([build.run_id, orchestrator.claude_sid_short])}
          </span>
        </Section>
      </div>
    </div>
  )
}
