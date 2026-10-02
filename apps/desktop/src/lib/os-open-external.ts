/**
 * The OS "open externally" door: hand a URL to the system default handler
 * through the Electron bridge (main validates the scheme and raises the
 * copy-the-link fallback dialog when the shell refuses).
 *
 * Shared by the plugin host (`PluginOs.openExternal`, contrib/plugin.ts) and
 * core surfaces that need the same never-throws contract, e.g. the MCP
 * elicitation URL consent card. Resolves `false` when there is no bridge (a
 * plain browser, an older shell) or the bridge rejects.
 */
export async function openExternalDoor(url: string): Promise<boolean> {
  const bridge = typeof window === 'undefined' ? undefined : window.hermesDesktop

  if (!bridge?.openExternal) {
    return false
  }

  try {
    await bridge.openExternal(url)

    return true
  } catch {
    return false
  }
}
