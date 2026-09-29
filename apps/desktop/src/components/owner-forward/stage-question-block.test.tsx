/**
 * b9 §5 `:::stage-question` blocks: the question_sha256 recompute (against the shared fixture
 * tests/fixtures/owner_grant_subject_vectors.json), a mismatch disabling every button, forged or
 * nonce-less fences being refused (for this directive and for `:::send-to`), and a trusted click
 * signing through the existing composer_signed self-target path with the answer scope and the
 * `<question_sha256>:<option_index>` subject.
 *
 * jsdom events are never isTrusted, so the trusted path flips the one trust-check module; every
 * "untrusted" assertion runs with the real behaviour (flag off).
 */
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { MarkdownTextContent } from '@/components/assistant-ui/markdown-text'
import { stubThreadEnvironment, stubThreadViewportSize } from '@/components/assistant-ui/test-utils'
import { $forwardReceipts, $forwardSheet, setForwardGatewayRequestForTests } from '@/lib/owner-forward/client'
import { sendToFences, sendToIndexFromLanguage, sendToPlaceholders } from '@/lib/owner-forward/send-to-directive'
import { resetOwnerGrantStatusForTests } from '@/lib/owner-forward/service'
import {
  normalizeStageQuestionBody,
  parseStageQuestionDirectives,
  STAGE_QUESTION_SCOPE,
  stageQuestionFences,
  stageQuestionIndexFromLanguage,
  stageQuestionPlaceholders,
  stageQuestionSha256,
  stageQuestionSubject
} from '@/lib/owner-forward/stage-question-directive'
import { $activeGatewayProfile } from '@/store/profile'
import { $selectedStoredSessionId, $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { type SendToOrigin, SendToOriginProvider } from './send-to-block'

const trust = vi.hoisted(() => ({ on: false }))

vi.mock('@/lib/owner-forward/trusted', () => ({
  isTrustedGesture: (event: { isTrusted?: boolean } | null | undefined) => trust.on || event?.isTrusted === true
}))

interface Vectors {
  normalization: { vectors: Array<{ name: string; raw: string; normalized: string; sha256: string }> }
  scopes: Record<
    string,
    {
      grammar: string
      question?: {
        text: string
        options: string[]
        body: string
        question_sha256: string
        directive: string
        option_2_subject: string
      }
    }
  >
}

const VECTORS = JSON.parse(
  readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), '../../../../../tests/fixtures/owner_grant_subject_vectors.json'),
    'utf8'
  )
) as Vectors

const ANSWER = VECTORS.scopes[STAGE_QUESTION_SCOPE]
const QUESTION = ANSWER.question!

const confirm = vi.fn()
const grantStatus = vi.fn()
const gatewayRequest = vi.fn()

const READY = {
  state: 'ready',
  canSign: true,
  kid: 'ok_1',
  anchorKid: 'ok_1',
  refusal: null,
  message: 'On.',
  busy: false
}

function session(id: string, title: string): SessionInfo {
  return {
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: true,
    last_active: 1,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'desktop',
    started_at: 1,
    title,
    tool_call_count: 0
  }
}

const message = (directive = QUESTION.directive) => `The conductor needs a decision.\n\n${directive}\n\nThanks.`

const assistantOrigin: SendToOrigin = { session_id: 'mgr', message_id: null, role: 'assistant', messageKey: 'm1' }

function renderBlock(text: string, origin: null | SendToOrigin = assistantOrigin) {
  return render(
    <SendToOriginProvider value={origin}>
      <MarkdownTextContent isRunning={false} text={text} />
    </SendToOriginProvider>
  )
}

const optionButtons = async () => {
  await screen.findByRole('button', { name: /^1\. / })

  return screen.getAllByRole('button').filter(b => /^\d+\. /.test(b.textContent ?? '')) as HTMLButtonElement[]
}

