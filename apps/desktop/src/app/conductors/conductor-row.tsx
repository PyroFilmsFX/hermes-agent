import { useStore } from '@nanostores/react'
import { type KeyboardEvent, memo, type MouseEvent, type ReactNode } from 'react'

import type { ConductorRow as ConductorRowData } from '@/api/conductors'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { formatAgo } from '@/lib/time'
import { cn } from '@/lib/utils'
import { $conductorsNow } from '@/store/conductors'

import {
  chipStateOf,
  clockOf,
  type ConductorChipState,
  type DerivableColumn,
  dueDayOf,
  formatHours,
  gateKindLabel,
  isDerivedColumn,
  isoToMs,
  NATIVE_SEATS,
  SEAT_ORDER
} from './conductors-model'

/**
 * Wide layout (≥ 1100 px) column track, shared by the header row and every
 * data row so the columns line up without a table element. Order is §8's:
 * project · session · now · remaining · estimate+gates · seats · lanes ·
 * CI/PR · owner blockers · last activity + liveness.
 */
export const CONDUCTORS_GRID_TRACK =
  'grid grid-cols-[minmax(9rem,1.2fr)_minmax(8rem,1.1fr)_minmax(10rem,1.6fr)_6.75rem_7.25rem_9.5rem_3.25rem_5.5rem_minmax(7rem,1fr)_8.5rem] gap-x-3'

/** Tests hook a counter here to prove unchanged rows skip rendering. */
export const conductorRowRenderProbe: { current: ((key: string) => void) | null } = { current: null }

// ── Small leaves ───────────────────────────────────────────────────────────

/** The only piece of a row that follows the shared 30 s clock, so a tick
 *  re-renders a text node per row, not the row. No timers of its own. */
function Ago({ atSec }: { atSec: number }) {
  const { t } = useI18n()
  const now = useStore($conductorsNow)

  if (!atSec) {
    return <span>{t.conductors.none}</span>
  }

  return <span>{formatAgo(atSec * 1000, t.conductors, now)}</span>
}

/** `≈` provenance mark (§3.6): Hermes derived the value next to it. */
function DerivedMark({ hint }: { hint: string }) {
  return (
    <Tip label={hint}>
      <span
        aria-label={hint}
        className="ml-1 shrink-0 cursor-default text-[0.68rem] text-(--ui-text-quaternary)"
        data-derived=""
        role="img"
      >
        ≈
      </span>
    </Tip>
  )
}

const DOT_CLASS: Record<ConductorChipState, string> = {
  active: 'bg-(--ui-text-secondary)',
  quiet: 'bg-(--ui-text-quaternary)',
  idle: 'border border-(--ui-text-tertiary)',
  stale: 'bg-(--ui-yellow)',
  abandoned: 'border border-(--ui-text-quaternary)'
}

const CHIP_CLASS: Record<ConductorChipState, string> = {
  active: 'border-(--ui-stroke-secondary) text-(--ui-text-secondary)',
  quiet: 'border-(--ui-stroke-tertiary) text-(--ui-text-tertiary)',
  idle: 'border-(--ui-stroke-tertiary) text-(--ui-text-tertiary)',
  stale: 'border-(--ui-yellow)/40 text-(--ui-yellow)',
  abandoned: 'border-(--ui-stroke-tertiary) text-(--ui-text-quaternary)'
}

/** Static status dot: no pulse, no ring (§8 motion rule). */
export function LivenessDot({ state }: { state: ConductorChipState }) {
  return (
    <span
      aria-hidden
      className={cn('inline-block size-1.5 shrink-0 rounded-full', DOT_CLASS[state])}
      data-liveness-dot={state}
    />
  )
}

/** §4 liveness chip. Color carries state only: amber for stale, neutral else. */
export function LivenessChip({ idleSince, state }: { idleSince?: null | number; state: ConductorChipState }) {
  const { t } = useI18n()
  const since = state === 'idle' && idleSince ? clockOf(idleSince * 1000) : null
  const hint = since
    ? `${t.conductors.livenessHint[state]} (${t.conductors.idleSince(since)})`
    : t.conductors.livenessHint[state]

  return (
    <Tip label={hint}>
      <span
        className={cn(
          'inline-flex shrink-0 items-center rounded border px-1 text-[0.625rem] leading-4',
          CHIP_CLASS[state]
        )}
        data-liveness={state}
      >
        {t.conductors.liveness[state]}
      </span>
    </Tip>
  )
}

