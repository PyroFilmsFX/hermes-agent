import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { registry } from '../registry'

import { Slot } from './slot'
import { ContribSurfaceProvider, useContribSurface } from './surface'

const disposers: Array<() => void> = []

afterEach(() => {
  cleanup()
  disposers.splice(0).forEach(dispose => dispose())
})

function SessionReport() {
  const { profile, sessionId } = useContribSurface()

  return (
    <span>
      session:{sessionId ?? 'none'} profile:{profile ?? 'primary'}
    </span>
  )
}

describe('contribution surface', () => {
  it('tells a contribution which session and profile it is rendering for', () => {
    const area = 'test.surface.session'
    disposers.push(registry.register({ area, id: 'report', render: SessionReport, source: 'disk' }))

    render(
      <ContribSurfaceProvider profile="coder" sessionId="rt-42">
        <Slot area={area} />
      </ContribSurfaceProvider>
    )

    expect(screen.getByText('session:rt-42 profile:coder')).toBeTruthy()
  })

  it('yields nulls outside a session surface instead of throwing', () => {
    const area = 'test.surface.global'
    disposers.push(registry.register({ area, id: 'report', render: SessionReport, source: 'disk' }))

    render(<Slot area={area} />)

    expect(screen.getByText('session:none profile:primary')).toBeTruthy()
  })

  it('keeps sibling surfaces apart', () => {
    const area = 'test.surface.tiles'
    disposers.push(registry.register({ area, id: 'report', render: SessionReport, source: 'disk' }))

    render(
      <>
        <ContribSurfaceProvider sessionId="tile-a">
          <Slot area={area} />
        </ContribSurfaceProvider>
        <ContribSurfaceProvider profile="research" sessionId="tile-b">
          <Slot area={area} />
        </ContribSurfaceProvider>
      </>
    )

    expect(screen.getByText('session:tile-a profile:primary')).toBeTruthy()
    expect(screen.getByText('session:tile-b profile:research')).toBeTruthy()
  })
})
