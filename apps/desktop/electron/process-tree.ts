import { execFile } from 'node:child_process'
import path from 'node:path'

import { ipcMain as electronIpcMain, type WebContents } from 'electron'

export interface RawProcessInfo {
  pid: number
  ppid: number
  etime: string
  time: string
  comm: string
  args: string
  command: string
}

export interface ProcessInfo {
  pid: number
  ppid: number
  command: string
  comm: string
  age: string
  ageSeconds: number
  cpuTime10s: number
  laneOrSession: string
  depth: number
}

export interface SpawnRateEntry {
  basename: string
  count: number
}

export interface ProcessTreeSnapshot {
  processes: ProcessInfo[]
  spawnsPerMinute: SpawnRateEntry[]
  timestamp: number
}

export interface ProcessTreeIpcDeps {
  ipcMain?: typeof electronIpcMain
  getRootPids: () => number[]
  getKnownCwds?: () => Record<string, string>
  execPs?: () => Promise<string>
  sampleIntervalMs?: number
}

export function parseTimeSeconds(timeStr: string): number {
  if (!timeStr) {return 0}
  let str = timeStr.trim()
  let days = 0
  const dashIndex = str.indexOf('-')

  if (dashIndex !== -1) {
    days = parseInt(str.slice(0, dashIndex), 10) || 0
    str = str.slice(dashIndex + 1)
  }

  const parts = str.split(':')
  let hours = 0
  let minutes = 0
  let seconds = 0

  if (parts.length === 3) {
    hours = parseInt(parts[0], 10) || 0
    minutes = parseInt(parts[1], 10) || 0
    seconds = parseFloat(parts[2]) || 0
  } else if (parts.length === 2) {
    minutes = parseInt(parts[0], 10) || 0
    seconds = parseFloat(parts[1]) || 0
  } else if (parts.length === 1) {
    seconds = parseFloat(parts[0]) || 0
  }

  return days * 86400 + hours * 3600 + minutes * 60 + seconds
}

export function parsePsLine(line: string): RawProcessInfo | null {
  const trimmed = line.trim()

  if (!trimmed) {return null}

  // Match: pid, ppid, etime, time, and optional remainder
  const match = trimmed.match(/^(\d+)\s+(\d+)\s+([^\s]+)\s+([^\s]+)(?:\s+(.*))?$/)

  if (!match) {return null}

  const pid = parseInt(match[1], 10)
  const ppid = parseInt(match[2], 10)
  const etime = match[3]
  const time = match[4]
  const remainder = match[5] ? match[5].trim() : ''

  let comm = ''
  let args = ''

  if (remainder) {
    const spaceIdx = remainder.search(/\s/)

    if (spaceIdx === -1) {
      comm = remainder
      args = remainder
    } else {
      comm = remainder.slice(0, spaceIdx)
      args = remainder.slice(spaceIdx + 1).trim()
    }
  }

  const fullCommand = args || comm
  const command = fullCommand.slice(0, 160)

  return {
    pid,
    ppid,
    etime,
    time,
    comm,
    args,
    command
  }
}

export function parsePsOutput(raw: string): RawProcessInfo[] {
  if (!raw) {return []}
  const lines = raw.split(/\r?\n/)
  const result: RawProcessInfo[] = []

  for (const line of lines) {
    const parsed = parsePsLine(line)

    if (parsed) {
      result.push(parsed)
    }
  }

  return result
}