function WaveTrack({ current, done, total }: { current: number; done: number; total: number }) {
  if (total <= 0 || total > 12) {
    return null
  }

  return (
    <span aria-hidden className="inline-flex shrink-0 items-center gap-px">
      {Array.from({ length: total }, (_, index) => {
        const n = index + 1

        return (
          <span
            className={cn(
              'h-1.5 w-1 rounded-[1px]',
              n <= done
                ? 'bg-(--ui-text-tertiary)'
                : n === current
                  ? 'border border-(--ui-text-secondary)'
                  : 'bg-(--ui-stroke-tertiary)'
            )}
            key={n}
          />
        )
      })}
    </span>
  )
}

function Cell({ children, className, column }: { children: ReactNode; className?: string; column: string }) {
  return (
    <div className={cn('flex min-w-0 flex-col justify-center', className)} data-col={column} role="gridcell">
      {children}
    </div>
  )
}

function Line({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn('flex min-w-0 items-center', className)}>{children}</span>
}

// ── Row ────────────────────────────────────────────────────────────────────

export interface ConductorRowProps {
  abandoned: boolean
  /** The owner profile the pane is running in; rows from another profile are
   *  not opened from here yet (R6 adds the profile route). */
  activeProfile: string
  onOpen?: (row: ConductorRowData, event: KeyboardEvent<HTMLDivElement> | MouseEvent<HTMLDivElement>) => void
  row: ConductorRowData
}

