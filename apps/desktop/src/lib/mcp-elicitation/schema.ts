/**
 * MCP elicitation (form mode): turn a server's `requestedSchema` into form
 * fields, and a user's draft back into typed `content`.
 *
 * The MCP spec restricts `requestedSchema` to a FLAT object whose properties
 * are primitives: string (minLength / maxLength / format email|uri|date|
 * date-time), number / integer (minimum / maximum), boolean, and single-select
 * enums (`enum` + legacy `enumNames`, or `oneOf`/`anyOf` of `{const, title}`).
 * Anything else is `unsupported`: an optional unsupported field is skipped, a
 * required one blocks Submit, so the user can only decline or cancel — the
 * form never sends content the server did not describe.
 *
 * Validation is a superset of the backend's (`validate_elicitation_content` in
 * tools/mcp_tool_sampling.py): required present and non-null, enum membership,
 * JS types that serialise to the Python types it checks (numbers as numbers,
 * integers as whole numbers, booleans as booleans). The length / range /
 * format constraints the backend does not check are enforced here, since the
 * server declared them.
 */

export type ElicitationStringFormat = 'date' | 'date-time' | 'email' | 'uri'

export type ElicitationPrimitive = boolean | number | string

export interface ElicitationEnumOption {
  label: string
  value: ElicitationPrimitive
}

interface FieldBase {
  name: string
  /** Display label: the schema `title`, else the property name. */
  label: string
  description?: string
  required: boolean
}

export type ElicitationField =
  | (FieldBase & {
      kind: 'string'
      default?: string
      format?: ElicitationStringFormat
      maxLength?: number
      minLength?: number
    })
  | (FieldBase & { kind: 'number'; default?: number; integer: boolean; maximum?: number; minimum?: number })
  | (FieldBase & { kind: 'boolean'; default?: boolean })
  | (FieldBase & { kind: 'enum'; default?: ElicitationPrimitive; options: ElicitationEnumOption[] })
  | (FieldBase & { kind: 'unsupported' })

/** Text / number inputs hold strings; enums hold the chosen option's index
 *  as a string ('' = none); checkboxes hold booleans. */
export type ElicitationDraft = Record<string, boolean | string>

export type ElicitationFieldError =
  | { code: 'required' }
  | { code: 'unsupported' }
  | { code: 'enum' }
  | { code: 'number' }
  | { code: 'integer' }
  | { code: 'minimum'; limit: number }
  | { code: 'maximum'; limit: number }
  | { code: 'minLength'; limit: number }
  | { code: 'maxLength'; limit: number }
  | { code: 'format'; format: ElicitationStringFormat }

export type ElicitationContentResult =
  | { ok: true; content: Record<string, ElicitationPrimitive> }
  | { ok: false; errors: Record<string, ElicitationFieldError> }

const FORMATS = new Set<string>(['date', 'date-time', 'email', 'uri'])

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)

const isPrimitive = (value: unknown): value is ElicitationPrimitive =>
  typeof value === 'string' || typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))

const finiteNumber = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) ? value : undefined

const nonNegativeInt = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : undefined

const text = (value: unknown): string | undefined =>
  typeof value === 'string' && value.trim() ? value.trim() : undefined

function enumOptions(spec: Record<string, unknown>): ElicitationEnumOption[] | null {
  if (Array.isArray(spec.enum)) {
    const names = Array.isArray(spec.enumNames) ? spec.enumNames : []

    const options = spec.enum.flatMap((value, index) =>
      isPrimitive(value) ? [{ value, label: text(names[index]) ?? String(value) }] : []
    )

    return options.length === spec.enum.length ? options : null
  }

  const variants = Array.isArray(spec.oneOf) ? spec.oneOf : Array.isArray(spec.anyOf) ? spec.anyOf : null

  if (!variants) {
    return null
  }

  const options = variants.flatMap(variant =>
    isRecord(variant) && isPrimitive(variant.const)
      ? [{ value: variant.const, label: text(variant.title) ?? String(variant.const) }]
      : []
  )

  return options.length > 0 && options.length === variants.length ? options : null
}

function parseField(name: string, raw: unknown, required: boolean): ElicitationField {
  const spec = isRecord(raw) ? raw : {}

  const base: FieldBase = {
    name,
    label: text(spec.title) ?? name,
    description: text(spec.description),
    required
  }

  const options = enumOptions(spec)

  if (options) {
    const fallback = spec.default

    return {
      ...base,
      kind: 'enum',
      options,
      default: options.some(option => option.value === fallback) ? (fallback as ElicitationPrimitive) : undefined
    }
  }

  switch (spec.type) {
    case 'string':
      return {
        ...base,
        kind: 'string',
        default: typeof spec.default === 'string' ? spec.default : undefined,
        format:
          typeof spec.format === 'string' && FORMATS.has(spec.format)
            ? (spec.format as ElicitationStringFormat)
            : undefined,
        maxLength: nonNegativeInt(spec.maxLength),
        minLength: nonNegativeInt(spec.minLength)
      }

    case 'number':

    case 'integer':
      return {
        ...base,
        kind: 'number',
        integer: spec.type === 'integer',
        default: finiteNumber(spec.default),
        maximum: finiteNumber(spec.maximum),
        minimum: finiteNumber(spec.minimum)
      }

    case 'boolean':
      return { ...base, kind: 'boolean', default: typeof spec.default === 'boolean' ? spec.default : undefined }

    default:
      return { ...base, kind: 'unsupported' }
  }
}

/** Parse `requestedSchema` into ordered fields. A missing or non-object
 *  schema is an empty form (Submit answers accept with `{}`). */
