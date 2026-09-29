/**
 * Guard sub-frame navigation for MCP app frames: prevent navigation of non-main
 * frames to URLs other than about:srcdoc / about:blank.
 */
export function shouldBlockFrameNavigation(isMainFrame: boolean, url: string): boolean {
  if (isMainFrame) {
    return false
  }
  const normalized = (url || '').trim().toLowerCase()
  if (normalized === 'about:srcdoc' || normalized === 'about:blank') {
    return false
  }
  return true
}