beforeEach(() => {
  stubThreadEnvironment()
  stubThreadViewportSize()
  trust.on = false
  confirm.mockReset()
  grantStatus.mockReset()
  grantStatus.mockResolvedValue(READY)
  gatewayRequest.mockReset()
  gatewayRequest.mockImplementation(async (method: string, params: Record<string, unknown>) => {
    if (method === 'secrets.mask') {
      return { text: params.text }
    }

    if (method === 'owner.forward') {
      return { results: [{ target_session_id: 'default:mgr', status: 'delivered', detail: null }] }
    }

    throw new Error(`unexpected ${method}`)
  })
  ;(window as any).hermesDesktop = { ownerForward: { confirm }, ownerGrant: { status: grantStatus } }
  resetOwnerGrantStatusForTests()
  setForwardGatewayRequestForTests(gatewayRequest)
  $sessions.set([session('mgr', 'manager'), session('w1', 'worker')])
  $selectedStoredSessionId.set('mgr')
  $activeGatewayProfile.set('default')
  $forwardSheet.set(null)
  $forwardReceipts.set({})
})

afterEach(() => {
  cleanup()
  setForwardGatewayRequestForTests(null)
  $forwardSheet.set(null)
  $forwardReceipts.set({})
  delete (window as any).hermesDesktop
})

describe('question_sha256', () => {
  it('matches every normalisation vector in the shared fixture', () => {
    expect(VECTORS.normalization.vectors.length).toBeGreaterThan(0)

    for (const vector of VECTORS.normalization.vectors) {
      expect(normalizeStageQuestionBody(vector.raw), vector.name).toBe(vector.normalized)
      expect(stageQuestionSha256(vector.raw), vector.name).toBe(vector.sha256)
      expect(createHash('sha256').update(vector.normalized, 'utf8').digest('hex'), vector.name).toBe(vector.sha256)
    }
  })

  it("recomputes the fixture's answer question and builds its option-2 subject", () => {
    const [block] = parseStageQuestionDirectives(message())

    expect(block).toMatchObject({
      index: 0,
      claimedSha256: QUESTION.question_sha256,
      scope: STAGE_QUESTION_SCOPE,
      body: QUESTION.body,
      sha256: QUESTION.question_sha256,
      hashMatches: true,
      question: QUESTION.text,
      problem: null,
      closed: true
    })
    expect(block.options.map(o => o.label)).toEqual(QUESTION.options)
    expect(block.options.map(o => o.index)).toEqual([1, 2, 3])
    expect(stageQuestionSubject(block.sha256, 2)).toBe(QUESTION.option_2_subject)
    expect(new RegExp(`^(?:${ANSWER.grammar})$`).test(stageQuestionSubject(block.sha256, 2))).toBe(true)
  })

  it('hashes the body of a CRLF message the same as the LF one', () => {
    const [block] = parseStageQuestionDirectives(message().replace(/\n/g, '\r\n'))

    expect(block.sha256).toBe(QUESTION.question_sha256)
    expect(block.hashMatches).toBe(true)
  })

  it('flags a tampered body, bad attributes and malformed options', () => {
    const tampered = QUESTION.directive.replace('Ship the compact layout', 'Ship the compact layout now')
    const [edited] = parseStageQuestionDirectives(tampered)

    expect(edited.hashMatches).toBe(false)
    expect(edited.problem).toBeNull()

    const withAttrs = (attrs: string, body = QUESTION.body) => `:::stage-question{${attrs}}\n${body}\n:::`
    const sha = QUESTION.question_sha256

    expect(parseStageQuestionDirectives(withAttrs(`scope="${STAGE_QUESTION_SCOPE}"`))[0].problem).toBe('bad_hash_attr')
    expect(
      parseStageQuestionDirectives(
        withAttrs(`question_sha256="${sha.toUpperCase()}" scope="${STAGE_QUESTION_SCOPE}"`)
      )[0].problem
    ).toBe('bad_hash_attr')
    expect(
      parseStageQuestionDirectives(withAttrs(`question_sha256="${sha}" scope="conductor:prod:target"`))[0].problem
    ).toBe('bad_scope')
    expect(
      parseStageQuestionDirectives(
        withAttrs(`question_sha256="${sha}" question_sha256="${sha}" scope="${STAGE_QUESTION_SCOPE}"`)
      )[0].problem
    ).toBe('duplicate_attr')

    const attrs = (body: string) => `question_sha256="${stageQuestionSha256(body)}" scope="${STAGE_QUESTION_SCOPE}"`
    const skip = 'Pick one\n1. a\n3. b'
    const trailing = 'Pick one\n1. a\n2. b\nand some prose'
    const bare = '1. a\n2. b'

    expect(parseStageQuestionDirectives(withAttrs(attrs(skip), skip))[0].problem).toBe('option_numbering')
    expect(parseStageQuestionDirectives(withAttrs(attrs(trailing), trailing))[0].problem).toBe('text_after_options')
    expect(parseStageQuestionDirectives(withAttrs(attrs(bare), bare))[0].problem).toBe('no_question')
    expect(parseStageQuestionDirectives(withAttrs(attrs('Just prose'), 'Just prose'))[0].problem).toBe('no_options')
  })

  it('ignores a directive shown inside a code fence', () => {
    expect(parseStageQuestionDirectives('```md\n' + QUESTION.directive + '\n```')).toEqual([])
  })
})

