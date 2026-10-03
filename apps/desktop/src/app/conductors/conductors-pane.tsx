import { useStore } from '@nanostores/react'
import { type KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useInRouterContext, useNavigate } from 'react-router'

import type { ConductorRow as ConductorRowData } from '@/api/conductors'
import type { OpenSessionNavigate } from '@/app/open-session'
import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { Badge } from '@/components/ui/badge'
import { Codicon } from '@/components/ui/codicon'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { sessionTitle } from '@/lib/chat-runtime'
import { effectiveSessionRole } from '@/lib/session-role'
import { formatAgo } from '@/lib/time'
import { cn } from '@/lib/utils'
import { $conductors, $conductorsNow, acquireConductorsPoller, refreshConductors } from '@/store/conductors'
import { $activeProfile } from '@/store/profile'
import { $projects } from '@/store/projects'
import { $sessions } from '@/store/session'
import { $sessionBindings, ensureSessionBinding, sessionBindingKey } from '@/store/session-binding'
import type { SessionInfo } from '@/types/hermes'

import {
  type ConductorOpenEvent,
  type ConductorTarget,
  conductorOpenIntent,
  conductorTarget,
  copyConductorText,
  openConductorExternal,
  openConductorTarget,
  sendToConductorTarget
} from './conductor-actions'
import { GhLimitPill, useConductorGhReads } from './conductor-ci'
import { rowBinding } from './conductor-binding'
import { ConductorRow } from './conductor-row'
import {
  type ConductorColumn,
  CONDUCTORS_GRID_MIN_WIDTH,
  CONDUCTORS_HEADER_CELL_LAYOUT,
  CONDUCTORS_HEADER_LAYOUT,
  CONDUCTORS_MERGED_HEADER,
  CONDUCTORS_PIECE,
  CONDUCTORS_SKELETON_LAYOUT
} from './conductors-layout'
import {
  clockOf,
  type ConductorFilter,
  EMPTY_ROW_CACHE,
  isAbandonedRow,
  matchesFilter,
  needsOwner,
  reconcileRows,
  type RowCache,
  sortConductorRows
} from './conductors-model'

type OpenSessionById = (sessionId: string, event: ConductorOpenEvent) => void
type OpenRow = (row: ConductorRowData, event: ConductorOpenEvent) => void
type SendRow = (row: ConductorRowData, text: string) => void

const noopNavigate: OpenSessionNavigate = () => {}

const refreshFresh = () => void refreshConductors({ fresh: true })

const COLUMN_KEYS: readonly ConductorColumn[] = [
  'project',
  'session',
  'now',
  'remaining',
  'estimate',
  'seats',
  'lanes',
  'ci',
  'blockers',
  'activity'
]

function UpdatedAgo({ fetchedAt }: { fetchedAt: number }) {
  const { t } = useI18n()
  const now = useStore($conductorsNow)

  return <>{t.conductors.updated(formatAgo(fetchedAt, t.conductors, Math.max(now, fetchedAt)))}</>
}

function HeaderRow() {
  const { t } = useI18n()
  const c = t.conductors

  return (
    <div
      className={cn(
        CONDUCTORS_HEADER_LAYOUT,
        'sticky top-0 z-[1] border-b border-(--ui-stroke-secondary) bg-(--ui-panel-background) px-3 py-1 text-[0.625rem] font-medium tracking-wide text-(--ui-text-tertiary) uppercase'
      )}
      role="row"
    >
      {COLUMN_KEYS.map(key => {
        const merged = CONDUCTORS_MERGED_HEADER[key]

        return (
          <div
            className={cn('truncate', CONDUCTORS_HEADER_CELL_LAYOUT[key])}
            data-col={key}
            key={key}
            role="columnheader"
          >
            {merged ? (
              <>
                <span className={CONDUCTORS_PIECE.wideOnly}>{c.columns[key]}</span>
                <span className={CONDUCTORS_PIECE.mediumOnly}>{c.mergedColumns[merged]}</span>
              </>
            ) : (
              c.columns[key]
            )}
          </div>
        )
      })}
    </div>
  )
}

