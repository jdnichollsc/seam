import { useCallback, useEffect, useState } from 'react'

/** Where the user is: a tab, and at most one open sequence or take. The address bar holds it, so a reload,
 *  Back/Forward and a pasted link all land on the same place. The server serves the app at these paths. An open
 *  sequence or take does not name a tab, so the tab beside it rides in the history entry (a pasted link gets the
 *  usual one). */
export type Mode = 'pair' | 'seq' | 'gen'
export type Route = { mode: Mode; seqId: string | null; takeId: string | null }

const TABS: Record<Mode, string> = { pair: '/', seq: '/sequence', gen: '/generate' }

export const tab = (mode: Mode): Route => ({ mode, seqId: null, takeId: null })
/** Opening something keeps the tab it was opened from (`from`); Generate Shot has no room for it, so it leaves. */
const beside = (from: Mode | undefined, usual: Mode): Mode => (from && from !== 'gen' ? from : usual)
export const sequence = (id: string, from?: Mode): Route => ({ mode: beside(from, 'seq'), seqId: id, takeId: null })
export const take = (id: string, from?: Mode): Route => ({ mode: beside(from, 'pair'), seqId: null, takeId: id })
/** Another tab: what is open stays open beside it, except in Generate Shot. */
export const withTab = (r: Route, mode: Mode): Route => (mode === 'gen' ? tab(mode) : { ...r, mode })

export function parse(pathname: string, from?: unknown): Route {
  let parts: string[]
  try {
    parts = pathname.split('/').filter(Boolean).map(decodeURIComponent)
  } catch {
    return tab('pair')
  }
  const [head, id] = parts
  const kept = from === 'pair' || from === 'seq' ? from : undefined
  if (parts.length === 2 && head === 'sequences') return sequence(id, kept)
  if (parts.length === 2 && head === 'takes') return take(id, kept)
  if (parts.length === 1 && head === 'sequence') return tab('seq')
  if (parts.length === 1 && head === 'generate') return tab('gen')
  return tab('pair')
}

export const path = (r: Route) =>
  r.seqId ? `/sequences/${encodeURIComponent(r.seqId)}` : r.takeId ? `/takes/${encodeURIComponent(r.takeId)}` : TABS[r.mode]

const here = () => parse(window.location.pathname, (window.history.state as { mode?: unknown } | null)?.mode)

export function useRoute() {
  const [route, setRoute] = useState(here)
  useEffect(() => {
    const read = () => setRoute(here())
    window.addEventListener('popstate', read)
    // an address the app does not know shows the first tab: say so in the address bar too
    const known = path(parse(window.location.pathname))
    if (known !== window.location.pathname) window.history.replaceState(null, '', known)
    return () => window.removeEventListener('popstate', read)
  }, [])
  /** Go somewhere: Back returns from it. `replace` is for corrections (an id that is gone), which Back must skip. */
  const go = useCallback((next: Route, replace = false) => {
    const to = path(next)
    // another tab beside the same open item is a step too: Back returns to the tab it was opened from
    const moved = to !== window.location.pathname || here().mode !== next.mode
    if (moved) window.history[replace ? 'replaceState' : 'pushState']({ mode: next.mode }, '', to)
    setRoute(next)
  }, [])
  return { route, go }
}