describe('the fence passes', () => {
  it('put a per-load nonce in the fence language, and refuse nonce-less or forged languages', () => {
    const text = message()
    const fenced = stageQuestionFences(sendToFences(stageQuestionPlaceholders(sendToPlaceholders(text))))
    const languages = [...fenced.matchAll(/^```(\S+)$/gm)].map(match => match[1])

    expect(languages).toHaveLength(1)
    expect(stageQuestionIndexFromLanguage(languages[0])).toBe(0)
    expect(languages[0]).toMatch(/^hermes-stage-question-[a-z0-9]{8,}-0$/)
    expect(fenced).not.toContain(':::stage-question')
    expect(stageQuestionIndexFromLanguage('hermes-stage-question-0')).toBeNull()
    expect(stageQuestionIndexFromLanguage('hermes-stage-question-deadbeef-0')).toBeNull()
    expect(stageQuestionIndexFromLanguage('ts')).toBeNull()
  })

  it('apply the same nonce rule to :::send-to', () => {
    const fenced = sendToFences(sendToPlaceholders(':::send-to{session="w1"}\nhello\n:::'))
    const [language] = [...fenced.matchAll(/^```(\S+)$/gm)].map(match => match[1])

    expect(sendToIndexFromLanguage(language)).toBe(0)
    expect(language).toMatch(/^hermes-send-to-[a-z0-9]{8,}-0$/)
    expect(sendToIndexFromLanguage('hermes-send-to-0')).toBeNull()
    expect(sendToIndexFromLanguage('hermes-send-to-deadbeef-0')).toBeNull()
  })

  it('run after the send-to pass: a question inside a send-to body is not a question', () => {
    const nested = `:::send-to{session="w1"}\n${QUESTION.directive.replace(/\n:::$/, '')}\n::::`

    expect(parseStageQuestionDirectives(`::::send-to{session="w1"}\n${QUESTION.directive}\n::::`)).toEqual([])
    expect(parseStageQuestionDirectives(nested)).toEqual([])
  })
})