/** First-fetch placeholder: three STATIC rows (no pulse, §8 motion rule). */
function SkeletonRows() {
  return (
    <>
      {[0, 1, 2].map(index => (
        <div
          aria-hidden
          className={cn(
            CONDUCTORS_SKELETON_LAYOUT,
            'min-h-10 items-center border-b border-(--ui-stroke-tertiary) px-3 py-1.5'
          )}
          data-slot="conductors-skeleton-row"
          key={index}
        >
          {COLUMN_KEYS.map(key => (
            <div className="h-2 w-3/4 rounded-sm bg-(--ui-bg-tertiary)" key={key} />
          ))}
        </div>
      ))}
    </>
  )
}

function UnbuiltOrchestrators({
  onOpen,
  onToggle,
  open,
  sessions
}: {
  onOpen: OpenSessionById
  onToggle: () => void
  open: boolean
  sessions: readonly SessionInfo[]
}) {
  const { t } = useI18n()
  const c = t.conductors
  const now = useStore($conductorsNow)

  return (
    <div data-slot="conductors-unbuilt">
      <button
        aria-expanded={open}
        className="px-3 py-1.5 text-(--ui-text-tertiary) hover:text-(--ui-text-primary)"
        data-slot="conductors-unbuilt-toggle"
        onClick={onToggle}
        type="button"
      >
        {open ? c.hideUnbuilt(sessions.length) : c.showUnbuilt(sessions.length)}
      </button>
      {open && (
        <ul>
          {sessions.map(session => {
            const role = effectiveSessionRole(session)
            const activeAt = (session.last_active || session.started_at) * 1000

            return (
              <li
                className="flex items-center gap-2 border-b border-(--ui-stroke-tertiary) px-3 py-1.5"
                data-session-id={session.id}
                data-slot="conductors-unbuilt-item"
                key={session.id}
              >
                <span className="min-w-0 truncate text-(--ui-text-primary)">{sessionTitle(session)}</span>
                {role && (
                  <Badge
                    className="text-[0.6rem] font-medium capitalize text-muted-foreground"
                    size="xs"
                    variant="outline"
                  >
                    {role}
                  </Badge>
                )}
                <span className="ml-auto shrink-0 text-(--ui-text-tertiary)">
                  {formatAgo(activeAt, c, Math.max(now, activeAt))}
                </span>
                <button
                  className="shrink-0 rounded border border-(--ui-stroke-secondary) px-2 py-0.5 text-(--ui-text-secondary) hover:bg-(--ui-control-hover-background)"
                  data-slot="conductors-unbuilt-open"
                  onClick={event => onOpen(session.id, event)}
                  type="button"
                >
                  {c.openUnbuilt}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

const ROW_SELECTOR = '[role="row"][data-row-key]'

export interface ConductorsPaneProps {
  /** Override for the row-open door (tests). Defaults to the §8 intents. */
  onOpenRow?: OpenRow
  /** Override for the "Orchestrators without a build" open action (tests). */
  onOpenSession?: OpenSessionById
  /** Override for Send… (tests). Defaults to stash-the-draft-and-open. */
  onSendRow?: SendRow
}

/** useNavigate() throws outside a Router (bare test harnesses render the pane
 *  router-free), so only the routed wrapper asks for it. */
export function ConductorsPane(props: ConductorsPaneProps = {}) {
  return useInRouterContext() ? (
    <RoutedConductorsPane {...props} />
  ) : (
    <ConductorsPaneView {...props} navigate={noopNavigate} />
  )
}

function RoutedConductorsPane(props: ConductorsPaneProps) {
  const routerNavigate = useNavigate()

  const navigate = useCallback<OpenSessionNavigate>((to, options) => void routerNavigate(to, options), [routerNavigate])

  return <ConductorsPaneView {...props} navigate={navigate} />
}

function ConductorsPaneView({
  navigate,
  onOpenRow,
  onOpenSession,
  onSendRow
}: ConductorsPaneProps & { navigate: OpenSessionNavigate }) {
  const { t } = useI18n()
  const c = t.conductors
  const visible = usePaneVisible()
  const state = useStore($conductors)
  const activeProfile = useStore($activeProfile)
  const [filter, setFilter] = useState<ConductorFilter>('all')
  const [showAbandoned, setShowAbandoned] = useState(false)
  const [showUnbuilt, setShowUnbuilt] = useState(false)
  const [expandedKeys, setExpandedKeys] = useState<ReadonlySet<string>>(() => new Set())
  const [focusKey, setFocusKey] = useState<null | string>(null)
  const gridRef = useRef<HTMLDivElement>(null)

  // §9: the poller runs only while this pane is mounted AND visible. An
  // inactive tab (keep-alive) releases it; the store ref-counts instances.
  useEffect(() => {
    if (!visible) {
      return
    }

    return acquireConductorsPoller()
  }, [visible])

  // Reconcile by key so a poll that changed nothing for a row keeps its
  // object — the memoised row then skips rendering.
  const cacheRef = useRef<RowCache>(EMPTY_ROW_CACHE)
  const data = state.data

  const rows = useMemo(() => {
    const next = reconcileRows(cacheRef.current, data?.rows ?? [])
    cacheRef.current = next.cache

    return next.rows
  }, [data])

  const sessions = useStore($sessions)

  // O3: orchestrator-like sessions no row points at. Renderer-only; reads the
  // sessions already in the store.
  const unbuiltOrchestrators = useMemo(() => {
    const owned = new Set<string>()

    for (const row of rows) {
      const id = row.orchestrator.hermes_session_id

      if (id) {
        owned.add(id)
      }
    }

    return sessions
      .filter(session => {
        const role = effectiveSessionRole(session)

        return (role === 'orchestrator' || role === 'manager') && !owned.has(session.id)
      })
      .sort((a, b) => (b.last_active || b.started_at) - (a.last_active || a.started_at))
  }, [rows, sessions])

  const { abandonedRows, liveRows } = useMemo(() => {
    const generatedAt = data?.generated_at ?? 0
    const live: ConductorRowData[] = []
    const gone: ConductorRowData[] = []

    for (const row of rows) {
      ;(isAbandonedRow(row, generatedAt) ? gone : live).push(row)
    }

    return { abandonedRows: sortConductorRows(gone), liveRows: sortConductorRows(live) }
  }, [rows, data?.generated_at])

  // R7: gh reads (PR checks, run status) only while the pane is visible.
  useConductorGhReads(liveRows, visible, state.fetchedAt)

  const shownRows = useMemo(() => liveRows.filter(row => matchesFilter(row, filter)), [liveRows, filter])
  const needsYou = useMemo(() => liveRows.filter(needsOwner).length, [liveRows])

  // R8: b10 session bindings name each shown row's project. Cache-first reads, no timer,
  // and only while the pane is visible.
  const bindings = useStore($sessionBindings)
  const projects = useStore($projects)

  const bindingAsks = useMemo(() => {
    const asks = new Map<string, [null | string, string]>()

    for (const row of showAbandoned ? [...shownRows, ...abandonedRows] : shownRows) {
      const sessionId = row.orchestrator.hermes_session_id

      if (sessionId) {
        asks.set(sessionBindingKey(row.orchestrator.profile, sessionId), [row.orchestrator.profile, sessionId])
      }
    }

    return [...asks.values()]
  }, [shownRows, abandonedRows, showAbandoned])

  useEffect(() => {
    if (!visible) {
      return
    }

    for (const [profile, sessionId] of bindingAsks) {
      ensureSessionBinding(profile, sessionId)
    }
  }, [bindingAsks, state.fetchedAt, visible])

  // R6 open intents (§8): plain = stack, ⌘ = tab, ⇧⌘ = window; a row owned by
  // another profile opens under that profile.
  const openRow = useCallback<OpenRow>(
    (row, event) => {
      if (onOpenRow) {
        onOpenRow(row, event)

        return
      }

      const target = conductorTarget(row, activeProfile)

      if (target) {
        openConductorTarget(target, conductorOpenIntent(event, target), navigate)
      }
    },
    [activeProfile, navigate, onOpenRow]
  )

  const openOrchestratorSession = useCallback<OpenSessionById>(
    (sessionId, event) => {
      if (onOpenSession) {
        onOpenSession(sessionId, event)

        return
      }

      const target: ConductorTarget = { sessionId }
      openConductorTarget(target, conductorOpenIntent(event, target), navigate)
    },
    [navigate, onOpenSession]
  )

  const sendRow = useCallback<SendRow>(
    (row, text) => {
      if (onSendRow) {
        onSendRow(row, text)

        return
      }

      const target = conductorTarget(row, activeProfile)

      if (target) {
        sendToConductorTarget(target, text, navigate)
      }
    },
    [activeProfile, navigate, onSendRow]
  )

  const toggleExpanded = useCallback((key: string) => {
    setExpandedKeys(previous => {
      const next = new Set(previous)

      if (!next.delete(key)) {
        next.add(key)
      }

      return next
    })
  }, [])

  // Roving tabindex: exactly one row is the grid's tab stop — the last one
  // focused while it is still shown, else the first shown row.
  const visibleKeys = useMemo(
    () => [...shownRows, ...(showAbandoned ? abandonedRows : [])].map(row => row.key),
    [abandonedRows, showAbandoned, shownRows]
  )

  const tabStop = focusKey && visibleKeys.includes(focusKey) ? focusKey : (visibleKeys[0] ?? null)

  const onGridKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement

    // Only a focused row moves; keys inside a popover field or menu don't.
    if (!target.matches?.(ROW_SELECTOR) || event.altKey || event.metaKey || event.ctrlKey) {
      return
    }

    const rowEls = Array.from(gridRef.current?.querySelectorAll<HTMLElement>(ROW_SELECTOR) ?? [])
    const index = rowEls.indexOf(target)
    let next = -1

    if (event.key === 'ArrowDown') {
      next = Math.min(rowEls.length - 1, index + 1)
    } else if (event.key === 'ArrowUp') {
      next = Math.max(0, index - 1)
    } else if (event.key === 'Home') {
      next = 0
    } else if (event.key === 'End') {
      next = rowEls.length - 1
    } else {
      return
    }

    event.preventDefault()
    const el = rowEls[next]

    if (el && el !== target) {
      setFocusKey(el.dataset.rowKey ?? null)
      el.focus()
    }
  }

  const firstLoad = !data && (state.status === 'idle' || state.status === 'loading')
  const firstError = !data && state.status === 'error'
  const staleData = Boolean(data) && state.status === 'error'
  const asOf = clockOf(state.fetchedAt)

  const filterOptions = useMemo(
    () =>
      (['all', 'needsMe', 'active', 'idleStale'] as const).map(id => ({
        id,
        label: c.filters[id]
      })),
    [c]
  )

  const renderRow = (row: ConductorRowData, abandoned: boolean) => (
    <ConductorRow
      abandoned={abandoned}
      activeProfile={activeProfile}
      binding={rowBinding(row, bindings, projects)}
      expanded={expandedKeys.has(row.key)}
      key={row.key}
      onCopy={copyConductorText}
      onExternal={openConductorExternal}
      onFocusRow={setFocusKey}
      onOpen={openRow}
      onRefresh={refreshFresh}
      onSend={sendRow}
      onToggleExpanded={toggleExpanded}
      row={row}
      tabbable={row.key === tabStop}
    />
  )

  const unbuiltSection = unbuiltOrchestrators.length > 0 && (
    <UnbuiltOrchestrators
      onOpen={openOrchestratorSession}
      onToggle={() => setShowUnbuilt(open => !open)}
      open={showUnbuilt}
      sessions={unbuiltOrchestrators}
    />
  )

  return (
    <div className="@container flex h-full min-h-0 flex-col text-xs" data-slot="conductors-pane">
      <div
        className={cn(
          'flex shrink-0 items-center gap-3 border-b border-(--ui-stroke-tertiary) px-3 py-1.5',
          staleData && 'text-(--ui-yellow)'
        )}
        data-slot="conductors-header"
      >
        <span className="font-medium text-(--ui-text-primary)">
          {c.title}
          {data && <span className="text-(--ui-text-tertiary)"> · {liveRows.length}</span>}
          {needsYou > 0 && <span className="text-(--ui-yellow)"> · {c.needsYou(needsYou)}</span>}
        </span>
        {data && <SegmentedControl onChange={setFilter} options={filterOptions} value={filter} />}
        <GhLimitPill />
        <span className="ml-auto truncate text-(--ui-text-tertiary)">
          {state.fetchedAt !== null && !staleData && <UpdatedAgo fetchedAt={state.fetchedAt} />}
        </span>
        <Tip label={c.refresh}>
          <button
            aria-label={c.refresh}
            className="grid size-5 place-items-center rounded text-(--ui-text-tertiary) hover:bg-(--ui-control-hover-background) hover:text-(--ui-text-primary)"
            onClick={refreshFresh}
            type="button"
          >
            <Codicon name="refresh" size="0.75rem" />
          </button>
        </Tip>
      </div>

      {staleData && (
        <div
          className="flex shrink-0 items-center gap-2 border-b border-(--ui-yellow)/30 bg-(--ui-yellow)/8 px-3 py-1 text-(--ui-yellow)"
          data-slot="conductors-stale-banner"
          role="status"
        >
          <span className="truncate">{c.staleBanner(asOf ?? c.none)}</span>
          <button className="ml-auto shrink-0 underline-offset-2 hover:underline" onClick={refreshFresh} type="button">
            {c.retry}
          </button>
        </div>
      )}

      {firstError ? (
        <div
          className="flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center"
          data-slot="conductors-error"
          role="alert"
        >
          <div className="text-sm text-(--ui-text-primary)">{c.errorTitle}</div>
          <div className="text-(--ui-text-tertiary)">{c.errorBody}</div>
          <button
            className="mt-1 rounded border border-(--ui-stroke-secondary) px-2 py-0.5 text-(--ui-text-secondary) hover:bg-(--ui-control-hover-background)"
            onClick={refreshFresh}
            type="button"
          >
            {c.retry}
          </button>
        </div>
      ) : data && liveRows.length === 0 && abandonedRows.length === 0 ? (
        <div
          className="flex flex-1 flex-col items-center justify-center gap-1 p-6 text-center"
          data-slot="conductors-empty"
        >
          <div className="text-sm text-(--ui-text-primary)">{c.emptyTitle}</div>
          <div className="text-(--ui-text-tertiary)">{c.emptyBody}</div>
          {data.sources.marker_index === 'missing' && <div className="text-(--ui-text-tertiary)">{c.emptyNoIndex}</div>}
          {unbuiltSection}
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-auto" data-slot="conductors-body">
          <div
            aria-busy={firstLoad || undefined}
            aria-label={c.gridLabel}
            className={CONDUCTORS_GRID_MIN_WIDTH}
            onKeyDown={onGridKeyDown}
            ref={gridRef}
            role="grid"
          >
            <HeaderRow />
            {firstLoad && <SkeletonRows />}
            {shownRows.map(row => renderRow(row, false))}
            {data && shownRows.length === 0 && liveRows.length > 0 && (
              <div className="px-3 py-4 text-center text-(--ui-text-tertiary)" role="row">
                <span role="gridcell">{c.emptyFilter}</span>
              </div>
            )}
            {data && liveRows.length === 0 && (
              <div className="px-3 py-4 text-center text-(--ui-text-tertiary)" role="row">
                <span role="gridcell">{c.emptyTitle}</span>
              </div>
            )}
            {showAbandoned && abandonedRows.map(row => renderRow(row, true))}
          </div>
          {abandonedRows.length > 0 && (
            <button
              aria-expanded={showAbandoned}
              className="px-3 py-1.5 text-(--ui-text-tertiary) hover:text-(--ui-text-primary)"
              data-slot="conductors-abandoned-toggle"
              onClick={() => setShowAbandoned(open => !open)}
              type="button"
            >
              {showAbandoned ? c.hideAbandoned(abandonedRows.length) : c.showAbandoned(abandonedRows.length)}
            </button>
          )}
          {unbuiltSection}
        </div>
      )}
    </div>
  )
}
