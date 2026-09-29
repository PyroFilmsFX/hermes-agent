import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const stylesPath = resolve(dirname(fileURLToPath(import.meta.url)), 'styles.css')
const stylesCss = readFileSync(stylesPath, 'utf8')

describe('styles.css animation pause and compositor optimizations', () => {
  it('covers all required continuous animation selectors under data-renderer-animations-paused', () => {
    // Find the pause rule that sets animation-play-state: paused
    const pauseRuleMatch = stylesCss.match(
      /:root\[data-renderer-animations-paused\][\s\S]*?\{[\s\S]*?animation-play-state:\s*paused[\s\S]*?\}/
    )
    expect(pauseRuleMatch).not.toBeNull()
    const pauseRule = pauseRuleMatch![0]

    // (1) Asserts the pause selector covers each selector in (1)
    expect(pauseRule).toContain('.animate-spin')
    expect(pauseRule).toContain('.animate-pulse')
    expect(pauseRule).toContain('.animate-ping')
    expect(pauseRule).toContain('.arc-border::before')
    expect(pauseRule).toContain('.glyph-spinner__strip')

    // Code card stream glow
    expect(pauseRule).toMatch(/code-card.*streaming/)
    expect(pauseRule).toContain('animation-play-state: paused')
  })

  it('ensures code-card-stream-glow keyframes no longer animate box-shadow', () => {
    const keyframesMatch = stylesCss.match(/@keyframes\s+code-card-stream-glow\s*\{([\s\S]*?)\n\}/)
    expect(keyframesMatch).not.toBeNull()
    const keyframeBody = keyframesMatch![1]

    // Glow keyframes no longer animate box-shadow
    expect(keyframeBody).not.toContain('box-shadow')
    // Instead animates opacity
    expect(keyframeBody).toContain('opacity')
  })

  it('animates code-card stream glow via pseudo-element', () => {
    // The glow animation is applied to a pseudo-element (e.g. ::after)
    expect(stylesCss).toMatch(
      /\[data-slot=['"]code-card['"]\]\[data-streaming=['"]true['"]\]::after[\s\S]*?code-card-stream-glow/
    )
  })

  it('disables the arc-border ring and stream glow under prefers-reduced-motion: reduce', () => {
    // Arc-border disabled under prefers-reduced-motion
    expect(stylesCss).toMatch(
      /@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)[\s\S]*?\.arc-border[\s\S]*?display:\s*none/
    )
    // Stream glow disabled under prefers-reduced-motion
    expect(stylesCss).toMatch(
      /@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)[\s\S]*?\[data-slot=['"]code-card['"]\]\[data-streaming=['"]true['"]\]::after[\s\S]*?display:\s*none/
    )
  })

  it('removes will-change: transform from arc-border in idle states', () => {
    // In paused state or reduced-motion state, will-change is reset to auto
    expect(stylesCss).toMatch(
      /:root\[data-renderer-animations-paused\][\s\S]*?\.arc-border::before[\s\S]*?will-change:\s*auto/
    )
    expect(stylesCss).toMatch(
      /@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)[\s\S]*?\.arc-border::before[\s\S]*?will-change:\s*auto/
    )
  })
})
