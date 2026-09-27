import { isForwardCommandText } from './parse-to'

/**
 * #60 owner-forward (design §4.2, V-1..V-3): every non-typed submit path (widget `window.hermes.send`,
 * `data-hermes-send`, `::ask` options, plugin `composer.submit`, queue drains) lands in `submitText`,
 * never in `submitDraft`. `/to` is recognized only in `submitDraft`, so `submitText` refuses it.
 */
export function refuseForwardInSubmitText(text: string): boolean {
  return isForwardCommandText(text)
}
