import { useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { notify, notifyError } from '@/store/notifications'

import type { VoiceActivityState, VoiceStatus } from '../types'

import type { MicRecording } from './use-mic-recorder'
import { useMicRecorder } from './use-mic-recorder'

export const DEFAULT_TRANSCRIBE_TIMEOUT_S = 60
export const DEFAULT_TRANSCRIBE_TIMEOUT_MS = DEFAULT_TRANSCRIBE_TIMEOUT_S * 1000

interface VoiceRecorderOptions {
  maxRecordingSeconds: number
  onTranscribeAudio?: (audio: Blob, signal?: AbortSignal) => Promise<string>
  focusInput: () => void
  onTranscript: (text: string) => void
  transcribeTimeoutMs?: number
}

export function useVoiceRecorder({
  maxRecordingSeconds,
  onTranscribeAudio,
  focusInput,
  onTranscript,
  transcribeTimeoutMs = DEFAULT_TRANSCRIBE_TIMEOUT_MS
}: VoiceRecorderOptions) {
  const { t } = useI18n()
  const voiceCopy = t.notifications.voice
  const { handle, level, recording } = useMicRecorder(voiceCopy)
  const handleRef = useRef(handle)
  handleRef.current = handle

  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>('idle')
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const startedAtRef = useRef(0)
  const intervalRef = useRef<number | null>(null)
  const timeoutRef = useRef<number | null>(null)
  const transcribeTimeoutRef = useRef<number | null>(null)
  const transcribeAbortRef = useRef<AbortController | null>(null)
  const transcribeCancelledRef = useRef(false)
  const mountedRef = useRef(true)

  const clearTimers = () => {
    if (intervalRef.current) {
      window.clearInterval(intervalRef.current)
      intervalRef.current = null
    }

    if (timeoutRef.current) {
      window.clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }

    if (transcribeTimeoutRef.current) {
      window.clearTimeout(transcribeTimeoutRef.current)
      transcribeTimeoutRef.current = null
    }
  }

  const cancel = () => {
    clearTimers()
    transcribeCancelledRef.current = true
    transcribeAbortRef.current?.abort()
    transcribeAbortRef.current = null
    handleRef.current.cancel()

    if (mountedRef.current) {
      setVoiceStatus('idle')
      setElapsedSeconds(0)
    }
  }

  useEffect(() => {
    mountedRef.current = true

    return () => {
      mountedRef.current = false
      clearTimers()
      transcribeCancelledRef.current = true
      transcribeAbortRef.current?.abort()
      transcribeAbortRef.current = null
      handleRef.current.cancel()
    }
  }, [])

  const stop = async () => {
    clearTimers()

    let result: MicRecording | null = null

    try {
      result = await handleRef.current.stop()
    } catch (error) {
      if (mountedRef.current) {
        setVoiceStatus('idle')
        setElapsedSeconds(0)
        notifyError(error, voiceCopy.recordingFailed)
      }

      return
    }

    if (!result || result.audio.size === 0) {
      if (mountedRef.current) {
        setVoiceStatus('idle')
        setElapsedSeconds(0)
      }

      return
    }

    if (!onTranscribeAudio) {
      if (mountedRef.current) {
        setVoiceStatus('idle')
        setElapsedSeconds(0)
      }

      return
    }

    if (!mountedRef.current) {
      return
    }

    setVoiceStatus('transcribing')

    transcribeCancelledRef.current = false
    const abortController = new AbortController()
    transcribeAbortRef.current = abortController

    let timeoutId: number | null = null
    const timeoutPromise = new Promise<never>((_, reject) => {
      timeoutId = window.setTimeout(() => {
        abortController.abort()
        reject(new Error(`Transcription timed out after ${Math.round(transcribeTimeoutMs / 1000)}s`))
      }, transcribeTimeoutMs)
      transcribeTimeoutRef.current = timeoutId
    })

    const abortPromise = new Promise<never>((_, reject) => {
      if (abortController.signal.aborted) {
        reject(new Error('Transcription aborted'))
      } else {
        abortController.signal.addEventListener('abort', () => reject(new Error('Transcription aborted')), {
          once: true
        })
      }
    })

    try {
      const raw = await Promise.race([
        onTranscribeAudio(result.audio, abortController.signal),
        timeoutPromise,
        abortPromise
      ])

      if (timeoutId !== null) {
        window.clearTimeout(timeoutId)
        transcribeTimeoutRef.current = null
      }

      if (!mountedRef.current || transcribeCancelledRef.current) {
        return
      }

      const transcript = (raw ?? '').trim()

      if (!transcript) {
        notify({ kind: 'warning', title: voiceCopy.noSpeechDetected, message: voiceCopy.tryRecordingAgain })
      } else {
        onTranscript(transcript)
      }
    } catch (error) {
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId)
        transcribeTimeoutRef.current = null
      }

      if (!mountedRef.current || transcribeCancelledRef.current) {
        return
      }

      notifyError(error, voiceCopy.transcriptionFailed)
    } finally {
      transcribeAbortRef.current = null

      if (mountedRef.current) {
        setVoiceStatus('idle')
        setElapsedSeconds(0)
        focusInput()
      }
    }
  }

  const start = async () => {
    if (!onTranscribeAudio) {
      notify({ kind: 'warning', title: voiceCopy.unavailable, message: voiceCopy.transcriptionUnavailable })

      return
    }

    try {
      await handleRef.current.start({ onError: error => notifyError(error, voiceCopy.recordingFailed) })

      if (!mountedRef.current) {
        handleRef.current.cancel()

        return
      }

      startedAtRef.current = Date.now()
      setElapsedSeconds(0)
      setVoiceStatus('recording')
      intervalRef.current = window.setInterval(
        () => setElapsedSeconds((Date.now() - startedAtRef.current) / 1000),
        250
      )
      const cap = Math.max(1, Math.min(Math.trunc(maxRecordingSeconds), 600))
      timeoutRef.current = window.setTimeout(() => void stop(), cap * 1000)
    } catch (error) {
      if (mountedRef.current) {
        setVoiceStatus('idle')
        setElapsedSeconds(0)
        notifyError(error, voiceCopy.recordingFailed)
      }
    }
  }

  const dictate = () => {
    if (recording) {
      void stop()
    } else if (voiceStatus === 'transcribing') {
      cancel()
    } else if (voiceStatus === 'idle') {
      void start()
    }
  }

  const voiceActivityState: VoiceActivityState = {
    elapsedSeconds,
    level,
    status: voiceStatus
  }

  return { cancel, dictate, voiceActivityState, voiceStatus }
}
