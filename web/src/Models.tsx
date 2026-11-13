import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { api, subscribe, type DownloadSet, type DownloadsState, type ModelAvailability } from './api'
import { Icon } from './Icon'

const FEATURES: [string, string][] = [
  ['transitions', 'Transitions'],
  ['shots', 'Generate shot'],
  ['orbit', '360° orbit'],
]
const STATE: Record<string, string> = {
  ready: 'Ready',
  missing: 'Not installed',
  needs_nodes: 'Needs nodes',
  not_offered: 'Not offered',
  not_checked: 'Not checked',
}
/** Opens the Models panel from anywhere a model can be picked. */
export const OpenModelsContext = createContext<() => void>(() => {})

/** Next to a model picker: where to see every model and get the missing ones. */
export function ModelsLink() {
  const open = useContext(OpenModelsContext)
  return <button type="button" className="link-btn models-link" onClick={open}>See all models · get the missing ones</button>
}

/** A disabled option says why, and where it would render instead. */
export const unavailableNote = (m: { available: boolean | null; reason: string; ready_elsewhere?: string | null }) =>
  m.available === false ? ` (${m.reason}${m.ready_elsewhere ? ` · ready on ${m.ready_elsewhere}` : ''})` : ''

const gb = (bytes: number) => `${(bytes / 1e9).toFixed(bytes >= 1e10 ? 0 : 1)} GB`

function Pill({ where, a }: { where: string; a: ModelAvailability }) {
  return (
    <span className={`model-pill is-${a.state}`} title={a.reason || undefined}>
      <span className="model-pill-where">{where}</span> {STATE[a.state] ?? a.state}
    </span>
  )
}

/** What a set enables, its files and licenses; one acceptance starts the download. Local only. */
function GetSet({ set, state, onChanged }: { set: DownloadSet; state: DownloadsState; onChanged: () => void }) {
  const [accept, setAccept] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = set.state?.status === 'running' ? set.state : null
  const act = async (f: () => Promise<unknown>) => {
    setError(null)
    try {
      await f()
      onChanged()
    } catch (e) {
      setError((e as Error).message)
    }
  }
  const missing = set.files.filter((f) => !f.present)
  const fits = state.free == null || state.free > set.remaining + 1e9  // a resumed set only needs what is left
  const blocker = set.blocker ?? state.blocker
  return (
    <div className="get-set">
      <ul className="get-files">
        {set.files.map((f) => (
          <li key={f.folder + f.name} className={f.present ? 'is-present' : ''}>
            <span>{f.folder}/{f.name}</span><span>{f.present ? 'installed' : gb(f.size)}</span>
          </li>
        ))}
      </ul>
      <p className="get-total">
        {missing.length} file{missing.length === 1 ? '' : 's'} · {gb(set.remaining)} to download
        {state.free != null && ` · ${gb(state.free)} free`}
        {state.models_dir && missing.length > 0 && <> · into <code>{state.models_dir}</code></>}
      </p>
      <ul className="get-licenses">
        {set.licenses.map((l) => (
          <li key={l.name}>
            <a href={l.url} target="_blank" rel="noreferrer">{l.name}</a>{l.note && ` · ${l.note}`}
          </li>
        ))}
      </ul>
      {set.gated && <p className="get-note">Gated on Hugging Face: accept the license there, then run <code>hf auth login</code> on this machine. Seam never asks for a token here.</p>}
      {blocker ? (
        <p className="get-note">{blocker}{set.blocker ? '' : <>: <code>{set.command}</code></>}</p>
      ) : run ? (
        <div className="shot-orbit" aria-live="polite">
          <span>{run.file ?? 'Starting'} · {gb(run.done)} of {gb(run.total)}</span>
          <span className="bar" style={{ ['--p' as string]: run.total ? run.done / run.total : 0 }} />
          <button className="btn btn-quiet" onClick={() => act(() => api.cancelDownload(set.id))}>Cancel</button>
        </div>
      ) : (
        <>
          <label className="get-accept">
            <input type="checkbox" checked={accept} onChange={(e) => setAccept(e.target.checked)} />
            <span>I have read and accept {set.licenses.length > 1 ? 'these licenses' : 'this license'}</span>
          </label>
          <button className="btn" disabled={!accept || !fits} onClick={() => act(() => api.startDownload(set.id))}>
            <Icon name="download" /> Download {gb(set.remaining)}
          </button>
          {!fits && <p className="error">Not enough disk space for this set.</p>}
        </>
      )}
      {set.state?.status === 'failed' && <p className="error">{set.state.error}</p>}
      {set.state?.status === 'cancelled' && <p className="get-note">Cancelled. Download again to resume where it stopped.</p>}
      {error && <p className="error">{error}</p>}
    </div>
  )
}