export function matchOwningSessionOrLane(
  args: string,
  cwd?: string,
  knownCwds?: Record<string, string>
): string {
  const haystack = `${cwd ?? ''} ${args}`

  // 1. Match /tmp/lane-* or /private/tmp/lane-*
  const tmpLaneMatch = haystack.match(/(?:\/private)?\/tmp\/(lane-[a-zA-Z0-9_-]+)/)

  if (tmpLaneMatch) {
    return `/tmp/${tmpLaneMatch[1]}`
  }

  // 2. Match .claude/worktrees/lane-*
  const claudeLaneMatch = haystack.match(/(?:\.claude\/worktrees\/)(lane-[a-zA-Z0-9_-]+)/)

  if (claudeLaneMatch) {
    return `.claude/worktrees/${claudeLaneMatch[1]}`
  }

  // 3. Match generic lane-*
  const genericLaneMatch = haystack.match(/(?:^|\s|\/)(lane-[a-zA-Z0-9_-]+)/)

  if (genericLaneMatch) {
    return genericLaneMatch[1]
  }

  // 4. Match --profile <name> or -p <name>
  const profileMatch = haystack.match(/(?:--profile[= ]| -p )([a-zA-Z0-9_-]+)/)

  if (profileMatch) {
    return `profile:${profileMatch[1]}`
  }

  // 5. Match --session <id> or -s <id>
  const sessionMatch = haystack.match(/(?:--session(?:-id)?[= ]| -s )([a-zA-Z0-9_-]+)/)

  if (sessionMatch) {
    return `session:${sessionMatch[1]}`
  }

  // 6. Match known session cwds if provided
  if (knownCwds) {
    for (const [id, knownPath] of Object.entries(knownCwds)) {
      if (knownPath && (haystack.includes(knownPath) || (cwd && cwd.includes(knownPath)))) {
        return id
      }
    }
  }

  return ''
}

export function buildProcessTree(
  allProcesses: RawProcessInfo[],
  rootPids: number[]
): Array<RawProcessInfo & { depth: number }> {
  const procByPid = new Map<number, RawProcessInfo>()
  const parentToChildren = new Map<number, number[]>()

  for (const proc of allProcesses) {
    procByPid.set(proc.pid, proc)
    const list = parentToChildren.get(proc.ppid)

    if (list) {
      list.push(proc.pid)
    } else {
      parentToChildren.set(proc.ppid, [proc.pid])
    }
  }

  const result: Array<RawProcessInfo & { depth: number }> = []
  const visited = new Set<number>()
  const queue: Array<{ pid: number; depth: number }> = []

  for (const rootPid of rootPids) {
    if (procByPid.has(rootPid) && !visited.has(rootPid)) {
      visited.add(rootPid)
      queue.push({ pid: rootPid, depth: 0 })
    }
  }

  while (queue.length > 0) {
    const { pid, depth } = queue.shift()!
    const proc = procByPid.get(pid)

    if (proc) {
      result.push({ ...proc, depth })
    }

    const children = parentToChildren.get(pid) ?? []

    for (const childPid of children) {
      if (!visited.has(childPid)) {
        visited.add(childPid)
        queue.push({ pid: childPid, depth: depth + 1 })
      }
    }
  }

  return result
}

export class ProcessTreeTracker {
  private cpuHistory = new Map<number, Array<{ timestamp: number; cpuTime: number }>>()
  private knownPids = new Set<number>()
  private spawnEvents: Array<{ pid: number; basename: string; timestamp: number }> = []
  private initialized = false

  public reset(): void {
    this.cpuHistory.clear()
    this.knownPids.clear()
    this.spawnEvents = []
    this.initialized = false
  }

  public update(
    rawProcesses: RawProcessInfo[],
    rootPids: number[],
    now: number = Date.now(),
    knownCwds?: Record<string, string>
  ): ProcessTreeSnapshot {
    const tree = buildProcessTree(rawProcesses, rootPids)
    const currentPids = new Set(tree.map(p => p.pid))

    if (!this.initialized) {
      for (const p of tree) {
        this.knownPids.add(p.pid)
      }

      this.initialized = true
    } else {
      for (const p of tree) {
        if (!this.knownPids.has(p.pid)) {
          this.knownPids.add(p.pid)
          const basename = p.comm ? path.basename(p.comm) : 'unknown'
          this.spawnEvents.push({ pid: p.pid, basename, timestamp: now })
        }
      }
    }

    const window60sStart = now - 60000
    this.spawnEvents = this.spawnEvents.filter(ev => ev.timestamp >= window60sStart)

    const counts = new Map<string, number>()

    for (const ev of this.spawnEvents) {
      counts.set(ev.basename, (counts.get(ev.basename) ?? 0) + 1)
    }

    const spawnsPerMinute: SpawnRateEntry[] = [...counts.entries()]
      .map(([basename, count]) => ({ basename, count }))
      .sort((a, b) => b.count - a.count || a.basename.localeCompare(b.basename))

    const processes: ProcessInfo[] = []
    const window10sStart = now - 10000

    for (const p of tree) {
      const currentCpu = parseTimeSeconds(p.time)
      let history = this.cpuHistory.get(p.pid)

      if (!history) {
        history = []
        this.cpuHistory.set(p.pid, history)
      }

      history.push({ timestamp: now, cpuTime: currentCpu })

      const pruneThreshold = now - 20000

      while (history.length > 1 && history[0].timestamp < pruneThreshold) {
        history.shift()
      }

      let baseline = history[0]

      for (const entry of history) {
        if (entry.timestamp <= window10sStart) {
          baseline = entry
        } else {
          break
        }
      }

      const rawDelta = history.length <= 1 ? 0 : Math.max(0, currentCpu - baseline.cpuTime)
      const cpuTime10s = Math.round(rawDelta * 100) / 100

      const laneOrSession = matchOwningSessionOrLane(p.args, undefined, knownCwds)

      processes.push({
        pid: p.pid,
        ppid: p.ppid,
        command: p.command,
        comm: p.comm,
        age: p.etime,
        ageSeconds: parseTimeSeconds(p.etime),
        cpuTime10s,
        laneOrSession,
        depth: p.depth
      })
    }

    for (const pid of this.cpuHistory.keys()) {
      if (!currentPids.has(pid)) {
        this.cpuHistory.delete(pid)
      }
    }

    return {
      processes,
      spawnsPerMinute,
      timestamp: now
    }
  }
}