export function parseElicitationSchema(schema: unknown): ElicitationField[] {
  if (!isRecord(schema) || !isRecord(schema.properties)) {
    return []
  }

  const required = new Set(
    Array.isArray(schema.required) ? schema.required.filter((key): key is string => typeof key === 'string') : []
  )

  return Object.entries(schema.properties).map(([name, spec]) => parseField(name, spec, required.has(name)))
}

/** Datetime-local wants `YYYY-MM-DDTHH:mm` in local time. */
function toDateTimeLocal(value: string): string {
  const date = new Date(value)

  if (Number.isNaN(date.getTime())) {
    return ''
  }

  const pad = (n: number) => String(n).padStart(2, '0')

  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** The draft a fresh form starts from: schema defaults, else empty. */
export function initialElicitationDraft(fields: ElicitationField[]): ElicitationDraft {
  const draft: ElicitationDraft = {}

  for (const field of fields) {
    switch (field.kind) {
      case 'string':
        draft[field.name] =
          field.default === undefined
            ? ''
            : field.format === 'date-time'
              ? toDateTimeLocal(field.default)
              : field.default

        break

      case 'number':
        draft[field.name] = field.default === undefined ? '' : String(field.default)

        break

      case 'boolean':
        draft[field.name] = field.default ?? false

        break
      case 'enum': {
        const index = field.options.findIndex(option => option.value === field.default)
        draft[field.name] = index >= 0 ? String(index) : ''

        break
      }

      default:
        break
    }
  }

  return draft
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const NUMBER = /^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i

function validDate(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)

  if (!match) {
    return false
  }

  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])]
  const date = new Date(Date.UTC(year, month - 1, day))

  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day
}

function validUri(value: string): boolean {
  if (!/^[a-z][a-z0-9+.-]*:/i.test(value)) {
    return false
  }

  try {
    new URL(value)

    return true
  } catch {
    return false
  }
}

function stringValue(
  field: Extract<ElicitationField, { kind: 'string' }>,
  raw: string
): { error: ElicitationFieldError } | { value: string } {
  let value = raw

  if (field.format === 'date-time') {
    const date = new Date(raw)

    if (Number.isNaN(date.getTime())) {
      return { error: { code: 'format', format: 'date-time' } }
    }

    value = date.toISOString()
  } else if (field.format === 'email' && !EMAIL.test(raw)) {
    return { error: { code: 'format', format: 'email' } }
  } else if (field.format === 'uri' && !validUri(raw)) {
    return { error: { code: 'format', format: 'uri' } }
  } else if (field.format === 'date' && !validDate(raw)) {
    return { error: { code: 'format', format: 'date' } }
  }

  const length = [...raw].length

  if (field.minLength !== undefined && length < field.minLength) {
    return { error: { code: 'minLength', limit: field.minLength } }
  }

  if (field.maxLength !== undefined && length > field.maxLength) {
    return { error: { code: 'maxLength', limit: field.maxLength } }
  }

  return { value }
}

function numberValue(
  field: Extract<ElicitationField, { kind: 'number' }>,
  raw: string
): { error: ElicitationFieldError } | { value: number } {
  const trimmed = raw.trim()

  if (!NUMBER.test(trimmed)) {
    return { error: { code: field.integer ? 'integer' : 'number' } }
  }

  const value = Number(trimmed)

  if (!Number.isFinite(value)) {
    return { error: { code: 'number' } }
  }

  if (field.integer && !Number.isSafeInteger(value)) {
    return { error: { code: 'integer' } }
  }

  if (field.minimum !== undefined && value < field.minimum) {
    return { error: { code: 'minimum', limit: field.minimum } }
  }

  if (field.maximum !== undefined && value > field.maximum) {
    return { error: { code: 'maximum', limit: field.maximum } }
  }

  return { value }
}

/** Validate one field; `undefined` value with no error means "omit". */
export function elicitationFieldValue(
  field: ElicitationField,
  draft: ElicitationDraft
): { error: ElicitationFieldError } | { value: ElicitationPrimitive | undefined } {
  const raw = draft[field.name]

  switch (field.kind) {
    case 'unsupported':
      return field.required ? { error: { code: 'unsupported' } } : { value: undefined }

    case 'boolean':
      // A checkbox always has a definite state, so it is always sent.
      return { value: raw === true }
    case 'enum': {
      const index = typeof raw === 'string' && raw !== '' ? Number(raw) : -1
      const option = field.options[index]

      if (!option) {
        return field.required ? { error: { code: 'required' } } : { value: undefined }
      }

      return { value: option.value }
    }

    case 'number': {
      const value = typeof raw === 'string' ? raw : ''

      if (!value.trim()) {
        return field.required ? { error: { code: 'required' } } : { value: undefined }
      }

      return numberValue(field, value)
    }

    case 'string': {
      const value = typeof raw === 'string' ? raw : ''

      if (!value.trim()) {
        return field.required ? { error: { code: 'required' } } : { value: undefined }
      }

      return stringValue(field, value)
    }
  }
}

/** Validate every field and build typed `content`, or every field's error. */
export function buildElicitationContent(fields: ElicitationField[], draft: ElicitationDraft): ElicitationContentResult {
  const content: Record<string, ElicitationPrimitive> = {}
  const errors: Record<string, ElicitationFieldError> = {}

  for (const field of fields) {
    const result = elicitationFieldValue(field, draft)

    if ('error' in result) {
      errors[field.name] = result.error
    } else if (result.value !== undefined) {
      content[field.name] = result.value
    }
  }

  return Object.keys(errors).length > 0 ? { ok: false, errors } : { ok: true, content }
}
