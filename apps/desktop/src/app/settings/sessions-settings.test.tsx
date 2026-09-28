// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getHermesConfigRecord, listAllProfileSessions, saveHermesConfig, setSessionArchived } from '@/hermes'
import { en } from '@/i18n/en'
import { $messagingSessions, $sessions, setMessagingSessions, setSessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { SessionsSettings } from './sessions-settings'

vi.mock('@/i18n', () => ({ useI18n: () => ({ t: en }) }))

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getHermesConfigRecord: vi.fn().mockResolvedValue({ config: {}, sessions: {} }),
  saveHermesConfig: vi.fn().mockResolvedValue(undefined),
  listAllProfileSessions: vi.fn(),
  setSessionArchived: vi.fn().mockResolvedValue(undefined)
}))

const archivedMatrixSession = {
  archived: true,
  ended_at: null,
  id: 'matrix-1',
  input_tokens: 0,
  is_active: false,
  last_active: 1,
  message_count: 2,
  model: null,
  output_tokens: 0,
  preview: null,
  source: 'matrix',
  started_at: 1,
  title: 'archived room',
  tool_call_count: 0
} as SessionInfo

beforeEach(() => {
  setSessions([])
  setMessagingSessions([])
  vi.mocked(listAllProfileSessions).mockResolvedValue({ sessions: [archivedMatrixSession], total: 1 } as never)
  vi.mocked(saveHermesConfig).mockClear()
})

afterEach(() => {
  cleanup()
})

describe('SessionsSettings unarchive', () => {
  it('restores a messaging-source session into $messagingSessions, not $sessions', async () => {
    render(<SessionsSettings />)
    const button = await screen.findByRole('button', { name: en.settings.sessions.unarchive })

    await act(async () => fireEvent.click(button))

    await waitFor(() => expect(setSessionArchived).toHaveBeenCalledWith('matrix-1', false, undefined))
    expect($messagingSessions.get().map(session => session.id)).toEqual(['matrix-1'])
    expect($messagingSessions.get()[0]?.archived).toBe(false)
    expect($sessions.get()).toEqual([])
  })
})

describe('SessionsSettings auto-archive lanes', () => {
  beforeEach(() => {
    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {}
  })

  afterEach(() => {
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('toggles auto_archive_lanes and persists setting', async () => {
    vi.mocked(getHermesConfigRecord).mockResolvedValueOnce({
      config: {},
      sessions: {
        auto_archive: false,
        auto_archive_days: 3,
        auto_archive_lanes: true,
        lane_archive_hours: 6
      }
    } as never)

    render(<SessionsSettings />)

    const laneToggle = await screen.findByRole('switch', {
      name: en.settings.sessions.autoArchiveLanesTitle
    })
    expect(laneToggle).toBeDefined()

    await act(async () => {
      fireEvent.click(laneToggle)
    })

    expect(saveHermesConfig).toHaveBeenCalledWith(
      {
        sessions: {
          auto_archive_lanes: false,
          lane_archive_hours: 6
        }
      },
      undefined
    )
  })

  it('updates lane_archive_hours on blur and persists setting', async () => {
    vi.mocked(getHermesConfigRecord).mockResolvedValueOnce({
      config: {},
      sessions: {
        auto_archive: false,
        auto_archive_days: 3,
        auto_archive_lanes: true,
        lane_archive_hours: 6
      }
    } as never)

    render(<SessionsSettings />)

    const hoursInput = await screen.findByLabelText(en.settings.sessions.autoArchiveLanesHoursLabel)
    expect((hoursInput as HTMLInputElement).value).toBe('6')

    fireEvent.change(hoursInput, { target: { value: '12' } })
    fireEvent.blur(hoursInput)

    expect(saveHermesConfig).toHaveBeenCalledWith(
      {
        sessions: {
          auto_archive_lanes: true,
          lane_archive_hours: 12
        }
      },
      undefined
    )
  })
})
