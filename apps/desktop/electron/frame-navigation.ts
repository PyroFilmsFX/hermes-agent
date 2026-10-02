/**
 * Sub-frame navigation guard for MCP app frames (M9b).
 *
 * Only frames the renderer marks as MCP apps (iframe name MCP_APP_FRAME_NAME) are guarded;
 * every other iframe (embeds, preview pane, plugin hubs) navigates as before. A guarded frame
 * may only load its srcdoc (or about:blank). The frame is recorded by its frame-tree id when it
 * is created, so an app that renames itself (window.name) stays guarded; the renderer's
 * second-load close remains the backstop.
 */
export const MCP_APP_FRAME_NAME = 'hermes-mcp-app'

export function isAllowedMcpAppFrameUrl(url: string): boolean {
  const normalized = (url || '').trim().toLowerCase()

  return normalized === 'about:blank' || normalized === 'about:srcdoc' || normalized.startsWith('about:srcdoc#')
}

export interface FrameNavigationGuard {
  /** Call from webContents 'frame-created'. */
  noteFrame(frameTreeNodeId: number | undefined, name: string | undefined): void
  shouldBlock(isMainFrame: boolean, url: string, frameTreeNodeId: number | undefined, name: string | undefined): boolean
}

export function createFrameNavigationGuard(): FrameNavigationGuard {
  const guarded = new Set<number>()

  const isGuarded = (id: number | undefined, name: string | undefined) =>
    name === MCP_APP_FRAME_NAME || (typeof id === 'number' && guarded.has(id))

  return {
    noteFrame(id, name) {
      if (typeof id === 'number' && name === MCP_APP_FRAME_NAME) {
        guarded.add(id)
      }
    },
    shouldBlock(isMainFrame, url, id, name) {
      if (isMainFrame || !isGuarded(id, name)) {
        return false
      }

      return !isAllowedMcpAppFrameUrl(url)
    }
  }
}
