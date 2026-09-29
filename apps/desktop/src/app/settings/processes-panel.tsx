import { useEffect, useMemo, useState } from 'react'

import { Input } from '@/components/ui/input'
import { useI18n } from '@/i18n'
import { Activity, ArrowDown, ArrowUp, Cpu, Loader2, Search } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { Pill, SectionHeading, SettingsContent } from './primitives'

export function ProcessesPanel() {
  const { t } = useI18n()
  const p = t.settings.processes
  const [snapshot, setSnapshot] = useState<DesktopProcessTreeSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [filterText, setFilterText] = useState('')
  const [sortColumn, setSortColumn] = useState<keyof DesktopProcessInfo>('cpuTime10s')
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc')

  useEffect(() => {
    if (!window.hermesDesktop?.processTree?.subscribe) {
      setLoading(false)
      return
    }

    const unsubscribe = window.hermesDesktop.processTree.subscribe(data => {
      setSnapshot(data)
      setLoading(false)
    })

    return () => {
      unsubscribe()
    }
  }, [])

  const handleSort = (col: keyof DesktopProcessInfo) => {
    if (sortColumn === col) {
      setSortOrder(prev => (prev === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortColumn(col)
      setSortOrder(col === 'cpuTime10s' ? 'desc' : 'asc')
    }
  }

  const filteredProcesses = useMemo(() => {
    const list = snapshot?.processes ?? []
    const q = filterText.trim().toLowerCase()
    if (!q) return list
    return list.filter(
      proc =>
        proc.pid.toString().includes(q) ||
        proc.ppid.toString().includes(q) ||
        proc.command.toLowerCase().includes(q) ||
        proc.comm.toLowerCase().includes(q) ||
        proc.laneOrSession.toLowerCase().includes(q)
    )
  }, [snapshot, filterText])

  const sortedProcesses = useMemo(() => {
    return [...filteredProcesses].sort((a, b) => {
      let cmp = 0
      if (sortColumn === 'cpuTime10s') {
        cmp = a.cpuTime10s - b.cpuTime10s
      } else if (sortColumn === 'pid') {
        cmp = a.pid - b.pid
      } else if (sortColumn === 'ppid') {
        cmp = a.ppid - b.ppid
      } else if (sortColumn === 'ageSeconds' || sortColumn === 'age') {
        cmp = a.ageSeconds - b.ageSeconds
      } else if (sortColumn === 'command') {
        cmp = a.command.localeCompare(b.command)
      } else if (sortColumn === 'laneOrSession') {
        cmp = a.laneOrSession.localeCompare(b.laneOrSession)
      }
      return sortOrder === 'desc' ? -cmp : cmp
    })
  }, [filteredProcesses, sortColumn, sortOrder])

  const spawns = snapshot?.spawnsPerMinute ?? []

  return (
    <SettingsContent>
      <div className="space-y-6">
        <SectionHeading
          aside={
            <Pill className="gap-1 font-mono text-[10px]" tone="success">
              <span className="size-1.5 animate-pulse rounded-full bg-emerald-500" />
              Live (5s)
            </Pill>
          }
          icon={Cpu}
          page
          title={p.title}
        />

        <p className="text-sm text-muted-foreground">{p.description}</p>

        {/* Spawns per minute rolling counter */}
        <div className="rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-card-background) p-4 shadow-xs">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              <Activity className="size-3.5" />
              {p.spawnsPerMinute}
            </h3>
            <span className="text-[11px] text-muted-foreground">60s rolling window</span>
          </div>

          {spawns.length === 0 ? (
            <p className="py-1 text-xs text-muted-foreground" data-testid="no-spawns-msg">
              {p.noSpawns}
            </p>
          ) : (
            <div className="flex flex-wrap gap-2" data-testid="spawns-list">
              {spawns.map(entry => (
                <div
                  className="flex items-center gap-2 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-chat-surface-background) px-2.5 py-1 text-xs shadow-xs"
                  key={entry.basename}
                >
                  <span className="font-mono font-medium text-foreground">{entry.basename}</span>
                  <Pill className="px-1.5 py-0 text-[10px]" tone="primary">
                    {entry.count}/min
                  </Pill>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Process table controls */}
        <div className="flex items-center justify-between gap-4">
          <div className="relative max-w-sm flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="h-8 pl-8 text-xs"
              onChange={e => setFilterText(e.target.value)}
              placeholder="Filter processes…"
              value={filterText}
            />
          </div>
          <span className="font-mono text-xs text-muted-foreground">
            {sortedProcesses.length} {sortedProcesses.length === 1 ? 'process' : 'processes'}
          </span>
        </div>

        {/* Processes table */}
        <div className="overflow-hidden rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-card-background) shadow-xs">
          <div className="max-h-[500px] overflow-auto">
            <table className="w-full border-collapse text-left text-xs" data-testid="processes-table">
              <thead className="sticky top-0 z-10 border-b border-(--ui-stroke-tertiary) bg-(--ui-chat-surface-background) font-medium text-muted-foreground">
                <tr>
                  <th className="px-3 py-2">
                    <button
                      className="flex items-center gap-1 hover:text-foreground"
                      onClick={() => handleSort('pid')}
                      type="button"
                    >
                      {p.tablePid}
                      {sortColumn === 'pid' &&
                        (sortOrder === 'asc' ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
                    </button>
                  </th>
                  <th className="px-3 py-2">
                    <button
                      className="flex items-center gap-1 hover:text-foreground"
                      onClick={() => handleSort('ppid')}
                      type="button"
                    >
                      {p.tablePpid}
                      {sortColumn === 'ppid' &&
                        (sortOrder === 'asc' ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
                    </button>
                  </th>
                  <th className="px-3 py-2">
                    <button
                      className="flex items-center gap-1 hover:text-foreground"
                      onClick={() => handleSort('command')}
                      type="button"
                    >
                      {p.tableCommand}
                      {sortColumn === 'command' &&
                        (sortOrder === 'asc' ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
                    </button>
                  </th>
                  <th className="px-3 py-2">
                    <button
                      className="flex items-center gap-1 hover:text-foreground"
                      onClick={() => handleSort('ageSeconds')}
                      type="button"
                    >
                      {p.tableAge}
                      {sortColumn === 'ageSeconds' &&
                        (sortOrder === 'asc' ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
                    </button>
                  </th>
                  <th className="px-3 py-2">
                    <button
                      className="flex items-center gap-1 hover:text-foreground"
                      onClick={() => handleSort('cpuTime10s')}
                      type="button"
                    >
                      {p.tableCpu}
                      {sortColumn === 'cpuTime10s' &&
                        (sortOrder === 'asc' ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
                    </button>
                  </th>
                  <th className="px-3 py-2">
                    <button
                      className="flex items-center gap-1 hover:text-foreground"
                      onClick={() => handleSort('laneOrSession')}
                      type="button"
                    >
                      {p.tableSession}
                      {sortColumn === 'laneOrSession' &&
                        (sortOrder === 'asc' ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
                    </button>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-(--ui-stroke-tertiary)">
                {loading && !snapshot ? (
                  <tr>
                    <td className="p-8 text-center text-muted-foreground" colSpan={6}>
                      <div className="flex items-center justify-center gap-2">
                        <Loader2 className="size-4 animate-spin" />
                        <span>{p.loading}</span>
                      </div>
                    </td>
                  </tr>
                ) : sortedProcesses.length === 0 ? (
                  <tr>
                    <td className="p-8 text-center text-muted-foreground" colSpan={6} data-testid="no-processes-msg">
                      {p.noProcesses}
                    </td>
                  </tr>
                ) : (
                  sortedProcesses.map(proc => (
                    <tr
                      className="transition-colors hover:bg-(--ui-chat-surface-background)"
                      data-testid="process-row"
                      key={proc.pid}
                    >
                      <td className="whitespace-nowrap px-3 py-2 font-mono font-medium">{proc.pid}</td>
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-muted-foreground">{proc.ppid}</td>
                      <td className="max-w-[320px] px-3 py-2">
                        <div
                          className="flex items-center gap-1 font-mono text-[11px]"
                          style={{ paddingLeft: `${proc.depth * 12}px` }}
                        >
                          {proc.depth > 0 && <span className="select-none text-muted-foreground">└─</span>}
                          <span className="truncate" title={proc.command}>
                            {proc.command}
                          </span>
                        </div>
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-muted-foreground">{proc.age}</td>
                      <td className="whitespace-nowrap px-3 py-2 font-mono">
                        <span
                          className={cn(
                            'rounded px-1.5 py-0.5',
                            proc.cpuTime10s > 1
                              ? 'bg-amber-500/15 font-semibold text-amber-500'
                              : proc.cpuTime10s > 0.1
                                ? 'bg-primary/10 text-primary'
                                : 'text-muted-foreground'
                          )}
                        >
                          {proc.cpuTime10s.toFixed(2)}s
                        </span>
                      </td>
                      <td className="max-w-[200px] truncate px-3 py-2">
                        {proc.laneOrSession ? (
                          <Pill className="truncate font-mono text-[10px]" tone="muted">
                            {proc.laneOrSession}
                          </Pill>
                        ) : (
                          <span className="text-muted-foreground/40">—</span>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </SettingsContent>
  )
}
