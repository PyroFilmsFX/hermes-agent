import { act, cleanup, renderHook } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { MicRecording } from './use-mic-recorder'
import { DEFAULT_TRANSCRIBE_TIMEOUT_MS, useVoiceRecorder } from './use-voice-recorder'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const notify = vi.fn()
const notifyError = vi.fn()

vi.mock('@/store/notifications', () => ({
  notify: (...args: unknown[]) => notify(...args),
  notifyError: (...args: unknown[]) => notifyError(...args)
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {
          noSpeechDetected: 'No speech detected',
          tryRecordingAgain: 'Try recording again.',
          transcriptionFailed: 'Voice transcription failed',
          recordingFailed: 'Voice recording failed',
          unavailable: 'Voice unavailable',
          transcriptionUnavailable: 'Voice transcription is not available yet.'
        }
      }
    }
  })
}))

let micStopResult: MicRecording | null = {
  audio: new Blob(['audio-data'], { type: 'audio/webm' }),
  durationMs: 1200,
  heardSpeech: true
}
let micStopReject: Error | null = null

const micHandle = {
  cancel: vi.fn(),
  start: vi.fn(async (_options?: unknown) => undefined),
  stop: vi.fn(async () => {
    if (micStopReject) {
      throw micStopReject
    }
    return micStopResult
  })
}

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => {
    const [rec, setRec] = useState(false)
    return {
      handle: {
        cancel: vi.fn(() => {
          setRec(false)
          micHandle.cancel()
        }),
        start: vi.fn(async (options?: unknown) => {
          await micHandle.start(options)
          setRec(true)
        }),
        stop: vi.fn(async () => {
          setRec(false)
          return micHandle.stop()
        })
      },
      level: 0.5,
      recording: rec
    }
  }
}))

