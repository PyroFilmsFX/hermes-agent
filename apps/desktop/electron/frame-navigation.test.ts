import { describe, expect, it } from 'vitest'

import { createFrameNavigationGuard, MCP_APP_FRAME_NAME } from './frame-navigation'

describe('MCP app frame navigation guard', () => {
  it('never blocks the main frame', () => {
    const guard = createFrameNavigationGuard()
    expect(guard.shouldBlock(true, 'https://example.com', 1, MCP_APP_FRAME_NAME)).toBe(false)
  })

  it('leaves other iframes alone (embeds, preview pane, plugin hubs)', () => {
    const guard = createFrameNavigationGuard()
    guard.noteFrame(7, '')
    expect(guard.shouldBlock(false, 'https://www.youtube.com/embed/x', 7, '')).toBe(false)
    expect(guard.shouldBlock(false, 'http://127.0.0.1:5173/', 8, undefined)).toBe(false)
  })

  it('lets an MCP app frame load its srcdoc, fragments and about:blank', () => {
    const guard = createFrameNavigationGuard()
    guard.noteFrame(3, MCP_APP_FRAME_NAME)
    for (const url of ['about:srcdoc', 'about:SrcDoc', 'about:srcdoc#section', 'about:blank']) {
      expect(guard.shouldBlock(false, url, 3, MCP_APP_FRAME_NAME)).toBe(false)
    }
  })

  it('blocks an MCP app frame from navigating anywhere else', () => {
    const guard = createFrameNavigationGuard()
    guard.noteFrame(3, MCP_APP_FRAME_NAME)
    for (const url of ['https://example.com', 'http://example.com', 'file:///etc/passwd', 'javascript:alert(1)']) {
      expect(guard.shouldBlock(false, url, 3, MCP_APP_FRAME_NAME)).toBe(true)
    }
  })

  it('keeps guarding an MCP app frame that renamed itself', () => {
    const guard = createFrameNavigationGuard()
    guard.noteFrame(3, MCP_APP_FRAME_NAME)
    expect(guard.shouldBlock(false, 'https://evil.example', 3, 'renamed')).toBe(true)
    expect(guard.shouldBlock(false, 'https://evil.example', 4, 'renamed')).toBe(false)
  })
})
