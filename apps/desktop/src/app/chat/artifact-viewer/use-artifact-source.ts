import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { knownOwnerForSession, requestForOwnedSession } from '@/store/session-states'
import { $gatewayState } from '@/store/session'
import {
  artifactViewerKey,
  cacheArtifactList,
  type ArtifactListSnapshot,
  type ArtifactViewerTarget,
  type LocalArtifactSource
} from '@/store/artifact-viewer'

const POLL_MS = 2_000
const PAGE_BYTES = 256 * 1024
const MAX_TEXT_BYTES = 4 * 1024 * 1024

interface TextWindow {
  mode: 'text'
  text: string
  offset: number
  length: number
  total_bytes: number
  eof: boolean
  bof: boolean
  redacted: boolean
  elided?: boolean
}

interface BytesWindow {
  mode: 'bytes'
  base64: string
  offset: number
  length: number
  total_bytes: number
  eof: boolean
}

type RpcWindow = TextWindow | BytesWindow

const rejectUnownedRequest = async <T>(): Promise<T> => {
  throw new Error('Artifact viewer owner unavailable')
}

function keepWithinLimit(pages: TextWindow[]) {
  let total = pages.reduce((sum, page) => sum + page.length, 0)
  let first = 0
  while (total > MAX_TEXT_BYTES && first < pages.length - 1) {
    total -= pages[first].length
    first++
  }
  return first ? pages.slice(first) : pages
}