describe('useVoiceRecorder terminal paths', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    micStopResult = {
      audio: new Blob(['audio-data'], { type: 'audio/webm' }),
      durationMs: 1200,
      heardSpeech: true
    }
    micStopReject = null
  })

  it('resets to idle on successful transcription', async () => {
    const onTranscript = vi.fn()
    const focusInput = vi.fn()
    const onTranscribeAudio = vi.fn(async () => 'Hello Hermes')

    const { result } = renderHook(() =>
      useVoiceRecorder({
        focusInput,
        maxRecordingSeconds: 60,
        onTranscribeAudio,
        onTranscript
      })
    )

    expect(result.current.voiceStatus).toBe('idle')
    expect(result.current.voiceActivityState.status).toBe('idle')

    // Start recording
    await act(async () => {
      result.current.dictate()
    })
    expect(result.current.voiceStatus).toBe('recording')

    // Stop recording -> transcribing -> success -> idle
    await act(async () => {
      result.current.dictate()
    })

    expect(onTranscribeAudio).toHaveBeenCalledTimes(1)
    expect(onTranscript).toHaveBeenCalledWith('Hello Hermes')
    expect(result.current.voiceStatus).toBe('idle')
    expect(result.current.voiceActivityState.status).toBe('idle')
    expect(focusInput).toHaveBeenCalled()
  })

  it('resets to idle when transcription returns empty text', async () => {
    const onTranscript = vi.fn()
    const focusInput = vi.fn()
    const onTranscribeAudio = vi.fn(async () => '   ')

    const { result } = renderHook(() =>
      useVoiceRecorder({
        focusInput,
        maxRecordingSeconds: 60,
        onTranscribeAudio,
        onTranscript
      })
    )

    await act(async () => {
      result.current.dictate()
    })
    await act(async () => {
      result.current.dictate()
    })

    expect(result.current.voiceStatus).toBe('idle')
    expect(result.current.voiceActivityState.status).toBe('idle')
    expect(onTranscript).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: 'warning',
        title: 'No speech detected'
      })
    )
  })

  it('resets to idle when transcription errors', async () => {
    const onTranscript = vi.fn()
    const focusInput = vi.fn()
    const onTranscribeAudio = vi.fn(async () => {
      throw new Error('Network error during transcription')
    })

    const { result } = renderHook(() =>
      useVoiceRecorder({
        focusInput,
        maxRecordingSeconds: 60,
        onTranscribeAudio,
        onTranscript
      })
    )

    await act(async () => {
      result.current.dictate()
    })
    await act(async () => {
      result.current.dictate()
    })

    expect(result.current.voiceStatus).toBe('idle')
    expect(result.current.voiceActivityState.status).toBe('idle')
    expect(notifyError).toHaveBeenCalledWith(
      expect.any(Error),
      'Voice transcription failed'
    )
  })

  it('resets to idle and aborts in-flight transcription when dictate toggled while transcribing', async () => {
    let resolveTranscribe!: (text: string) => void
    const transcribePromise = new Promise<string>(resolve => {
      resolveTranscribe = resolve
    })
    const onTranscribeAudio = vi.fn(() => transcribePromise)

    const { result } = renderHook(() =>
      useVoiceRecorder({
        focusInput: vi.fn(),
        maxRecordingSeconds: 60,
        onTranscribeAudio,
        onTranscript: vi.fn()
      })
    )

    await act(async () => {
      result.current.dictate()
    })
    act(() => {
      result.current.dictate() // triggers stop
    })
    await act(async () => {
      await Promise.resolve() // let handle.stop resolve -> transcribing
    })

    expect(result.current.voiceStatus).toBe('transcribing')

    // While transcribing, user clicks dictate again to abort/cancel
    act(() => {
      result.current.dictate()
    })

    expect(result.current.voiceStatus).toBe('idle')
    expect(result.current.voiceActivityState.status).toBe('idle')

    // Resolving later does not flip it back or error
    await act(async () => {
      resolveTranscribe('late text')
    })
    expect(result.current.voiceStatus).toBe('idle')
  })

  it('resets to idle and cancels in-flight transcription via cancel()', async () => {
    let resolveTranscribe!: (text: string) => void
    const transcribePromise = new Promise<string>(resolve => {
      resolveTranscribe = resolve
    })
    const onTranscribeAudio = vi.fn(() => transcribePromise)

    const { result } = renderHook(() =>
      useVoiceRecorder({
        focusInput: vi.fn(),
        maxRecordingSeconds: 60,
        onTranscribeAudio,
        onTranscript: vi.fn()
      })
    )

    await act(async () => {
      result.current.dictate()
    })
    act(() => {
      result.current.dictate()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current.voiceStatus).toBe('transcribing')

    act(() => {
      result.current.cancel()
    })

    expect(result.current.voiceStatus).toBe('idle')
    expect(result.current.voiceActivityState.status).toBe('idle')
  })

  it('resets to idle and notifies error when transcription times out', async () => {
    vi.useFakeTimers()
    const onTranscribeAudio = vi.fn(() => new Promise<string>(() => {})) // never resolves

    const { result } = renderHook(() =>
      useVoiceRecorder({
        focusInput: vi.fn(),
        maxRecordingSeconds: 60,
        onTranscribeAudio,
        onTranscript: vi.fn()
      })
    )

    await act(async () => {
      result.current.dictate()
    })
    act(() => {
      result.current.dictate()
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(result.current.voiceStatus).toBe('transcribing')

    // Advance timers past DEFAULT_TRANSCRIBE_TIMEOUT_MS (60s)
    await act(async () => {
      vi.advanceTimersByTime(DEFAULT_TRANSCRIBE_TIMEOUT_MS + 100)
      await Promise.resolve()
    })

    expect(result.current.voiceStatus).toBe('idle')
    expect(result.current.voiceActivityState.status).toBe('idle')
    expect(notifyError).toHaveBeenCalledWith(
      expect.any(Error),
      'Voice transcription failed'
    )
    vi.useRealTimers()
  })

  it('resets to idle when recording is stopped before any audio (null or 0-byte result)', async () => {
    micStopResult = null
    const onTranscribeAudio = vi.fn(async () => 'Hello')

    const { result } = renderHook(() =>
      useVoiceRecorder({
        focusInput: vi.fn(),
        maxRecordingSeconds: 60,
        onTranscribeAudio,
        onTranscript: vi.fn()
      })
    )

    await act(async () => {
      result.current.dictate()
    })
    await act(async () => {
      result.current.dictate()
    })

    expect(result.current.voiceStatus).toBe('idle')
    expect(result.current.voiceActivityState.status).toBe('idle')
    expect(onTranscribeAudio).not.toHaveBeenCalled()

    // Now test with 0-byte audio blob
    micStopResult = {
      audio: new Blob([], { type: 'audio/webm' }),
      durationMs: 0,
      heardSpeech: false
    }

    await act(async () => {
      result.current.dictate()
    })
    await act(async () => {
      result.current.dictate()
    })

    expect(result.current.voiceStatus).toBe('idle')
    expect(result.current.voiceActivityState.status).toBe('idle')
    expect(onTranscribeAudio).not.toHaveBeenCalled()
  })

  it('resets to idle when handle.stop() throws an error', async () => {
    micStopReject = new Error('Hardware mic error during stop')
    const onTranscribeAudio = vi.fn(async () => 'Hello')

    const { result } = renderHook(() =>
      useVoiceRecorder({
        focusInput: vi.fn(),
        maxRecordingSeconds: 60,
        onTranscribeAudio,
        onTranscript: vi.fn()
      })
    )

    await act(async () => {
      result.current.dictate()
    })
    await act(async () => {
      result.current.dictate()
    })

    expect(result.current.voiceStatus).toBe('idle')
    expect(result.current.voiceActivityState.status).toBe('idle')
    expect(onTranscribeAudio).not.toHaveBeenCalled()
  })

  it('cleans up and resets to idle when unmounted mid-request', async () => {
    let resolveTranscribe!: (text: string) => void
    const transcribePromise = new Promise<string>(resolve => {
      resolveTranscribe = resolve
    })
    const onTranscribeAudio = vi.fn(() => transcribePromise)
    const onTranscript = vi.fn()
    const focusInput = vi.fn()

    const { result, unmount } = renderHook(() =>
      useVoiceRecorder({
        focusInput,
        maxRecordingSeconds: 60,
        onTranscribeAudio,
        onTranscript
      })
    )

    await act(async () => {
      result.current.dictate()
    })
    act(() => {
      result.current.dictate()
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(result.current.voiceStatus).toBe('transcribing')

    unmount()

    // Resolving late after unmount must not trigger callbacks or errors
    await act(async () => {
      resolveTranscribe('late transcript')
    })

    expect(onTranscript).not.toHaveBeenCalled()
    expect(focusInput).not.toHaveBeenCalled()
  })
})
