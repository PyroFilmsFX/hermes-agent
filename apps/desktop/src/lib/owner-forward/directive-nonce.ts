/**
 * b9 §5: the per-load nonce shared by the transcript directive passes (`:::send-to`, `:::stage-question`).
 *
 * Each pass swaps a directive for an inert token line (before `preprocessMarkdown`) and then for a
 * fenced block whose LANGUAGE carries this nonce (after it). The renderer's code override claims a
 * fence only when its language carries the nonce, so a model that writes a fence such as
 * ```` ```hermes-send-to-0 ```` or ```` ```hermes-stage-question-0 ```` gets an ordinary code block,
 * never a live card. The nonce is drawn once per renderer load and never written into model-visible text.
 */

function drawNonce(): string {
  const bytes = new Uint8Array(8)
  const source = globalThis.crypto

  if (source && typeof source.getRandomValues === 'function') {
    source.getRandomValues(bytes)
  } else {
    for (let i = 0; i < bytes.length; i++) {
      bytes[i] = Math.floor(Math.random() * 256)
    }
  }

  // Lowercase alphanumerics only: the nonce sits in a token line and a fence info string.
  return Array.from(bytes, b => b.toString(36).padStart(2, '0')).join('')
}

export const DIRECTIVE_NONCE = drawNonce()