function ConductorRowImpl({ abandoned, activeProfile, onOpen, row }: ConductorRowProps) {
  conductorRowRenderProbe.current?.(row.key)

  const { t } = useI18n()
  const c = t.conductors
  const { build, orchestrator, project, record } = row
  const state = chipStateOf(row, abandoned)
  const derived = (column: DerivableColumn) => isDerivedColumn(row, column)

  const sessionId = orchestrator.hermes_session_id
  const profile = orchestrator.profile
  const crossProfile = Boolean(profile && profile !== activeProfile)
  const openable = Boolean(onOpen && sessionId && !crossProfile)
  const openBlockedHint = !sessionId ? c.notHermesSession : crossProfile && profile ? c.otherProfile(profile) : null

  const open = (event: KeyboardEvent<HTMLDivElement> | MouseEvent<HTMLDivElement>) => {
    if (openable) {
      onOpen?.(row, event)
    }
  }

  // Column 3: current unit (or the marker's stage when conductor is silent).
  const unit = build.current_units[0]
  const unitText = unit ? [unit.id, unit.title].filter(Boolean).join(' ') : build.phase
  const extraUnits = build.current_units.length - 1

  // Column 4: remaining waves include the current one.
  const wavesLeft = build.waves.total > 0 ? Math.max(0, build.waves.total - build.waves.done) : null

  // Column 5: estimate greys out when the record is present but not fresh (§3.4).
  const staleRecord = record.present && !record.fresh
  const recordClock = record.status_at ? clockOf(record.status_at * 1000) : null

  const gateLines = build.gates.map(gate => {
    const since = clockOf(isoToMs(gate.since))
    const due = dueDayOf(isoToMs(gate.due))

    return [gateKindLabel(gate.kind), gate.label, since && c.gateSince(since), due && c.gateDue(due)]
      .filter(Boolean)
      .join(' · ')
  })

  // Column 6: seats in fixed order. Relay-derived counts can't see native lanes.
  const seatsDerived = derived('seats')
  const refusalsBySeat = new Map<string, string[]>()

  for (const refusal of build.refusals) {
    const lines = refusalsBySeat.get(refusal.seat) ?? []
    lines.push(`${refusal.code} × ${refusal.count}${refusal.note ? ` — ${refusal.note}` : ''}`)
    refusalsBySeat.set(refusal.seat, lines)
  }

  const seatEntries = [
    ...SEAT_ORDER,
    ...((build.seats.other?.spawned ?? 0) > 0 ? [{ key: 'other', mono: 'ot' } as const] : [])
  ].map(({ key, mono }) => {
    const count = build.seats[key]
    const unknown = seatsDerived && NATIVE_SEATS.has(key)

    return { count, key, mono, unknown }
  })

  const seatTip = (
    <span className="flex flex-col gap-0.5">
      {seatEntries.map(({ count, key, unknown }) => (
        <span key={key}>
          {unknown
            ? `${key}: ?`
            : `${key}: ${count?.spawned ?? 0} spawned · ${count?.running ?? 0} running · ${count?.failed ?? 0} failed` +
              (count?.refused ? ` · ${c.refusedAtLeast(count.refused)}` : '')}
          {(refusalsBySeat.get(key) ?? []).map(line => (
            <span className="block pl-2 text-(--ui-text-tertiary)" key={line}>
              {line}
            </span>
          ))}
        </span>
      ))}
      {seatsDerived && <span className="text-(--ui-text-tertiary)">{c.seatUnknown}</span>}
    </span>
  )

  const ci = build.ci[0]
  const blocker = build.owner_blockers[0]
  const extraBlockers = build.owner_blockers.length - 1

  return (
    <div
      aria-disabled={openable ? undefined : true}
      className={cn(
        CONDUCTORS_GRID_TRACK,
        'min-h-10 border-b border-(--ui-stroke-tertiary) px-3 py-1.5 text-xs text-(--ui-text-secondary)',
        openable && 'cursor-pointer hover:bg-(--ui-row-hover-background) focus-visible:bg-(--ui-row-hover-background)',
        abandoned && 'opacity-70'
      )}
      data-row-key={row.key}
      onClick={open}
      onKeyDown={event => {
        if (event.key === 'Enter') {
          open(event)
        }
      }}
      role="row"
      tabIndex={openable ? 0 : -1}
    >
      {/* 1. status dot + project / branch */}
      <Cell column="project">
        <Line className="gap-1.5">
          <LivenessDot state={state} />
          <span className="truncate text-(--ui-text-primary)">{project.name || c.none}</span>
        </Line>
        {project.branch && (
          <span className="truncate pl-3 font-mono text-[0.65rem] text-(--ui-text-tertiary)">{project.branch}</span>
        )}
      </Cell>

      {/* 2. session: title, role badge, profile tag when not default */}
      <Cell column="session">
        <Tip label={openBlockedHint}>
          <span className={cn('truncate', !sessionId && 'font-mono text-(--ui-text-tertiary)')}>
            {orchestrator.title || orchestrator.claude_sid_short || c.none}
          </span>
        </Tip>
        {(orchestrator.role || (profile && profile !== 'default')) && (
          <Line className="gap-1 text-[0.625rem] text-(--ui-text-tertiary)">
            {orchestrator.role && <span className="rounded bg-(--ui-bg-tertiary) px-1">{orchestrator.role}</span>}
            {profile && profile !== 'default' && <span className="truncate">{profile}</span>}
          </Line>
        )}
      </Cell>

      {/* 3. now: wave track + current unit */}
      <Cell column="now">
        <Line className="gap-1.5">
          <WaveTrack current={build.waves.current} done={build.waves.done} total={build.waves.total} />
          <span className="tabular-nums">
            {build.waves.total > 0 ? `W${build.waves.current}/${build.waves.total}` : c.none}
          </span>
        </Line>
        {unitText && (
          <Line className="text-[0.68rem] text-(--ui-text-tertiary)">
            <span className="truncate">{unitText}</span>
            {extraUnits > 0 && <span className="ml-1 shrink-0">{c.more(extraUnits)}</span>}
            {derived('now') && <DerivedMark hint={c.derived} />}
          </Line>
        )}
      </Cell>

      {/* 4. remaining */}
      <Cell column="remaining">
        <Line>
          <span className="truncate tabular-nums">
            {wavesLeft === null ? c.none : c.waves(wavesLeft)}
            {' · '}
            {build.units ? c.units(build.units.remaining) : c.unitsUnknown}
          </span>
          {derived('remaining') && <DerivedMark hint={c.derived} />}
        </Line>
      </Cell>

      {/* 5. estimate p50 / p90 + gates chip */}
      <Cell column="estimate">
        {build.estimate ? (
          <Tip label={staleRecord && recordClock ? c.estimateStale(recordClock) : null}>
            <span className={cn('tabular-nums', staleRecord && 'text-(--ui-text-quaternary)')} data-estimate="">
              <span className={staleRecord ? undefined : 'text-(--ui-text-primary)'}>
                {formatHours(build.estimate.p50)} h
              </span>
              <span className="text-(--ui-text-tertiary)"> / {formatHours(build.estimate.p90)} h</span>
            </span>
          </Tip>
        ) : (
          <Tip label={c.estimateMissing}>
            <span className="text-(--ui-text-quaternary)" data-estimate="">
              {c.none}
            </span>
          </Tip>
        )}
        {build.gates.length > 0 && (
          <Line>
            <Tip
              label={
                <span className="flex flex-col gap-0.5">
                  {gateLines.map((line, index) => (
                    <span key={build.gates[index].id || index}>{line}</span>
                  ))}
                </span>
              }
            >
              <span className="rounded border border-(--ui-stroke-tertiary) px-1 text-[0.625rem] leading-4 text-(--ui-text-tertiary)">
                {c.gates(build.gates.length)}
              </span>
            </Tip>
            {derived('gates') && <DerivedMark hint={c.derived} />}
          </Line>
        )}
      </Cell>

      {/* 6. seats: fixed-order monograms, amber refused superscript */}
      <Cell column="seats">
        <Line>
          <Tip label={seatTip}>
            <span className="flex min-w-0 items-baseline gap-1.5 font-mono text-[0.68rem]">
              {seatEntries.map(({ count, key, mono, unknown }) => {
                const spawned = count?.spawned ?? 0

                return (
                  <span
                    className={cn(spawned === 0 && !unknown && 'text-(--ui-text-quaternary)')}
                    data-seat={key}
                    key={key}
                  >
                    <span className="text-(--ui-text-tertiary)">{mono}</span>
                    <span className="tabular-nums">{unknown ? '?' : spawned}</span>
                    {count?.refused ? (
                      <sup className="text-[0.55rem] text-(--ui-yellow)" data-refused="">
                        {count.refused}
                      </sup>
                    ) : null}
                  </span>
                )
              })}
            </span>
          </Tip>
          {seatsDerived && <DerivedMark hint={c.derivedRelayOnly} />}
        </Line>
      </Cell>

      {/* 7. lanes */}
      <Cell column="lanes">
        <Line>
          <span className="tabular-nums">
            {build.lanes.cap === null ? build.lanes.running : `${build.lanes.running}/${build.lanes.cap}`}
          </span>
          {build.lanes.stale > 0 && (
            <Tip label={c.staleLanes(build.lanes.stale)}>
              <span className="ml-0.5 text-[0.6rem] text-(--ui-yellow)">+{build.lanes.stale}</span>
            </Tip>
          )}
          {derived('lanes') && <DerivedMark hint={c.derivedWorkspace} />}
        </Line>
      </Cell>

      {/* 8. CI/PR — conductor's own observation until R7 joins the PR cache */}
      <Cell column="ci">
        {ci ? (
          <span
            className={cn('truncate tabular-nums', ci.state === 'failure' && 'text-(--ui-red)')}
            data-ci-state={ci.state}
          >
            {ci.pr !== null ? `#${ci.pr}` : ci.kind || 'run'}{' '}
            {ci.state === 'success' ? '✓' : ci.state === 'failure' ? '✕' : ci.state === 'pending' ? '·' : '?'}
          </span>
        ) : (
          <span className="text-(--ui-text-quaternary)">{c.none}</span>
        )}
      </Cell>

      {/* 9. owner blockers */}
      <Cell column="blockers">
        {blocker || build.blocked ? (
          <Line className="text-(--ui-yellow)">
            <span className="truncate">{blocker ? blocker.label || blocker.action : c.blocked}</span>
            {extraBlockers > 0 && <span className="ml-1 shrink-0">{c.more(extraBlockers)}</span>}
            {derived('blockers') && <DerivedMark hint={c.derived} />}
          </Line>
        ) : (
          <span className="text-(--ui-text-quaternary)">{c.none}</span>
        )}
      </Cell>

      {/* 10. last activity + liveness chip */}
      <Cell column="activity">
        <Line className="justify-between gap-1.5">
          <span className="truncate tabular-nums text-(--ui-text-tertiary)">
            <Ago atSec={build.last_activity_at} />
          </span>
          <LivenessChip idleSince={build.idle_since} state={state} />
        </Line>
      </Cell>
    </div>
  )
}

/** Memoised by reference: the pane reconciles rows by key, so an unchanged row
 *  keeps its object and its props stay equal across polls. */
export const ConductorRow = memo(ConductorRowImpl)
