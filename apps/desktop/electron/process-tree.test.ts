import assert from 'node:assert/strict'

import { describe, it } from 'vitest'

import {
  buildProcessTree,
  matchOwningSessionOrLane,
  parsePsLine,
  parsePsOutput,
  parseTimeSeconds,
  ProcessTreeTracker,
  registerProcessTreeIpc
} from './process-tree'

describe('parseTimeSeconds', () => {
  it('parses mm:ss and mm:ss.cc', () => {
    assert.equal(parseTimeSeconds('00:05'), 5)
    assert.equal(parseTimeSeconds('01:23'), 83)
    assert.equal(parseTimeSeconds('0:00.50'), 0.5)
    assert.equal(parseTimeSeconds('1:23.45'), 83.45)
  })

  it('parses hh:mm:ss', () => {
    assert.equal(parseTimeSeconds('01:00:00'), 3600)
    assert.equal(parseTimeSeconds('01:23:45'), 5025)
  })

  it('parses dd-hh:mm:ss', () => {
    assert.equal(parseTimeSeconds('1-00:00:00'), 86400)
    assert.equal(parseTimeSeconds('2-03:45:12'), 2 * 86400 + 3 * 3600 + 45 * 60 + 12)
  })

  it('returns 0 for empty or invalid strings', () => {
    assert.equal(parseTimeSeconds(''), 0)
    assert.equal(parseTimeSeconds('  '), 0)
  })
})

describe('parsePsLine & parsePsOutput', () => {
  it('parses valid ps output lines with irregular spacing', () => {
    const line = '  1001     1   05:20   00:12.34  python3  /usr/bin/python3 main.py --profile dev'
    const parsed = parsePsLine(line)
    assert.ok(parsed)
    assert.equal(parsed.pid, 1001)
    assert.equal(parsed.ppid, 1)
    assert.equal(parsed.etime, '05:20')
    assert.equal(parsed.time, '00:12.34')
    assert.equal(parsed.comm, 'python3')
    assert.equal(parsed.args, '/usr/bin/python3 main.py --profile dev')
    assert.equal(parsed.command, '/usr/bin/python3 main.py --profile dev')
  })

  it('truncates command to 160 characters', () => {
    const longArgs = 'node server.js ' + 'a'.repeat(200)
    const line = `  2000   100   00:10   00:01.00  node  ${longArgs}`
    const parsed = parsePsLine(line)
    assert.ok(parsed)
    assert.equal(parsed.command.length, 160)
  })

  it('parses multiple lines and skips empty lines', () => {
    const text = `
   100      1   10:00   01:00.00  Electron  /Applications/Hermes.app/Contents/MacOS/Hermes
   200    100   09:30   00:45.00  python3   python3 -m hermes_cli.main serve

   300    200   00:05   00:00.10  git       git status
`

    const procs = parsePsOutput(text)
    assert.equal(procs.length, 3)
    assert.equal(procs[0].pid, 100)
    assert.equal(procs[1].pid, 200)
    assert.equal(procs[2].pid, 300)
  })
})

describe('matchOwningSessionOrLane', () => {
  it('matches /tmp/lane-* paths', () => {
    const result = matchOwningSessionOrLane('git -C /tmp/lane-b9-proc-panel status')
    assert.equal(result, '/tmp/lane-b9-proc-panel')
  })

  it('matches .claude/worktrees/lane-* paths', () => {
    const result = matchOwningSessionOrLane('node worker.js --dir .claude/worktrees/lane-w_20260929T024309Z_1c4a')
    assert.equal(result, '.claude/worktrees/lane-w_20260929T024309Z_1c4a')
  })

  it('matches --profile arguments', () => {
    const result = matchOwningSessionOrLane('python3 -m hermes_cli.main serve --profile test-profile')
    assert.equal(result, 'profile:test-profile')
  })

  it('matches known session cwds if provided', () => {
    const knownCwds = { 'session-xyz': '/home/user/project' }
    const result = matchOwningSessionOrLane('rg pattern /home/user/project/src', undefined, knownCwds)
    assert.equal(result, 'session-xyz')
  })

  it('returns empty string if no session or lane matched', () => {
    const result = matchOwningSessionOrLane('bash -i')
    assert.equal(result, '')
  })
})

