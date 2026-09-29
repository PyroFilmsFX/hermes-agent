import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ProcessesPanel } from './processes-panel'

describe('ProcessesPanel', () => {
  const originalHermesDesktop = window.hermesDesktop

  beforeEach(() => {
    cleanup()
  })

  afterEach(() => {
    cleanup()
    window.hermesDesktop = originalHermesDesktop
    vi.clearAllMocks()
  })

  it('subscribes on mount and unsubscribes on unmount', () => {
    let subscriberCallback: ((snapshot: DesktopProcessTreeSnapshot) => void) | null = null
    const unsubscribeMock = vi.fn()
    const subscribeMock = vi.fn((cb: (snapshot: DesktopProcessTreeSnapshot) => void) => {
      subscriberCallback = cb
      return unsubscribeMock
    })

    window.hermesDesktop = {
      ...originalHermesDesktop,
      processTree: {
        get: vi.fn(),
        subscribe: subscribeMock
      }
    } as any

    // 1. Mount (open panel)
    const { unmount } = render(<ProcessesPanel />)
    expect(subscribeMock).toHaveBeenCalledTimes(1)
    expect(unsubscribeMock).not.toHaveBeenCalled()

    // 2. Unmount (close panel)
    unmount()
    expect(unsubscribeMock).toHaveBeenCalledTimes(1)
  })

  it('renders table sorted by CPU (10s) descending and displays spawns per minute', () => {
    let subscriberCallback: ((snapshot: DesktopProcessTreeSnapshot) => void) | null = null
    const unsubscribeMock = vi.fn()

    window.hermesDesktop = {
      ...originalHermesDesktop,
      processTree: {
        get: vi.fn(),
        subscribe: (cb: (snapshot: DesktopProcessTreeSnapshot) => void) => {
          subscriberCallback = cb
          return unsubscribeMock
        }
      }
    } as any

    render(<ProcessesPanel />)

    const sampleSnapshot: DesktopProcessTreeSnapshot = {
      processes: [
        {
          pid: 100,
          ppid: 1,
          command: 'electron',
          comm: 'electron',
          age: '05:00',
          ageSeconds: 300,
          cpuTime10s: 0.15,
          laneOrSession: '',
          depth: 0
        },
        {
          pid: 200,
          ppid: 100,
          command: 'python -m hermes_cli.main serve --profile test',
          comm: 'python',
          age: '04:50',
          ageSeconds: 290,
          cpuTime10s: 2.85,
          laneOrSession: 'profile:test',
          depth: 1
        },
        {
          pid: 300,
          ppid: 200,
          command: 'git status',
          comm: 'git',
          age: '00:02',
          ageSeconds: 2,
          cpuTime10s: 0.05,
          laneOrSession: '/tmp/lane-b9-proc-panel',
          depth: 2
        }
      ],
      spawnsPerMinute: [
        { basename: 'git', count: 3 },
        { basename: 'python', count: 1 }
      ],
      timestamp: Date.now()
    }

    act(() => {
      subscriberCallback?.(sampleSnapshot)
    })

    // Check spawns per minute
    expect(screen.getByTestId('spawns-list')).toBeTruthy()
    expect(screen.getByText('git')).toBeTruthy()
    expect(screen.getByText('3/min')).toBeTruthy()
    expect(screen.getByText('python')).toBeTruthy()
    expect(screen.getByText('1/min')).toBeTruthy()

    // Check table rows
    const rows = screen.getAllByTestId('process-row')
    expect(rows).toHaveLength(3)

    // First row should be PID 200 because it has highest CPU (2.85s > 0.15s > 0.05s)
    expect(rows[0].textContent).toContain('200')
    expect(rows[0].textContent).toContain('2.85s')
    expect(rows[0].textContent).toContain('profile:test')

    // Second row should be PID 100 (0.15s)
    expect(rows[1].textContent).toContain('100')
    expect(rows[1].textContent).toContain('0.15s')

    // Third row should be PID 300 (0.05s)
    expect(rows[2].textContent).toContain('300')
    expect(rows[2].textContent).toContain('0.05s')
    expect(rows[2].textContent).toContain('/tmp/lane-b9-proc-panel')
  })

  it('renders empty message when no processes are returned', () => {
    let subscriberCallback: ((snapshot: DesktopProcessTreeSnapshot) => void) | null = null

    window.hermesDesktop = {
      ...originalHermesDesktop,
      processTree: {
        get: vi.fn(),
        subscribe: (cb: (snapshot: DesktopProcessTreeSnapshot) => void) => {
          subscriberCallback = cb
          return vi.fn()
        }
      }
    } as any

    render(<ProcessesPanel />)

    act(() => {
      subscriberCallback?.({
        processes: [],
        spawnsPerMinute: [],
        timestamp: Date.now()
      })
    })

    expect(screen.getByTestId('no-processes-msg')).toBeTruthy()
    expect(screen.getByTestId('no-spawns-msg')).toBeTruthy()
  })
})
