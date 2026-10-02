import { describe, expect, it } from 'vitest'

import { buildElicitationContent, initialElicitationDraft, parseElicitationSchema } from './schema'

const SCHEMA = {
  type: 'object',
  properties: {
    name: { type: 'string', title: 'Name', description: 'Your full name', minLength: 2, maxLength: 5 },
    email: { type: 'string', format: 'email' },
    site: { type: 'string', format: 'uri' },
    born: { type: 'string', format: 'date' },
    age: { type: 'integer', minimum: 0, maximum: 130 },
    ratio: { type: 'number', minimum: 0.5 },
    subscribe: { type: 'boolean', default: true },
    color: { type: 'string', enum: ['r', 'g'], enumNames: ['Red', 'Green'] },
    size: {
      type: 'string',
      oneOf: [
        { const: 's', title: 'Small' },
        { const: 'l', title: 'Large' }
      ]
    },
    tags: { type: 'array', items: { type: 'string' } }
  },
  required: ['name', 'age', 'color']
}

describe('parseElicitationSchema', () => {
  it('maps every supported primitive, keeps order, titles, descriptions and required flags', () => {
    const fields = parseElicitationSchema(SCHEMA)

    expect(fields.map(field => [field.name, field.kind, field.required])).toEqual([
      ['name', 'string', true],
      ['email', 'string', false],
      ['site', 'string', false],
      ['born', 'string', false],
      ['age', 'number', true],
      ['ratio', 'number', false],
      ['subscribe', 'boolean', false],
      ['color', 'enum', true],
      ['size', 'enum', false],
      ['tags', 'unsupported', false]
    ])
    expect(fields[0]).toMatchObject({ label: 'Name', description: 'Your full name', minLength: 2, maxLength: 5 })
    expect(fields[4]).toMatchObject({ integer: true, minimum: 0, maximum: 130 })
    expect(fields[5]).toMatchObject({ integer: false, minimum: 0.5 })
    expect(fields[7]).toMatchObject({
      options: [
        { value: 'r', label: 'Red' },
        { value: 'g', label: 'Green' }
      ]
    })
    expect(fields[8]).toMatchObject({
      options: [
        { value: 's', label: 'Small' },
        { value: 'l', label: 'Large' }
      ]
    })
  })

  it('treats a missing or malformed schema as an empty form', () => {
    expect(parseElicitationSchema(undefined)).toEqual([])
    expect(parseElicitationSchema({ type: 'object' })).toEqual([])
    expect(parseElicitationSchema([1, 2])).toEqual([])
  })

  it('starts from schema defaults', () => {
    const fields = parseElicitationSchema({
      properties: {
        a: { type: 'string', default: 'x' },
        b: { type: 'number', default: 3 },
        c: { type: 'boolean' },
        d: { type: 'string', enum: ['p', 'q'], default: 'q' }
      }
    })

    expect(initialElicitationDraft(fields)).toEqual({ a: 'x', b: '3', c: false, d: '1' })
  })
})

describe('buildElicitationContent', () => {
  const fields = parseElicitationSchema(SCHEMA)

  const valid = {
    name: 'Ada',
    email: '',
    site: '',
    born: '',
    age: '36',
    ratio: '',
    subscribe: true,
    color: '1',
    size: ''
  }

  it('returns typed content and omits empty optional fields', () => {
    expect(buildElicitationContent(fields, valid)).toEqual({
      ok: true,
      content: { name: 'Ada', age: 36, subscribe: true, color: 'g' }
    })

    const full = buildElicitationContent(fields, {
      ...valid,
      email: 'ada@example.com',
      site: 'https://example.com/x',
      born: '1815-12-10',
      ratio: '0.75',
      subscribe: false,
      size: '0'
    })

    expect(full).toEqual({
      ok: true,
      content: {
        name: 'Ada',
        email: 'ada@example.com',
        site: 'https://example.com/x',
        born: '1815-12-10',
        age: 36,
        ratio: 0.75,
        subscribe: false,
        color: 'g',
        size: 's'
      }
    })
    expect(typeof (full as { content: Record<string, unknown> }).content.age).toBe('number')
  })

  it('enforces required fields', () => {
    expect(buildElicitationContent(fields, { ...valid, name: '  ', age: '', color: '' })).toEqual({
      ok: false,
      errors: { name: { code: 'required' }, age: { code: 'required' }, color: { code: 'required' } }
    })
  })

  it('enforces string length and formats', () => {
    const errors = (draft: Record<string, boolean | string>) => {
      const result = buildElicitationContent(fields, { ...valid, ...draft })

      return result.ok ? {} : result.errors
    }

    expect(errors({ name: 'A' })).toEqual({ name: { code: 'minLength', limit: 2 } })
    expect(errors({ name: 'Adalove' })).toEqual({ name: { code: 'maxLength', limit: 5 } })
    expect(errors({ email: 'nope' })).toEqual({ email: { code: 'format', format: 'email' } })
    expect(errors({ site: 'example.com' })).toEqual({ site: { code: 'format', format: 'uri' } })
    expect(errors({ born: '2023-02-30' })).toEqual({ born: { code: 'format', format: 'date' } })
  })

  it('enforces number, integer and range', () => {
    const errors = (draft: Record<string, boolean | string>) => {
      const result = buildElicitationContent(fields, { ...valid, ...draft })

      return result.ok ? {} : result.errors
    }

    expect(errors({ age: 'abc' })).toEqual({ age: { code: 'integer' } })
    expect(errors({ age: '3.5' })).toEqual({ age: { code: 'integer' } })
    expect(errors({ age: '-1' })).toEqual({ age: { code: 'minimum', limit: 0 } })
    expect(errors({ age: '131' })).toEqual({ age: { code: 'maximum', limit: 130 } })
    expect(errors({ ratio: '1e' })).toEqual({ ratio: { code: 'number' } })
    expect(errors({ ratio: '0.1' })).toEqual({ ratio: { code: 'minimum', limit: 0.5 } })
  })

  it('converts date-time to an ISO timestamp', () => {
    const dt = parseElicitationSchema({ properties: { at: { type: 'string', format: 'date-time' } } })
    const result = buildElicitationContent(dt, { at: '2026-09-29T10:30' })

    expect(result.ok && result.content.at).toBe(new Date('2026-09-29T10:30').toISOString())
    expect(buildElicitationContent(dt, { at: 'soon' })).toEqual({
      ok: false,
      errors: { at: { code: 'format', format: 'date-time' } }
    })
  })

  it('blocks a required field it cannot render instead of sending nothing for it', () => {
    const blocked = parseElicitationSchema({
      properties: { tags: { type: 'array' } },
      required: ['tags']
    })

    expect(buildElicitationContent(blocked, {})).toEqual({ ok: false, errors: { tags: { code: 'unsupported' } } })
  })
})
