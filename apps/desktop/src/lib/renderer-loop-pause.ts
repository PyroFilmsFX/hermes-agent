interface WindowStatePayload {
  isMinimized?: boolean
  isVisible?: boolean
}

export const RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE = 'data-renderer-animations-paused'

export function createRendererLoopPauseController(onChange: () => void, { pauseWhenUnfocused = false } = {}) {
  let windowPaused = false
  let windowFocused = document.hasFocus()

  const onVisibilityChange = () => onChange()

  const onBlur = () => {
    if (windowFocused) {
      windowFocused = false
      onChange()
    }
  }

  const onFocus = () => {
    if (!windowFocused) {
      windowFocused = true
      onChange()
    }
  }

  const offWindowState = window.hermesDesktop?.onWindowStateChanged?.((payload: WindowStatePayload) => {
    const next = payload?.isMinimized === true || payload?.isVisible === false

    if (windowPaused === next) {
      return
    }

    windowPaused = next
    onChange()
  })

  document.addEventListener('visibilitychange', onVisibilityChange)

  if (pauseWhenUnfocused) {
    window.addEventListener('blur', onBlur)
    window.addEventListener('focus', onFocus)
  }

  return {
    dispose: () => {
      document.removeEventListener('visibilitychange', onVisibilityChange)
      window.removeEventListener('blur', onBlur)
      window.removeEventListener('focus', onFocus)
      offWindowState?.()
    },
    isPaused: () => document.visibilityState === 'hidden' || (pauseWhenUnfocused && !windowFocused) || windowPaused
  }
}

export const BLUR_PAUSE_DELAY_MS = 2_000

/**
 * Mirrors the main window's observability onto :root so continuous decorative
 * CSS animations can sleep with the JS renderer loops. The caller owns the
 * returned cleanup; overlay windows intentionally do not install this state.
 *
 * Hidden and minimized windows pause animations immediately. Unfocused windows
 * pause animations after ~2 s of blur, and resume immediately upon focus.
 */
export function installRendererAnimationPauseState({ blurDelayMs = BLUR_PAUSE_DELAY_MS } = {}): () => void {
  const root = document.documentElement
  let controller: ReturnType<typeof createRendererLoopPauseController>
  let blurTimer: ReturnType<typeof setTimeout> | null = null
  let isBlurPaused = false

  const clearBlurTimer = () => {
    if (blurTimer !== null) {
      clearTimeout(blurTimer)
      blurTimer = null
    }
  }

  const sync = () => {
    const isPaused = controller.isPaused() || isBlurPaused
    root.toggleAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE, isPaused)
  }

  const armBlurTimer = () => {
    clearBlurTimer()
    if (!document.hasFocus()) {
      blurTimer = setTimeout(() => {
        blurTimer = null
        if (!document.hasFocus()) {
          isBlurPaused = true
          sync()
        }
      }, blurDelayMs)
    }
  }

  const onBlur = () => {
    armBlurTimer()
  }

  const onFocus = () => {
    clearBlurTimer()
    if (isBlurPaused) {
      isBlurPaused = false
      sync()
    }
  }

  controller = createRendererLoopPauseController(sync)

  window.addEventListener('blur', onBlur)
  window.addEventListener('focus', onFocus)

  if (!document.hasFocus()) {
    armBlurTimer()
  }

  sync()

  return () => {
    clearBlurTimer()
    window.removeEventListener('blur', onBlur)
    window.removeEventListener('focus', onFocus)
    controller.dispose()
    root.removeAttribute(RENDERER_ANIMATIONS_PAUSED_ATTRIBUTE)
  }
}