export function useArtifactSource(target: ArtifactViewerTarget) {
  const gatewayState = useStore($gatewayState)
  const paneVisible = usePaneVisible()
  const [snapshot, setSnapshot] = useState<ArtifactListSnapshot | null>(null)
  const [source, setSource] = useState<LocalArtifactSource | null>(null)
  const [pages, setPages] = useState<TextWindow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [failures, setFailures] = useState(0)
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  const [variant, setVariant] = useState<LocalArtifactSource['variant']>('log')
  const variantRef = useRef(variant)
  const owner = JSON.stringify(knownOwnerForSession(target.sessionId))
  const key = artifactViewerKey(target)
  const targetRef = useRef(target)
  const keyRef = useRef(key)
  const ownerRef = useRef(owner)
  const sourceRef = useRef(source)
  const paneVisibleRef = useRef(paneVisible)
  const failureCountRef = useRef(0)

  targetRef.current = target
  keyRef.current = key
  ownerRef.current = owner
  sourceRef.current = source
  paneVisibleRef.current = paneVisible
  variantRef.current = variant

  const window = useMemo<TextWindow | null>(() => {
    if (!pages.length) return null
    const first = pages[0]
    const last = pages[pages.length - 1]
    return {
      mode: 'text',
      text: pages.map(page => page.text).join(''),
      offset: first.offset,
      length: pages.reduce((sum, page) => sum + page.length, 0),
      total_bytes: last.total_bytes,
      eof: last.eof,
      bof: first.bof,
      redacted: pages.some(page => page.redacted),
      elided: pages.some(page => page.elided)
    }
  }, [pages])

  useEffect(() => {
    failureCountRef.current = 0
    setFailures(0)
    setSnapshot(null)
    setSource(null)
    sourceRef.current = null
    setPages([])
    setError(null)
    setVariant('log')
  }, [key])

  useEffect(() => {
    let cancelled = false
    let pending = false
    const targetKey = key
    const isCurrent = () => !cancelled && keyRef.current === targetKey && ownerRef.current === owner
      && JSON.stringify(knownOwnerForSession(targetRef.current.sessionId)) === owner

    const refresh = async () => {
      const currentTarget = targetRef.current
      const list = await requestForOwnedSession<ArtifactListSnapshot>(
        currentTarget.sessionId,
        rejectUnownedRequest,
        'conductor_artifacts.list',
        { session_id: currentTarget.sessionId, ...(currentTarget.jobId ? { job_id: currentTarget.jobId } : {}), ...(currentTarget.runId ? { run_id: currentTarget.runId } : {}) }
      )
      if (!isCurrent()) return
      setSnapshot(list)
      cacheArtifactList(currentTarget, list)
      const picked = list.local?.find(row => row.variant === variantRef.current) ?? list.local?.[0] ?? null
      sourceRef.current = picked
      setSource(picked)
      if (!picked || picked.viewable === 'metadata') {
        setPages([])
        return
      }
      const tail = await requestForOwnedSession<RpcWindow>(
        currentTarget.sessionId,
        rejectUnownedRequest,
        'conductor_artifacts.read',
        { session_id: currentTarget.sessionId, source: 'local', job_id: picked.job_id, variant: picked.variant, mode: 'text', length: PAGE_BYTES, from_end: true }
      )
      if (!isCurrent() || tail.mode !== 'text') return
      setPages(current => keepWithinLimit(current.length ? [...current.slice(0, -1), tail] : [tail]))
    }

    const poll = async () => {
      if (!isCurrent() || pending || failureCountRef.current >= 3) return
      pending = true
      try {
        await refresh()
        if (isCurrent()) setError(null)
      } catch {
        if (isCurrent()) {
          failureCountRef.current++
          setFailures(failureCountRef.current)
          setError('read')
        }
      } finally {
        pending = false
      }
    }

    void poll()
    const timer = globalThis.window.setInterval(() => {
      if (paneVisibleRef.current && document.visibilityState === 'visible' && sourceRef.current?.status === 'running') void poll()
    }, POLL_MS)
    return () => {
      cancelled = true
      globalThis.window.clearInterval(timer)
    }
  }, [gatewayState, key, owner, variant])

  /** Switch between the worker's transcript (`log`) and agy's language-server `agy_log`. */
  const selectVariant = (next: LocalArtifactSource['variant']) => {
    if (next === variantRef.current) return
    variantRef.current = next
    sourceRef.current = null
    setPages([])
    setVariant(next)
  }

  const loadEarlier = async () => {
    const currentSource = sourceRef.current
    const first = pages[0]
    if (!currentSource || !first || first.bof || loadingEarlier) return
    setLoadingEarlier(true)
    const requestKey = key
    try {
      const start = Math.max(0, first.offset - PAGE_BYTES)
      const currentTarget = targetRef.current
      const page = await requestForOwnedSession<RpcWindow>(
        currentTarget.sessionId,
        rejectUnownedRequest,
        'conductor_artifacts.read',
        { session_id: currentTarget.sessionId, source: 'local', job_id: currentSource.job_id, variant: currentSource.variant, mode: 'text', offset: start, length: first.offset - start }
      )
      if (keyRef.current !== requestKey || page.mode !== 'text') return
      setPages(current => keepWithinLimit([page, ...current]))
    } catch {
      if (keyRef.current === requestKey) setError('earlier')
    } finally {
      setLoadingEarlier(false)
    }
  }

  const download = async () => {
    const currentSource = sourceRef.current
    if (!currentSource || currentSource.viewable !== 'text') return
    const requestKey = key
    const chunks: ArrayBuffer[] = []
    let offset = 0
    let total = 0
    let eof = false
    try {
      while (!eof && total <= 64 * 1024 * 1024) {
        const currentTarget = targetRef.current
        const chunk = await requestForOwnedSession<RpcWindow>(
          currentTarget.sessionId,
          rejectUnownedRequest,
          'conductor_artifacts.read',
          { session_id: currentTarget.sessionId, source: 'local', job_id: currentSource.job_id, variant: currentSource.variant, mode: 'bytes', offset, length: PAGE_BYTES }
        )
        if (chunk.mode !== 'bytes' || keyRef.current !== requestKey) break
        const bytes = Uint8Array.from(atob(chunk.base64), char => char.charCodeAt(0))
        const buffer = new ArrayBuffer(bytes.byteLength)
        new Uint8Array(buffer).set(bytes)
        chunks.push(buffer)
        offset = chunk.offset + chunk.length
        total += chunk.length
        eof = chunk.eof
      }
      if (!eof || total > 64 * 1024 * 1024 || keyRef.current !== requestKey) throw new Error('file exceeds download limit')
      const blob = new Blob(chunks, { type: 'text/plain' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${currentSource.job_id}${currentSource.variant === 'log' ? '.log' : '.agy.log'}`
      anchor.click()
      URL.revokeObjectURL(url)
    } catch {
      if (keyRef.current === requestKey) setError('download')
    }
  }

  return { snapshot, source, window, error, failures, loadEarlier, loadingEarlier, download, variant, selectVariant }
}