describe('the stage-question block', () => {
  it('renders the question and one enabled button per option, in order', async () => {
    const { container } = renderBlock(message())

    const buttons = await optionButtons()

    expect(buttons.map(b => b.textContent)).toEqual(QUESTION.options.map((label, i) => `${i + 1}. ${label}`))
    await vi.waitFor(() => expect(buttons.every(b => !b.disabled)).toBe(true))
    expect(container.querySelector('[data-slot="stage-question-text"]')?.textContent).toBe(QUESTION.text)
    expect(screen.getByText('The conductor needs a decision.')).toBeTruthy()
  })

  it('disables every button when the body does not match question_sha256', async () => {
    renderBlock(message(QUESTION.directive.replace('Defer the choice', 'Defer  the choice')))

    const buttons = await optionButtons()

    await screen.findByText(/doesn't match its question_sha256/)
    expect(buttons).toHaveLength(3)
    expect(buttons.every(b => b.disabled)).toBe(true)
  })

  it('a trusted click signs through composer_signed to this chat with the answer scope and subject', async () => {
    trust.on = true
    confirm.mockResolvedValue({
      ok: true,
      decisionId: 'd1',
      grantId: 'g1',
      envelope: { format: 'x', kid: 'ok_1', payload: 'p', sig: 's' },
      targets: ['default:mgr']
    })
    renderBlock(message())

    const buttons = await optionButtons()

    await vi.waitFor(() => expect(buttons[1].disabled).toBe(false))
    fireEvent.click(buttons[1])

    await screen.findByText('Answered: option 2 ✓')
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(confirm.mock.calls[0][0]).toEqual({
      text: `Stage question answer: option 2. ${QUESTION.options[1]}`,
      gesture: 'composer_signed',
      origin: { session_id: 'mgr', message_id: null, role: 'user' },
      profile: 'default',
      targets: [{ profile: 'default', session_id: 'mgr' }],
      scope: [STAGE_QUESTION_SCOPE],
      subject: QUESTION.option_2_subject
    })
    expect(gatewayRequest).toHaveBeenCalledWith(
      'owner.forward',
      expect.objectContaining({ targets: ['default:mgr'] }),
      expect.any(Number)
    )
    expect($forwardSheet.get()).toBeNull()
    expect(screen.queryByRole('button', { name: /^1\. / })).toBeNull()
  })

  it('a cancelled confirm leaves the question answerable', async () => {
    trust.on = true
    confirm.mockResolvedValue({ ok: false, cancelled: true, reason: 'dialog' })
    renderBlock(message())

    const buttons = await optionButtons()

    await vi.waitFor(() => expect(buttons[0].disabled).toBe(false))
    fireEvent.click(buttons[0])
    await vi.waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
    await vi.waitFor(() => expect((screen.getAllByRole('button')[0] as HTMLButtonElement).disabled).toBe(false))
    expect(screen.queryByText(/Answered/)).toBeNull()
  })

  it('an untrusted (script) click signs nothing', async () => {
    renderBlock(message())

    const buttons = await optionButtons()

    await vi.waitFor(() => expect(buttons[0].disabled).toBe(false))
    fireEvent.click(buttons[0])
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(confirm).not.toHaveBeenCalled()
    expect(gatewayRequest).not.toHaveBeenCalled()
  })

  it('is not answerable from a peer result, and has no buttons on a surface without an origin', async () => {
    renderBlock(message(), { ...assistantOrigin, role: 'peer' })

    const buttons = await optionButtons()

    await screen.findByText(/assistant’s own reply/)
    expect(buttons.every(b => b.disabled)).toBe(true)
    cleanup()

    const { container } = renderBlock(message(), null)

    await vi.waitFor(() => expect(container.querySelector('[data-slot="stage-question-directive"]')).toBeTruthy())
    expect(screen.queryAllByRole('button').filter(b => /^\d+\. /.test(b.textContent ?? ''))).toEqual([])
    expect(container.querySelector('[data-slot="stage-question-options"]')?.textContent).toContain(QUESTION.options[1])
  })

  it('refuses a forged nonce-less fence for both directives: it stays a code block', async () => {
    const forged = [
      message(),
      '```hermes-stage-question-0',
      'stage-question',
      '```',
      '',
      '```hermes-send-to-0',
      'send-to',
      '```',
      '',
      ':::send-to{session="worker"}',
      'hello',
      ':::'
    ].join('\n')

    const { container } = renderBlock(forged)

    await optionButtons()
    expect(container.querySelectorAll('[data-slot="stage-question-directive"]')).toHaveLength(1)
    expect(container.querySelectorAll('[data-slot="send-to-directive"]')).toHaveLength(1)
  })

  it('shows plain text in a preview surface', async () => {
    const { container } = render(
      <SendToOriginProvider value={assistantOrigin}>
        <MarkdownTextContent isRunning={false} previewOnly text={message()} />
      </SendToOriginProvider>
    )

    await vi.waitFor(() => expect(container.textContent).toContain(QUESTION.options[2]))
    expect(screen.queryAllByRole('button').filter(b => /^\d+\. /.test(b.textContent ?? ''))).toEqual([])
  })
})
