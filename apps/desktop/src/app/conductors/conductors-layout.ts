// Width-responsive layout for the Conductors grid (#49 §8, R4).
//
// One markup, three layouts, switched by CONTAINER queries on the pane root
// (`@container` in conductors-pane.tsx) — never viewport media queries and
// never a JS resize loop. Every layout re-flows the SAME ten gridcells, so the
// grid semantics, the per-cell `data-col` hooks and the `≈` marks are
// identical at every width; only placement and a few secondary pieces change.
//
// Tailwind's `@max-[N]` means `width < N`, and narrower `@max-*` rules are
// emitted after wider ones, so a narrow override always beats a medium one.
//
//   wide   (≥ 1100 px)  ten-column table (CONDUCTORS_GRID_TRACK)
//   medium (700–1099)   eight columns: Remaining+Estimate stack into "Plan",
//                       Lanes+Seats stack into "Crew" (§8: 4+5 and 6+7)
//   narrow (< 700 px)   two-line card: dot · project/branch · session ·
//                       liveness, then W · unit · estimate · lanes · CI · blocker

export type ConductorColumn =
  'activity' | 'blockers' | 'ci' | 'estimate' | 'lanes' | 'now' | 'project' | 'remaining' | 'seats' | 'session'

/** Wide (≥ 1100 px) ten-column track, shared by the header row and every row. */
export const CONDUCTORS_GRID_TRACK =
  'grid grid-cols-[minmax(9rem,1.2fr)_minmax(8rem,1.1fr)_minmax(10rem,1.6fr)_6.75rem_7.25rem_9.5rem_3.25rem_5.5rem_minmax(7rem,1fr)_8.5rem] gap-x-3'

/** Medium (700–1099 px): project · session · now · plan · crew · CI · blockers · activity. */
export const CONDUCTORS_MEDIUM_TRACK =
  '@max-[1100px]:grid-cols-[minmax(5rem,1.2fr)_minmax(5rem,1.1fr)_minmax(5.5rem,1.4fr)_minmax(5.5rem,1fr)_minmax(4.5rem,0.9fr)_3.5rem_minmax(4rem,1fr)_5rem] @max-[1100px]:gap-x-2'

/** Narrow (< 700 px): the row stops being a grid and wraps into a two-line card. */
export const CONDUCTORS_NARROW_CARD =
  '@max-[700px]:flex @max-[700px]:flex-wrap @max-[700px]:items-center @max-[700px]:gap-x-2 @max-[700px]:gap-y-0.5'

/** Data rows and the header share one track so the columns line up. */
export const CONDUCTORS_ROW_LAYOUT = `${CONDUCTORS_GRID_TRACK} ${CONDUCTORS_MEDIUM_TRACK} ${CONDUCTORS_NARROW_CARD}`

/** Cards carry their own labels; the column header only exists as a table. */
export const CONDUCTORS_HEADER_LAYOUT = `${CONDUCTORS_GRID_TRACK} ${CONDUCTORS_MEDIUM_TRACK} @max-[700px]:hidden`

/** Skeletons keep a plain grid at every width (a card of bars reads as noise). */
export const CONDUCTORS_SKELETON_LAYOUT = `${CONDUCTORS_GRID_TRACK} ${CONDUCTORS_MEDIUM_TRACK} @max-[700px]:grid-cols-3`

/** The grid's own minimum: wide scrolls sideways under 68 rem, the others fit. */
export const CONDUCTORS_GRID_MIN_WIDTH = 'min-w-[68rem] @max-[1100px]:min-w-0'

/** Line-2 card separator: a CSS `·`, so it never enters a cell's text. */
const CARD_SEP =
  "@max-[700px]:before:mr-2 @max-[700px]:before:text-(--ui-text-quaternary) @max-[700px]:before:content-['·']"

const CARD_INLINE = '@max-[700px]:flex-row @max-[700px]:items-center @max-[700px]:justify-start'

/**
 * Per-cell placement. Medium pins every cell with `grid-area` (row / col /
 * row-end / col-end) on a two-row track: the merged columns stack their two
 * cells, everything else spans both rows. Narrow orders the flex items into
 * line 1 (project, session, activity), a forced break (order 4), then line 2.
 */