export function defaultExecPs(): Promise<string> {
  if (process.platform === 'win32') {
    return Promise.resolve('')
  }

  return new Promise(resolve => {
    execFile(
      'ps',
      ['-A', '-o', 'pid=,ppid=,etime=,time=,comm=,args='],
      { maxBuffer: 10 * 1024 * 1024 },
      (err, stdout) => {
        if (err) {
          resolve('')

          return
        }

        resolve(stdout)
      }
    )
  })
}

export function registerProcessTreeIpc({
  ipcMain: ipc = electronIpcMain,
  getRootPids,
  getKnownCwds,
  execPs = defaultExecPs,
  sampleIntervalMs = 5000
}: ProcessTreeIpcDeps) {
  const subscribers = new Set<WebContents>()
  let intervalTimer: ReturnType<typeof setInterval> | null = null
  const tracker = new ProcessTreeTracker()

  async function tick() {
    if (subscribers.size === 0) {
      if (intervalTimer) {
        clearInterval(intervalTimer)
        intervalTimer = null
      }

      return
    }

    try {
      const rootPids = getRootPids()
      const raw = await execPs()
      const rawProcs = parsePsOutput(raw)
      const knownCwds = getKnownCwds ? getKnownCwds() : undefined
      const snapshot = tracker.update(rawProcs, rootPids, Date.now(), knownCwds)

      for (const wc of subscribers) {
        if (!wc.isDestroyed()) {
          wc.send('hermes:process-tree:update', snapshot)
        }
      }
    } catch {
      // Ignore background sampling errors
    }
  }

  ipc.on('hermes:process-tree:subscribe', event => {
    const wc = event.sender

    if (!subscribers.has(wc)) {
      subscribers.add(wc)
      wc.once('destroyed', () => {
        subscribers.delete(wc)

        if (subscribers.size === 0 && intervalTimer) {
          clearInterval(intervalTimer)
          intervalTimer = null
          tracker.reset()
        }
      })
    }

    if (!intervalTimer) {
      void tick()
      intervalTimer = setInterval(() => void tick(), sampleIntervalMs)
    }
  })

  ipc.on('hermes:process-tree:unsubscribe', event => {
    const wc = event.sender
    subscribers.delete(wc)

    if (subscribers.size === 0 && intervalTimer) {
      clearInterval(intervalTimer)
      intervalTimer = null
      tracker.reset()
    }
  })

  ipc.handle('hermes:process-tree:get', async () => {
    const rootPids = getRootPids()
    const raw = await execPs()
    const rawProcs = parsePsOutput(raw)
    const knownCwds = getKnownCwds ? getKnownCwds() : undefined

    return tracker.update(rawProcs, rootPids, Date.now(), knownCwds)
  })

  return {
    stop: () => {
      if (intervalTimer) {
        clearInterval(intervalTimer)
        intervalTimer = null
      }

      subscribers.clear()
      tracker.reset()
    }
  }
}