describe('buildProcessTree', () => {
  it('recursively gathers descendants of root PIDs and excludes unrelated processes', () => {
    const sample = [
      { pid: 100, ppid: 1, etime: '10:00', time: '01:00', comm: 'electron', args: 'electron', command: 'electron' },
      { pid: 101, ppid: 100, etime: '09:00', time: '00:30', comm: 'helper', args: 'helper', command: 'helper' },
      { pid: 102, ppid: 101, etime: '08:00', time: '00:10', comm: 'worker', args: 'worker', command: 'worker' },
      { pid: 200, ppid: 1, etime: '09:00', time: '00:40', comm: 'python', args: 'python', command: 'python' },
      { pid: 201, ppid: 200, etime: '02:00', time: '00:05', comm: 'git', args: 'git', command: 'git' },
      { pid: 999, ppid: 1, etime: '50:00', time: '10:00', comm: 'other', args: 'other', command: 'other' }
    ]

    const tree = buildProcessTree(sample, [100, 200])
    const pids = tree.map(p => p.pid)

    assert.ok(pids.includes(100))
    assert.ok(pids.includes(101))
    assert.ok(pids.includes(102))
    assert.ok(pids.includes(200))
    assert.ok(pids.includes(201))
    assert.ok(!pids.includes(999))

    // Depth check
    const depthMap = new Map(tree.map(p => [p.pid, p.depth]))
    assert.equal(depthMap.get(100), 0)
    assert.equal(depthMap.get(101), 1)
    assert.equal(depthMap.get(102), 2)
    assert.equal(depthMap.get(200), 0)
    assert.equal(depthMap.get(201), 1)
  })
})

describe('ProcessTreeTracker (CPU delta & Spawn rate)', () => {
  it('computes CPU deltas over last 10s and rolling spawn rate over 60s', () => {
    const tracker = new ProcessTreeTracker()

    // Sample 1 at T = 0
    const raw1 = [
      { pid: 100, ppid: 1, etime: '00:01', time: '00:01.00', comm: 'electron', args: 'electron', command: 'electron' },
      { pid: 200, ppid: 100, etime: '00:01', time: '00:02.00', comm: 'python', args: 'python', command: 'python' }
    ]

    const s1 = tracker.update(raw1, [100], 0)
    assert.equal(s1.processes.length, 2)
    // First sample delta should be 0
    assert.equal(s1.processes.find(p => p.pid === 200)?.cpuTime10s, 0)
    // First sample establishes baseline, 0 spawns
    assert.equal(s1.spawnsPerMinute.length, 0)

    // Sample 2 at T = 5000 (5s later)
    // PID 200 used 1.5s more CPU (time is now 00:03.50)
    // New child PID 300 (git) spawned
    const raw2 = [
      { pid: 100, ppid: 1, etime: '00:06', time: '00:01.20', comm: 'electron', args: 'electron', command: 'electron' },
      { pid: 200, ppid: 100, etime: '00:06', time: '00:03.50', comm: 'python', args: 'python', command: 'python' },
      { pid: 300, ppid: 200, etime: '00:02', time: '00:00.40', comm: 'git', args: 'git status', command: 'git status' }
    ]

    const s2 = tracker.update(raw2, [100], 5000)
    assert.equal(s2.processes.length, 3)
    // Delta for PID 200: 3.5 - 2.0 = 1.5s
    assert.equal(s2.processes.find(p => p.pid === 200)?.cpuTime10s, 1.5)
    // Spawn counter: git spawned once
    assert.deepEqual(s2.spawnsPerMinute, [{ basename: 'git', count: 1 }])

    // Sample 3 at T = 10000 (10s later from start)
    // PID 200 used 1.0s more CPU (time is now 00:04.50) -> total delta over 10s is 4.5 - 2.0 = 2.5s
    // New child PID 301 (git) spawned, PID 400 (rg) spawned
    const raw3 = [
      { pid: 100, ppid: 1, etime: '00:11', time: '00:01.50', comm: 'electron', args: 'electron', command: 'electron' },
      { pid: 200, ppid: 100, etime: '00:11', time: '00:04.50', comm: 'python', args: 'python', command: 'python' },
      { pid: 300, ppid: 200, etime: '00:07', time: '00:00.60', comm: 'git', args: 'git diff', command: 'git diff' },
      { pid: 301, ppid: 200, etime: '00:01', time: '00:00.10', comm: 'git', args: 'git log', command: 'git log' },
      { pid: 400, ppid: 200, etime: '00:01', time: '00:00.20', comm: 'rg', args: 'rg foo', command: 'rg foo' }
    ]

    const s3 = tracker.update(raw3, [100], 10000)
    assert.equal(s3.processes.length, 5)
    // Delta for PID 200: 4.50 - 2.00 = 2.50s (over the 10s window)
    assert.equal(s3.processes.find(p => p.pid === 200)?.cpuTime10s, 2.5)
    // Spawns in rolling 60s: git=2, rg=1
    assert.deepEqual(s3.spawnsPerMinute, [
      { basename: 'git', count: 2 },
      { basename: 'rg', count: 1 }
    ])

    // Sample 4 at T = 75000 (more than 60s after spawns)
    // No new spawns, PID 300, 301, 400 exited
    const raw4 = [
      { pid: 100, ppid: 1, etime: '01:16', time: '00:02.00', comm: 'electron', args: 'electron', command: 'electron' },
      { pid: 200, ppid: 100, etime: '01:16', time: '00:06.00', comm: 'python', args: 'python', command: 'python' }
    ]

    const s4 = tracker.update(raw4, [100], 75000)
    // The previous spawns from T=5000 and T=10000 are older than 60s, so spawn count is 0
    assert.equal(s4.spawnsPerMinute.length, 0)
  })
})

