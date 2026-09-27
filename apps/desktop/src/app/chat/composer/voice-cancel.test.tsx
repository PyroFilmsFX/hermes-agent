import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import type { ChatBarState } from './types'
import { VoiceFan } from './voice-fan'
import { VoiceMenu } from './voice-menu'

afterEach(cleanup)

const state = { voice: { active: false, enabled: true } } as ChatBarState

describe('transcription cancellation controls', () => {
  it('keeps the mic fan hub enabled and calls dictate to cancel', () => {
    const onDictate = vi.fn()
    render(
      <I18nProvider>
        <VoiceFan
          autoSpeak={false}
          disabled
          onDictate={onDictate}
          onToggleAutoSpeak={() => undefined}
          state={state}
          voiceStatus="transcribing"
        />
      </I18nProvider>
    )

    const cancel = screen.getByRole('button', { name: 'Cancel transcription' })
    expect((cancel as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(cancel)
    expect(onDictate).toHaveBeenCalledOnce()
  })

  it('keeps the voice menu dictation item enabled and calls dictate to cancel', () => {
    const onDictate = vi.fn()
    render(
      <I18nProvider>
        <VoiceMenu
          autoSpeak={false}
          disabled
          onDictate={onDictate}
          onStartConversation={() => undefined}
          onToggleAutoSpeak={() => undefined}
          state={state}
          voiceStatus="transcribing"
        />
      </I18nProvider>
    )

    const trigger = screen.getByRole('button', { name: 'Cancel transcription' })
    expect((trigger as HTMLButtonElement).disabled).toBe(false)
    fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(trigger)
    const cancel = screen.getByRole('menuitemcheckbox', { name: 'Cancel transcription' })
    expect(cancel.getAttribute('aria-disabled')).not.toBe('true')
    fireEvent.click(cancel)
    expect(onDictate).toHaveBeenCalledOnce()
  })
})
