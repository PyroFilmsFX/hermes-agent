/** Gateway display hints are not the tool's result. Keep them off the raw
 * payload so summaries cannot overwrite evidence, including false/null/"". */
export interface ToolResultMetadata {
  duration_s?: number
  error?: string | boolean
  inline_diff?: string
  message?: string
  preview?: string
  summary?: string
  todos?: unknown
  /** SDK lane: the live result was capped; the card labels the omitted count. */
  truncated?: { shown: number; total: number }
  /** SDK lane: ToolResultBlock / tool_use_result metadata passthrough. */
  tool_use_result?: unknown
}

export interface ToolResultSource {
  result?: unknown
  toolResultMetadata?: ToolResultMetadata
}

/** A derived record for existing presentation code; never stored as the result. */
export function toolResultRecord(source: ToolResultSource): Record<string, unknown> {
  let value = source.result

  if (typeof value === 'string') {
    try {
      value = JSON.parse(value)
    } catch {
      value = undefined
    }
  }

  const record = value && typeof value === 'object' && !Array.isArray(value) ? value : {}

  // Authoritative result fields win over fallible/abbreviated display hints.
  return { ...source.toolResultMetadata, ...record }
}

/** The event's own account of a failure, for calls whose result carries none. */
export function envelopeErrorText(metadata: ToolResultMetadata | undefined): string {
  const text = typeof metadata?.error === 'string' ? metadata.error : metadata?.message

  return text?.trim() ?? ''
}