describe('registerProcessTreeIpc', () => {
  it('starts sampling on subscribe and stops when unsubscribed', async () => {
    const listeners = new Map<string, (event: any, ...args: any[]) => void>()
    const handles = new Map<string, (event: any, ...args: any[]) => Promise<any>>()

    const mockIpc = {
      on: (channel: string, handler: any) => listeners.set(channel, handler),
      handle: (channel: string, handler: any) => handles.set(channel, handler)
    } as any

    let psCalls = 0

    const mockExecPs = async () => {
      psCalls++

      return '  100    1   01:00   00:05.00  electron  electron'
    }

    const sentEvents: Array<{ channel: string; data: any }> = []
    let destroyed = false
    const destroyedListeners = new Set<() => void>()

    const mockSender = {
      isDestroyed: () => destroyed,
      send: (channel: string, data: any) => sentEvents.push({ channel, data }),
      once: (event: string, fn: () => void) => {
        if (event === 'destroyed') {destroyedListeners.add(fn)}
      }
    } as any

    const service = registerProcessTreeIpc({
      ipcMain: mockIpc,
      getRootPids: () => [100],
      execPs: mockExecPs,
      sampleIntervalMs: 1000
    })

    // No calls before subscribe
    assert.equal(psCalls, 0)

    // Subscribe
    const subscribeHandler = listeners.get('hermes:process-tree:subscribe')
    assert.ok(subscribeHandler)
    subscribeHandler({ sender: mockSender })

    // Immediate sample triggered
    await new Promise(r => setTimeout(r, 10))
    assert.equal(psCalls, 1)
    assert.equal(sentEvents.length, 1)
    assert.equal(sentEvents[0].channel, 'hermes:process-tree:update')
    assert.equal(sentEvents[0].data.processes.length, 1)
    assert.equal(sentEvents[0].data.processes[0].pid, 100)

    // Unsubscribe
    const unsubscribeHandler = listeners.get('hermes:process-tree:unsubscribe')
    assert.ok(unsubscribeHandler)
    unsubscribeHandler({ sender: mockSender })

    service.stop()
  })
})