/** Every model per feature, ready or not on your GPU and on Comfy Cloud; missing local ones can be fetched here. */
export function ModelsDialog({ onClose, onReady }: { onClose: () => void; onReady: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [state, setState] = useState<DownloadsState | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const labels = useRef<Record<string, string>>({})
  const again = useRef(0)
  const shown = useRef(true)
  const load = useCallback(() => api.downloads().then((s) => {
    labels.current = Object.fromEntries(s.sets.map((x) => [x.id, x.label]))
    setState(s)
    window.clearTimeout(again.current)
    if (s.checking && shown.current) again.current = window.setTimeout(load, 4000)  // Comfy Cloud's list takes a while the first time
  }).catch((e) => setError((e as Error).message)), [])
  useEffect(() => {
    shown.current = true
    return () => {
      shown.current = false  // an answer that arrives after closing must not ask again
      window.clearTimeout(again.current)
    }
  }, [])
  useEffect(() => {
    dialog.current?.showModal()
    load()
    return subscribe({
      download: (d) => {
        setState((s) => s && { ...s, sets: s.sets.map((x) => (x.id === d.set ? { ...x, state: d } : x)) })
        if (d.status === 'done') {
          load()
          onReady()
          setToast(`${labels.current[d.set] ?? 'The models'}: ready`)
        }
      },
    })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const setOf = (id: string | null) => state?.sets.find((s) => s.id === id)
  return (
    <dialog ref={dialog} className="glb models-dialog" onClose={onClose} aria-label="Models">
      <header className="glb-head">
        <span className="chip sock-image">Models</span>
        <span className="glb-file">{state ? `Rendering on ${state.backend === 'cloud' ? 'Comfy Cloud' : 'your GPU'}` : 'Checking…'}</span>
        <button className="btn-icon btn-xs" onClick={onClose} aria-label="Close"><Icon name="close" /></button>
      </header>
      <div className="models-body">
        {error && <p className="error">{error}</p>}
        {toast && <p className="models-toast" role="status">{toast}</p>}
        {state && FEATURES.map(([feature, title]) => (
          <section key={feature} className="models-feature">
            <h3>{title}</h3>
            {feature === 'transitions' && <p className="get-note">Sequences use these same models.</p>}
            <ul>
              {state.models.filter((m) => m.feature === feature).map((m) => {
                const set = setOf(m.set)
                const canGet = m.local.state === 'missing' && set && set.missing > 0
                return (
                  <li key={m.id} className="models-row">
                    <div className="models-line">
                      <span className="models-name">{m.label}{m.kind === 'partner' && <small> · partner, uses credits</small>}</span>
                      <Pill where="Your GPU" a={m.local} />
                      <Pill where="Comfy Cloud" a={m.cloud} />
                      {canGet && (
                        <button className="btn btn-quiet" aria-expanded={open === m.id} onClick={() => setOpen(open === m.id ? null : m.id)}>
                          {set!.state?.status === 'running' ? 'Downloading…' : `Get · ${gb(set!.remaining)}`}
                        </button>
                      )}
                    </div>
                    {m.local.state === 'needs_nodes' && <p className="get-note">{m.local.reason}</p>}
                    {m.local.state === 'missing' && !set && <p className="get-note">Install it in ComfyUI yourself.</p>}
                    {open === m.id && set && <GetSet set={set} state={state} onChanged={load} />}
                  </li>
                )
              })}
            </ul>
          </section>
        ))}
      </div>
    </dialog>
  )
}