export const CONDUCTORS_CELL_LAYOUT: Record<ConductorColumn, string> = {
  project: `@max-[1100px]:[grid-area:1/1/3/2] @max-[700px]:order-1 @max-[700px]:max-w-[45%] @max-[700px]:gap-1.5 ${CARD_INLINE}`,
  session: `@max-[1100px]:[grid-area:1/2/3/3] @max-[700px]:order-2 @max-[700px]:flex-1 @max-[700px]:gap-1.5 ${CARD_INLINE}`,
  now: `@max-[1100px]:[grid-area:1/3/3/4] @max-[700px]:order-5 @max-[700px]:max-w-full @max-[700px]:gap-1.5 ${CARD_INLINE}`,
  remaining: '@max-[1100px]:[grid-area:1/4/2/5] @max-[700px]:hidden',
  estimate: `@max-[1100px]:[grid-area:2/4/3/5] @max-[1100px]:flex-row @max-[1100px]:items-center @max-[1100px]:justify-start @max-[1100px]:gap-1.5 @max-[700px]:order-6 ${CARD_INLINE} ${CARD_SEP}`,
  lanes: `@max-[1100px]:[grid-area:1/5/2/6] @max-[700px]:order-7 ${CARD_INLINE} ${CARD_SEP}`,
  seats: '@max-[1100px]:[grid-area:2/5/3/6] @max-[700px]:hidden',
  ci: `@max-[1100px]:[grid-area:1/6/3/7] @max-[700px]:order-8 ${CARD_INLINE} ${CARD_SEP}`,
  blockers: `@max-[1100px]:[grid-area:1/7/3/8] @max-[700px]:order-9 @max-[700px]:min-w-0 ${CARD_INLINE} ${CARD_SEP}`,
  activity: `@max-[1100px]:[grid-area:1/8/3/9] @max-[700px]:order-3 ${CARD_INLINE}`
}

/** Header cells: medium hides the two absorbed headers; the survivors relabel. */
export const CONDUCTORS_HEADER_CELL_LAYOUT: Partial<Record<ConductorColumn, string>> = {
  estimate: '@max-[1100px]:hidden',
  lanes: '@max-[1100px]:hidden'
}

/** Headers whose medium label differs (the merged columns). */
export const CONDUCTORS_MERGED_HEADER: Partial<Record<ConductorColumn, 'crew' | 'plan'>> = {
  remaining: 'plan',
  seats: 'crew'
}

/** Pieces that only belong to one layout. */
export const CONDUCTORS_PIECE = {
  /** Line break between card line 1 and line 2. */
  cardBreak: 'hidden @max-[700px]:order-4 @max-[700px]:block @max-[700px]:h-0 @max-[700px]:basis-full',
  /** Shown at medium and wide only (wave track, gates chip, the "4 m ago" text). */
  notNarrow: '@max-[700px]:hidden',
  /** Wide-only header label. */
  wideOnly: '@max-[1100px]:hidden',
  /** Medium-only header label (hidden when wide; the header itself hides narrow). */
  mediumOnly: 'hidden @max-[1100px]:inline',
  /** A seat with nothing to say drops out of the compact "Crew" summary. */
  quietSeat: '@max-[1100px]:hidden',
  /** "3/6" → "3/6 lanes" once lanes lose their own column header. */
  lanesSuffix: '@max-[1100px]:after:ml-1 @max-[1100px]:after:content-[attr(data-suffix)]',
  /** Activity stacks "4 m ago" over the chip in the narrower medium column. */
  activityStack: '@max-[1100px]:flex-col @max-[1100px]:items-start @max-[1100px]:gap-0.5',
  /** Branch sits inline after the project name on a card. */
  branchInline: '@max-[700px]:pl-0',
  /** Detail sections: three columns wide, two medium, one narrow. */
  detailGrid: 'grid-cols-3 @max-[1100px]:grid-cols-2 @max-[700px]:grid-cols-1'
} as const
