import { createContext, Fragment, lazy, Suspense, type ReactNode, type RefObject, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import gsap from 'gsap'
import { useGSAP } from '@gsap/react'
import {
  api,
  comfyBundleUrl,
  receiptUrl,
  sequenceReceiptUrl,
  subscribe,
  uploadUrl,
  type BackendName,
  type BackendState,
  type Health,
  type ModelRow,
  type Job,
  type Preset,
  type Sequence,
  type EditIn,
  type ShotFields,
  type Example,
  type Upload,
} from './api'

gsap.registerPlugin(useGSAP)

// three.js loads only when someone drops a 3D model.
const GlbStudio = lazy(() => import('./GlbStudio'))
const isModel = (f: File) => /\.glb$/i.test(f.name) || f.type === 'model/gltf-binary'

const MODEL_LABEL: Record<string, string> = { l25: 'LTX-2.5', l23: 'LTX-2.3', h3: 'MiniMax H3' }
const ModelsContext = createContext<ModelRow[]>([])

/** Lengths are qualified per effect on its own model; any model picked instead renders at its own lengths. */
function allowedLengths(effect: { model?: string; seconds?: number[] } | undefined, pick: string, models: ModelRow[]): number[] {
  if (!pick || pick === effect?.model) return effect?.seconds ?? [5]
  return models.find((m) => m.id === pick)?.seconds ?? [5]
}

/** The model of one transition. Empty: the effect's own, reviewed model. Unavailable ones stay listed, disabled. */
function ModelSelect({ value, onChange, effectModel, disabled, compact, label = 'Model' }: {
  value: string
  onChange: (id: string) => void
  effectModel?: string
  disabled?: boolean
  compact?: boolean
  label?: string
}) {
  const models = useContext(ModelsContext)
  const own = models.find((m) => m.id === effectModel)?.label ?? MODEL_LABEL[effectModel ?? '']
  const row = (m: ModelRow) => (
    <option key={m.id} value={m.id} disabled={m.available === false}>
      {m.label}{m.available === false ? ` (${m.reason})` : ''}
    </option>
  )
  const others = models.filter((m) => m.id !== effectModel)
  const select = (
    <select aria-label={compact ? label : undefined} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
      <option value="">{own ? `${own} (default)` : 'Default'}</option>
      <optgroup label="Open models">{others.filter((m) => m.kind === 'open').map(row)}</optgroup>
      <optgroup label="Partner models · use credits">{others.filter((m) => m.kind === 'partner').map(row)}</optgroup>
    </select>
  )
  if (compact) return select
  const picked = value && value !== effectModel ? models.find((m) => m.id === value) : undefined
  return (
    <label className="next-effect model-pick">
      <span>{label}</span>
      {select}
      {picked && (
        <small className="add-next-note">
          Not reviewed with this effect.{picked.kind === 'partner' ? ' Uses credits on a paid provider.' : ''}
        </small>
      )}
    </label>
  )
}
const ACTIVE = new Set(['queued', 'running'])
/** Effect picker value for "pick one of the qualified effects for me" (resolved when the take is made). */
const SURPRISE = '__surprise__'
const MOTION = '(prefers-reduced-motion: no-preference)'
const REPO = 'https://github.com/jdnichollsc/seam'

const SEQ_MAX = 8
/** Two copies of one take (the live jobs feed, a sequence's REST snapshot): the later update wins. */
const newer = (x?: Job | null, y?: Job | null) => (!x ? (y ?? undefined) : !y || x.updated_at >= y.updated_at ? x : y)
const SEQ_ACTIVE = new Set(['running', 'assembling'])

type Rec = { id: string; created_at: number; updated_at: number }
/** REST snapshots, mutation responses and live events can arrive in any order: per record, the later update wins. */
function merge<T extends Rec>(prev: T[], incoming: T[]): T[] {
  const byId = new Map(prev.map((x) => [x.id, x]))
  for (const x of incoming) {
    const old = byId.get(x.id)
    if (!old || x.updated_at >= old.updated_at) byId.set(x.id, x)
  }
  return Array.from(byId.values()).sort((a, b) => b.created_at - a.created_at)
}

/** Jobs and sequences, kept live from one event stream; refetched when the stream reconnects. `hydrate` folds a
 *  sequence a request just returned (with its takes, which may be older than the jobs page) into the same state. */
function useLive() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [seqs, setSeqs] = useState<Sequence[]>([])
  const hydrate = useCallback((seq: Sequence) => {
    setSeqs((prev) => merge(prev, [seq]))
    setJobs((prev) => merge(prev, (seq.takes ?? []).filter((t): t is Job => !!t)))
  }, [])
  useEffect(() => {
    const load = () => {
      api.jobs().then((js) => setJobs((prev) => merge(prev, js))).catch(() => {})
      api
        .sequences()
        .then((qs) => {
          setSeqs((prev) => merge(prev, qs))
          // their takes may be older than the jobs page: fold them into jobs, the one place a take is read from
          setJobs((prev) => merge(prev, qs.flatMap((q) => q.takes ?? []).filter((t): t is Job => !!t)))
        })
        .catch(() => {})
    }
    return subscribe({
      job: (job) => setJobs((prev) => merge(prev, [job])),
      // live sequence events carry no takes: keep the ones REST gave (the jobs stream has the live ones)
      sequence: (seq) => setSeqs((prev) => merge(prev, [{ ...seq, takes: prev.find((x) => x.id === seq.id)?.takes }])),
      open: load,  // first connect and every reconnect: nothing sent while disconnected is lost
    })
  }, [])
  return { jobs, seqs, hydrate }
}

/** "Add the next shot", in two steps like an editor's Extend: choose the shot, then check where it leaves from, the
 *  effect into it and the new shot side by side before anything renders. Only the new transition renders. */
function NextShot({ label, fromUrl, busy, disabled, blocked, effects, defaultEffect, seconds, onMake }: {
  label: string
  fromUrl?: string
  busy: boolean
  disabled?: boolean
  blocked?: string  // why it cannot grow yet; shown instead of the hint
  effects: { id: string; name: string; model?: string }[]
  defaultEffect: string
  seconds: number
  onMake: (f: File, effect: string, direction: string, model: string) => Promise<boolean>
}) {
  const input = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [effect, setEffect] = useState(defaultEffect)
  const [direction, setDirection] = useState('')  // what happens during THIS transition (its own prompt)
  const [modelChoice, setModelChoice] = useState('')
  const effectModel = effects.find((x) => x.id === effect)?.model
  const preview = useMemo(() => (file ? URL.createObjectURL(file) : ''), [file])
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview) }, [preview])
  const pick = (f?: File | null) => {
    if (!f || disabled || busy) return
    setFile(f)
    setEffect((e) => (effects.some((x) => x.id === e) ? e : defaultEffect))
  }
  const choose = (
    <input
      ref={input}
      type="file"
      accept="image/png,image/jpeg,image/webp,video/mp4,video/quicktime,video/webm"
      className="visually-hidden"
      tabIndex={-1}
      aria-label={label}
      onChange={(e) => {
        pick(e.target.files?.[0])
        e.target.value = ''
      }}
    />
  )
  const drop = {
    onDragOver: (e: React.DragEvent) => {
      e.preventDefault()
      if (!disabled) setOver(true)
    },
    onDragLeave: () => setOver(false),
    onDrop: (e: React.DragEvent) => {
      e.preventDefault()
      setOver(false)
      pick(e.dataTransfer.files?.[0])
    },
  }
  if (!file) {
    return (
      <div className={`add-next${over ? ' is-over' : ''}${disabled ? ' is-off' : ''}`} {...drop}>
        <button className="btn btn-outline btn-sm" disabled={disabled || busy} onClick={() => input.current?.click()}>
          <Icon name="plus" /> {label}
        </button>
        <span className="add-next-note">
          {blocked ?? 'Choose or drop the next image or 1–6 s clip. You check the effect into it before anything renders.'}
        </span>
        {choose}
      </div>
    )
  }
  const isVideo = file.type.startsWith('video/')
  return (
    <div className={`add-next is-staged${over ? ' is-over' : ''}`} {...drop}>
      <div className="next-chain">
        <figure className="next-end">
          {fromUrl ? <img src={fromUrl} alt="" /> : <span className="next-blank" />}
          <figcaption>Leaves from</figcaption>
        </figure>
        <label className="next-effect">
          <span>Effect · {seconds} s</span>
          <select value={effect} onChange={(e) => setEffect(e.target.value)} disabled={busy}>
            <option value="" disabled>Pick an effect</option>
            {effects.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>
        </label>
        <figure className="next-end">
          {isVideo ? <video src={preview} muted playsInline /> : <img src={preview} alt="" />}
          <figcaption>New shot</figcaption>
        </figure>
      </div>
      <ModelSelect value={modelChoice} onChange={setModelChoice} effectModel={effectModel} disabled={busy} />
      <label className="next-direction">
        <span>What happens during this transition · optional</span>
        <textarea rows={2} maxLength={600} value={direction} disabled={busy}
          placeholder="She turns toward the camera and smiles, warm evening light"
          onChange={(e) => setDirection(e.target.value)} />
      </label>
      <div className="next-actions">
        <button className="btn btn-primary btn-sm" disabled={busy || !effect}
          onClick={async () => { if (await onMake(file, effect, direction.trim(), modelChoice)) { setFile(null); setDirection('') } }}>
          {busy ? 'Adding…' : 'Make this transition'}
        </button>
        <button className="btn btn-outline btn-sm" disabled={busy} onClick={() => input.current?.click()}>Change shot</button>
        <button className="btn btn-outline btn-sm" disabled={busy} onClick={() => setFile(null)}>Cancel</button>
        <span className="add-next-note">Only this transition renders; the takes before it are reused.</span>
      </div>
      {choose}
    </div>
  )
}

type FxOption = { id: string; name: string; seconds: number[]; model?: string }
type Remake = { preset: string; model: string; seconds: number; prompt_extra: string; seed?: number }

/** "New take" of one transition in a sequence: keep or change its effect, length and direction, pin a seed or not.
 *  Unchanged, it is a plain re-roll (a new seed). Only this transition renders. */
function TakeEditor({ take, effects, story, busy, onMake, onClose }: {
  take: Job
  effects: FxOption[]
  story: string  // the sequence's story now: the new take uses it
  busy: boolean
  onMake: (body: Remake) => Promise<boolean>
  onClose: () => void
}) {
  const p = take.params
  const [fx, setFx] = useState(effects.some((e) => e.id === p.preset) ? p.preset : (effects[0]?.id ?? ''))
  const models = useContext(ModelsContext)
  const effect = effects.find((e) => e.id === fx)
  // a model the user picked stays picked; the effect's own model reads as the default
  const [pick, setPick] = useState(p.model && p.model !== effects.find((e) => e.id === p.preset)?.model ? p.model : '')
  const lens = allowedLengths(effect, pick, models)
  const [secs, setSecs] = useState(p.seconds ?? 5)
  const length = lens.includes(secs) ? secs : lens[0]
  const [extra, setExtra] = useState(p.prompt_extra ?? '')
  const [seed, setSeed] = useState('')
  const changed = fx !== p.preset || (pick || effect?.model) !== p.model || length !== (p.seconds ?? 5) || extra.trim() !== (p.prompt_extra ?? '').trim()
  const newStory = (p.story ?? '') !== story
  return (
    <div className="take-edit">
      <label className="next-effect">
        <span>Effect</span>
        <select value={fx} disabled={busy} onChange={(e) => setFx(e.target.value)}>
          {effects.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
        </select>
      </label>
      <ModelSelect value={pick} onChange={setPick} effectModel={effect?.model} disabled={busy} />
      <div className="take-edit-len">
        <span>Length</span>
        <div className="seg seg-sm" role="radiogroup" aria-label="Take length">
          {lens.map((s) => (
            <button key={s} role="radio" aria-checked={length === s} aria-selected={length === s} disabled={busy}
              onClick={() => setSecs(s)}>{s} s</button>
          ))}
        </div>
      </div>
      <label className="take-edit-wide">
        <span>What happens during this change</span>
        <textarea rows={2} maxLength={600} value={extra} disabled={busy} placeholder="She turns toward the camera and smiles"
          onChange={(e) => setExtra(e.target.value)} />
      </label>
      <label className="take-edit-seed">
        <span>Seed</span>
        <input value={seed} inputMode="numeric" placeholder="New random" disabled={busy}
          onChange={(e) => setSeed(e.target.value.replace(/\D/g, ''))} />
      </label>
      {p.prompt_override && (
        <p className="take-edit-note">This take was made from a hand-written prompt; the new one uses the effect and direction above.</p>
      )}
      <div className="next-actions">
        <button className="btn btn-primary btn-sm" disabled={busy || !fx}
          onClick={async () => {
            if (await onMake({ preset: fx, model: pick, seconds: length, prompt_extra: extra.trim(), seed: seed ? Number(seed) : undefined })) onClose()
          }}>
          {busy ? 'Starting…' : changed ? 'Make this take' : 'New take, same direction'}
        </button>
        <button className="btn btn-outline btn-sm" disabled={busy} onClick={onClose}>Cancel</button>
        <span className="add-next-note">
          {newStory ? 'It uses the sequence’s current story. ' : ''}Only this transition renders; the others are kept.
        </span>
      </div>
    </div>
  )
}

type Board = { at: number; order: number[]; story: string; fx: Record<string, { preset: string; extra: string; model?: string }> }

/** The sequence as a storyboard: its story, and its shots in order (move, remove). Changes stage here; Apply renders
 *  only the hand-offs whose two shots were not already next to each other, and joins the video again. */
function Storyboard({ seq, takes, effects, busy, onApply }: {
  seq: Sequence
  takes: (Job | null | undefined)[]
  effects: FxOption[]
  busy: boolean
  onApply: (body: EditIn) => Promise<boolean>
}) {
  const [draft, setDraft] = useState<Board | null>(null)  // null: nothing staged, the strip shows the sequence
  const models = useContext(ModelsContext)
  const [drag, setDrag] = useState<number | null>(null)
  const settled = !SEQ_ACTIVE.has(seq.status)
  const story = seq.story ?? ''
  const b: Board = draft ?? { at: seq.updated_at, order: seq.shots.map((_, i) => i), story, fx: {} }
  const kept = new Map(seq.segments.map((_, i) => [`${i}-${i + 1}`, i]))
  const edit = (change: (d: Board) => Board) => setDraft(change(b))
  const move = (from: number, to: number) => edit((d) => {
    if (to < 0 || to >= d.order.length || from === to) return d
    const order = [...d.order]
    const [x] = order.splice(from, 1)
    order.splice(to, 0, x)
    return { ...d, order }
  })
  const fallback = (k: number) => {  // a new hand-off starts with the effect of the take that sat there (or the last)
    const t = takes[Math.min(k, takes.length - 1)]
    const e = effects.find((x) => x.id === t?.params.preset) ?? effects[0]
    return { preset: e?.id ?? '', extra: '' }
  }
  const links = b.order.slice(0, -1).map((a, k) => {
    const key = `${a}-${b.order[k + 1]}`
    const keep = kept.get(key)
    return { k, key, keep, take: keep === undefined ? undefined : takes[keep], spec: b.fx[key] ?? fallback(k) }
  })
  const fresh = links.filter((l) => l.keep === undefined)
  const reordered = b.order.join() !== seq.shots.map((_, i) => i).join()
  const storyChanged = b.story.trim() !== story
  const dirty = reordered || storyChanged
  const stale = !!draft && draft.at !== seq.updated_at
  const lenOf = (id: string, pick = '') => allowedLengths(effects.find((e) => e.id === id), pick, models)[0]
  const apply = async () => {
    const ok = await onApply({
      order: b.order.map((i) => i + 1),
      transitions: Object.fromEntries(fresh.map((l) => [String(l.k + 1),
        { preset: l.spec.preset, model: l.spec.model || undefined, seconds: lenOf(l.spec.preset, l.spec.model), prompt_extra: l.spec.extra.trim() }])),
      story: b.story.trim(),
      expected_updated_at: b.at,
    })
    if (ok) setDraft(null)
  }
  const summary = !reordered
    ? 'Saves the story; nothing renders. Transitions you make from now on use it.'
    : fresh.length
      ? `${fresh.length} new transition${fresh.length > 1 ? 's' : ''} will render, ${links.length - fresh.length} kept.`
      : 'Nothing renders: every transition is kept and the video joins again.'
  return (
    <section className="board" aria-label="Storyboard">
      <label className="board-story">
        <span>The whole sequence</span>
        <textarea rows={1} maxLength={300} value={b.story} disabled={!settled || busy}
          placeholder="One idea for the whole film: who, where, the mood. Every new transition uses it."
          onChange={(e) => edit((d) => ({ ...d, story: e.target.value }))} />
      </label>
      <ol className="board-strip" aria-label="Shots in order">
        {b.order.map((i, pos) => {
          const shot = seq.shots[i]
          const link = links[pos]
          return (
            <li key={i} className="board-item">
              <figure
                className={`board-shot${drag === pos ? ' is-drag' : ''}`}
                draggable={settled && !busy}
                onDragStart={() => setDrag(pos)}
                onDragEnd={() => setDrag(null)}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => { e.preventDefault(); if (drag !== null) move(drag, pos); setDrag(null) }}
              >
                <img src={shot.first_url} alt="" />
                <figcaption>
                  <span>Shot {pos + 1}{i !== pos ? <em> · was {i + 1}</em> : null}{shot.clip ? <em> · clip</em> : null}</span>
                  <span className="board-tools">
                    <button className="btn-icon btn-xs" disabled={!settled || busy || pos === 0} onClick={() => move(pos, pos - 1)}
                      title="Earlier" aria-label={`Move shot ${pos + 1} earlier`}><Icon name="left" /></button>
                    <button className="btn-icon btn-xs" disabled={!settled || busy || pos === b.order.length - 1} onClick={() => move(pos, pos + 1)}
                      title="Later" aria-label={`Move shot ${pos + 1} later`}><Icon name="right" /></button>
                    <button className="btn-icon btn-xs" disabled={!settled || busy || b.order.length <= 2}
                      onClick={() => edit((d) => ({ ...d, order: d.order.filter((x) => x !== i) }))}
                      title={b.order.length <= 2 ? 'A sequence keeps at least two shots' : 'Remove this shot; its neighbours join up'}
                      aria-label={`Remove shot ${pos + 1}`}><Icon name="close" /></button>
                  </span>
                </figcaption>
              </figure>
              {link && (
                link.keep !== undefined ? (
                  <span className="board-link" title="Kept: this transition is reused">
                    <span className="board-link-name">{effects.find((e) => e.id === link.take?.params.preset)?.name ?? 'Kept'}</span>
                    <span className="board-link-state">kept</span>
                  </span>
                ) : (
                  <span className="board-link is-new">
                    <select aria-label={`Effect into shot ${pos + 2}`} value={link.spec.preset} disabled={busy}
                      onChange={(e) => edit((d) => ({ ...d, fx: { ...d.fx, [link.key]: { ...link.spec, preset: e.target.value } } }))}>
                      {effects.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
                    </select>
                    <ModelSelect compact label={`Model into shot ${pos + 2}`} value={link.spec.model ?? ''} disabled={busy}
                      effectModel={effects.find((e) => e.id === link.spec.preset)?.model}
                      onChange={(id) => edit((d) => ({ ...d, fx: { ...d.fx, [link.key]: { ...link.spec, model: id } } }))} />
                    <input aria-label={`What happens from shot ${pos + 1} to shot ${pos + 2}`} placeholder="Direction (optional)"
                      maxLength={600} value={link.spec.extra} disabled={busy}
                      onChange={(e) => edit((d) => ({ ...d, fx: { ...d.fx, [link.key]: { ...link.spec, extra: e.target.value } } }))} />
                    <span className="board-link-state">new · renders</span>
                  </span>
                )
              )}
            </li>
          )
        })}
      </ol>
      {!settled && !draft && <p className="add-next-note">Edit the shots once this sequence has finished.</p>}
      {dirty && (
        <div className="board-bar" role="status">
          <span>{stale ? 'This sequence changed since you started. Discard to start again from it.' : summary}</span>
          <button className="btn btn-primary btn-sm" disabled={busy || stale || !settled} onClick={apply}>
            {busy ? 'Applying…' : 'Apply'}
          </button>
          <button className="btn btn-outline btn-sm" disabled={busy} onClick={() => setDraft(null)}>Discard</button>
        </div>
      )}
    </section>
  )
}

/* Icons: one stroke family (24 grid, 1.8 stroke), drawn here so nothing depends on an icon font. */
const ICONS: Record<string, string> = {
  download: 'M12 4v11m0 0-5-5m5 5 5-5M5 20h14',
  graph: 'M4 6h6v5H4zM14 13h6v5h-6zM10 8.5h2.5a2 2 0 0 1 2 2V13',
  refresh: 'M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6',
  close: 'M6 6l12 12M18 6 6 18',
  receipt: 'M7 3h10v18l-2.5-1.6L12 21l-2.5-1.6L7 21zM10 8h4M10 12h4',
  upload: 'M12 16V5m0 0-4.5 4.5M12 5l4.5 4.5M5 19h14',
  github: 'M9 19c-4 1.3-4-2-6-2.5m12 4.5v-3.4a3 3 0 0 0-.8-2.3c2.7-.3 5.6-1.3 5.6-6a4.6 4.6 0 0 0-1.3-3.2 4.3 4.3 0 0 0-.1-3.2s-1-.3-3.4 1.3a11.7 11.7 0 0 0-6 0C6.6 2.6 5.6 2.9 5.6 2.9a4.3 4.3 0 0 0-.1 3.2A4.6 4.6 0 0 0 4.2 9.3c0 4.6 2.9 5.7 5.6 6a3 3 0 0 0-.8 2.3V21',
  play: 'M8 5.5v13l11-6.5z',
  pause: 'M8 5v14M16 5v14',
  sound: 'M4 9.5h3.5L12 5v14l-4.5-4.5H4zM16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11',
  muted: 'M4 9.5h3.5L12 5v14l-4.5-4.5H4zM16.5 9.5l5 5M21.5 9.5l-5 5',
  expand: 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5',
  up: 'M12 19V5m0 0-6 6m6-6 6 6',
  left: 'M19 12H5m0 0 6-6m-6 6 6 6',
  right: 'M5 12h14m0 0-6-6m6 6-6 6',
  down: 'M12 5v14m0 0-6-6m6 6 6-6',
  plus: 'M12 5v14M5 12h14',
  dice: 'M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zM8.5 8.5h.01M15.5 8.5h.01M12 12h.01M8.5 15.5h.01M15.5 15.5h.01',
}

function Icon({ name }: { name: keyof typeof ICONS }) {
  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d={ICONS[name]} />
    </svg>
  )
}

/** Where takes render: the status chip opens the choice. The key is write-only: it goes to the local server, never back. */
function BackendControl({ health, healthError, onChange }: {
  health: Health | null
  healthError: string | null
  onChange: (backend: BackendName) => void
}) {
  const [open, setOpen] = useState(false)
  const [state, setState] = useState<BackendState | null>(null)
  const [settingUpCloud, setSettingUpCloud] = useState(false)  // Comfy Cloud picked, no key yet: nothing switched so far
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  const run = async (action: () => Promise<BackendState>) => {
    setBusy(true)
    setProblem(null)
    try {
      const next = await action()
      setState(next)
      setKey('')
      setSettingUpCloud(false)
      onChange(next.backend)
    } catch (e) {
      setProblem((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const toggle = () => {
    if (!open) {
      setSettingUpCloud(false)
      setProblem(null)
      api.backend().then(setState).catch((e) => setProblem(String(e.message ?? e)))
    }
    setOpen(!open)
  }
  const locked = state && (state.source === 'SEAM_BACKEND' || state.source === 'COMFY_BASE_URL')
  const showCloud = state?.backend === 'cloud' || settingUpCloud
  const pickCloud = () => {
    if (state?.cloud.key_configured) void run(() => api.setBackend('cloud'))
    else setSettingUpCloud(true)
  }
  const saveKey = () =>
    run(async () => {
      const saved = await api.setCloudKey(key)
      return saved.backend === 'cloud' || locked ? saved : api.setBackend('cloud')  // they picked Comfy Cloud to get here
    })
  const label = health
    ? health.backend === 'cloud' ? 'Comfy Cloud' : 'Your GPU'
    : healthError ? 'Engine offline · run seam serve' : 'Connecting…'

  return (
    <div className="backend">
      <button type="button" className={`engine ${health ? 'is-up' : healthError ? 'is-down' : ''}`} aria-expanded={open}
        disabled={!health} onClick={toggle}>
        {label}
      </button>
      {open && state && (
        <div className="backend-panel" role="group" aria-label="Where takes render">
          <p className="backend-title">Where takes render</p>
          <div className="backend-choice">
            <button type="button" className={showCloud ? '' : 'is-on'} disabled={busy || !!locked}
              onClick={() => (state.backend === 'local' ? setSettingUpCloud(false) : void run(() => api.setBackend('local')))}>Your GPU</button>
            <button type="button" className={showCloud ? 'is-on' : ''} disabled={busy || !!locked} onClick={pickCloud}>Comfy Cloud</button>
          </div>
          {locked && <p className="backend-note">{state.source} decides this.</p>}
          {!showCloud && !locked && <p className="backend-note">Renders on this machine with your ComfyUI. No credits.</p>}
          {showCloud && (
            <>
              <p className="backend-note">Your shots are uploaded to Comfy Cloud and takes use your credits. Takes already started stay where they are.</p>
              <form onSubmit={(e) => { e.preventDefault(); void saveKey() }}>
                <label htmlFor="cloud-key">Comfy Cloud API key</label>
                <div className="backend-key">
                  <input id="cloud-key" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)}
                    placeholder={state.cloud.key_configured ? 'Saved · paste to replace' : 'Paste your key'} />
                  <button type="submit" className="btn btn-outline" disabled={busy || !key.trim()}>{busy ? 'Checking…' : 'Save'}</button>
                </div>
              </form>
              <p className="backend-note">
                <a href={state.keys_url} target="_blank" rel="noreferrer">Create a key</a>
                {state.cloud.key_source === 'saved' && (
                  <> · <button type="button" className="backend-link" disabled={busy} onClick={() => run(api.clearCloudKey)}>Forget the saved key</button></>
                )}
              </p>
            </>
          )}
          {problem && <p className="backend-problem" role="alert">{problem}</p>}
        </div>
      )}
    </div>
  )
}

/** A ComfyUI-style node: title chip with its socket colour, optional meta on the right. */
function Node({ title, socket, meta, className = '', children, label, wire }: {
  wire?: string
  title: string
  socket: 'image' | 'effect' | 'video' | 'text'
  meta?: ReactNode
  className?: string
  children: ReactNode
  label?: string
}) {
  return (
    <section className={`node ${className}`} aria-label={label ?? title}>
      <header className="node-head" data-wire={wire}>
        <span className={`chip sock-${socket}`}>{title}</span>
        {meta && <span className="node-meta">{meta}</span>}
      </header>
      {children}
    </section>
  )
}

type Pt = { x: number; y: number }

/**
 * The graph's wires, measured from the live layout: links down the rail from node to node (Shots → Effect →
 * Direction → Make) and one across into OUTPUT. A fixed overlay, so they follow scroll and resize.
 */
function Wires({ root, running }: { root: RefObject<HTMLDivElement | null>; running: boolean }) {
  const svg = useRef<SVGSVGElement>(null)
  useEffect(() => {
    const el = root.current
    const g = svg.current
    if (!el || !g) return
    let frame = 0
    const draw = () => {
      frame = 0
      const pt = (name: string, side: 'left' | 'right'): Pt | null => {
        const n = el.querySelector(`[data-wire="${name}"]`)
        if (!n) return null
        const r = n.getBoundingClientRect()
        return { x: side === 'left' ? r.left : r.right, y: r.top + r.height / 2 }
      }
      const stacked = window.matchMedia('(max-width: 960px)').matches
      const rail = el.querySelector('.rail')?.getBoundingClientRect()
      const clip = g.querySelector('#rail-clip rect')
      if (rail && clip) {
        // Rail links stay inside the rail (never over the header).
        clip.setAttribute('x', String(rail.left - 30))
        clip.setAttribute('y', String(stacked ? -1e4 : rail.top))
        clip.setAttribute('width', String(rail.width + 30))
        clip.setAttribute('height', String(stacked ? 1e5 : rail.height))
      }
      // A node scrolled out of the rail sends its wire in from the rail's edge, with no socket drawn there.
      const view = el.querySelector('.rail-scroll')?.getBoundingClientRect()
      const hidden = new Set<number>()
      const chain = [pt('shots', 'left'), pt('effect', 'left'), pt('direction', 'left'), pt('make', 'left')].map((p, i) => {
        if (!p || !view || stacked || i === 3) return p
        const y = Math.min(Math.max(p.y, view.top), view.bottom)
        if (y !== p.y) hidden.add(i)
        return { x: p.x, y }
      })
      const loop = (a: Pt, b: Pt) => {
        const out = Math.min(14, a.x - 2)
        return `M${a.x} ${a.y}C${a.x - out} ${a.y + 30} ${b.x - out} ${b.y - 30} ${b.x} ${b.y}`
      }
      const links: (string | null)[] = []
      for (let i = 0; i < chain.length - 1; i++) {
        const a = chain[i]
        const b = chain[i + 1]
        links.push(a && b ? loop(a, b) : null)
      }
      const m = pt('make', 'right')
      const o = pt('output', 'left')
      const across = m && o && !stacked
      if (across) {
        const dx = Math.max(36, (o.x - m.x) / 2)
        links.push(`M${m.x} ${m.y}C${m.x + dx} ${m.y} ${o.x - dx} ${o.y} ${o.x} ${o.y}`)
      } else links.push(null)
      g.querySelectorAll<SVGGElement>('g.link').forEach((link, i) => {
        const d = links[i]
        link.style.display = d ? '' : 'none'
        if (d) link.querySelectorAll('path').forEach((path) => path.setAttribute('d', d))
      })
      const ends = [...chain, across ? m : null, across ? o : null]
      g.querySelectorAll<SVGCircleElement>('circle.sock').forEach((c, i) => {
        const e = ends[i]
        c.style.display = e && !hidden.has(i) ? '' : 'none'
        if (e) {
          c.setAttribute('cx', String(e.x))
          c.setAttribute('cy', String(e.y))
        }
      })
    }
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(draw)
    }
    draw()
    const ro = new ResizeObserver(schedule)
    el.querySelectorAll('.node, .make, .rail-scroll').forEach((n) => ro.observe(n))
    const rail = el.querySelector('.rail-scroll')
    window.addEventListener('scroll', schedule, { passive: true })
    window.addEventListener('resize', schedule)
    rail?.addEventListener('scroll', schedule, { passive: true })
    const mo = new MutationObserver(schedule)  // layout shifts that resize nothing (e.g. a node's content swapping)
    mo.observe(el, { childList: true, subtree: true })
    return () => {
      cancelAnimationFrame(frame)
      ro.disconnect()
      mo.disconnect()
      window.removeEventListener('scroll', schedule)
      window.removeEventListener('resize', schedule)
      rail?.removeEventListener('scroll', schedule)
    }
  }, [root])

  // The one authored loop: while a take renders, pulses run along every wire toward OUTPUT.
  useGSAP(
    () => {
      if (!running) return
      gsap.matchMedia().add(MOTION, () => {
        gsap.to('.flow', { strokeDashoffset: -40, duration: 0.8, ease: 'none', repeat: -1 })
      })
    },
    { scope: svg, dependencies: [running], revertOnUpdate: true },
  )

  return (
    <svg ref={svg} className={`wires${running ? ' is-live' : ''}`} aria-hidden="true">
      <defs>
        <clipPath id="rail-clip">
          <rect />
        </clipPath>
      </defs>
      {[0, 1, 2, 3].map((i) => (
        <g key={i} className="link" clipPath={i < 3 ? 'url(#rail-clip)' : undefined}>
          <path className="line" />
          <path className="flow" />
        </g>
      ))}
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <circle key={i} className="sock" r="4" clipPath={i < 4 ? 'url(#rail-clip)' : undefined} />
      ))}
    </svg>
  )
}

/** The take, with transport controls in the app's palette rather than the browser's. */
function Player({ src, poster }: { src: string; poster?: string }) {
  const v = useRef<HTMLVideoElement>(null)
  const [playing, setPlaying] = useState(true)
  const [muted, setMuted] = useState(true)
  const [t, setT] = useState(0)
  const [dur, setDur] = useState(0)
  const toggle = () => {
    const el = v.current
    if (!el) return
    if (el.paused) void el.play().catch(() => {})
    else el.pause()
  }
  return (
    <div className="player">
      <video
        ref={v}
        className="out-video"
        src={src}
        poster={poster}
        autoPlay
        muted={muted}
        loop
        playsInline
        onClick={toggle}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onTimeUpdate={(e) => setT(e.currentTarget.currentTime)}
        onLoadedMetadata={(e) => setDur(e.currentTarget.duration)}
      />
      <div className="transport">
        <button className="btn-icon" onClick={toggle} aria-label={playing ? 'Pause' : 'Play'} title={playing ? 'Pause' : 'Play'}>
          <Icon name={playing ? 'pause' : 'play'} />
        </button>
        <input
          type="range"
          className="scrub"
          min={0}
          max={dur || 0}
          step={0.01}
          value={t}
          aria-label="Position"
          style={{ ['--p' as string]: dur ? t / dur : 0 }}
          onChange={(e) => {
            if (v.current) v.current.currentTime = Number(e.target.value)
          }}
        />
        <span className="time">
          {t.toFixed(1)} / {dur ? dur.toFixed(1) : '–'} s
        </span>
        <button className="btn-icon" onClick={() => setMuted((m) => !m)} aria-label={muted ? 'Unmute' : 'Mute'} title={muted ? 'Unmute' : 'Mute'}>
          <Icon name={muted ? 'muted' : 'sound'} />
        </button>
        <button className="btn-icon" onClick={() => void v.current?.requestFullscreen?.()} aria-label="Full screen" title="Full screen">
          <Icon name="expand" />
        </button>
      </div>
    </div>
  )
}

type SlotProps = {
  label: string
  side: 'a' | 'b'
  value: Upload | null
  busy: boolean
  onFile: (f: File) => void
  onClear: () => void
}

function Slot({ label, side, value, busy, onFile, onClear }: SlotProps) {
  const input = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  return (
    <div
      className={`slot${over ? ' is-over' : ''}${value ? ' has-image' : ''}`}
      onDragOver={(e) => {
        e.preventDefault()
        setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setOver(false)
        const f = e.dataTransfer.files?.[0]
        if (f) onFile(f)
      }}
    >
      {value ? (
        <>
          <img src={uploadUrl(value.id)} alt={`${label}`} />
          {value.clip && (
            <span className="slot-clip" title={side === 'a' ? 'The transition starts from its last frame' : 'The transition lands on its first frame'}>
              Clip{value.clip_seconds ? ` ${value.clip_seconds.toFixed(1)} s` : ''}
            </span>
          )}
          <button className="slot-clear" onClick={onClear} aria-label={`Remove ${label.toLowerCase()}`}>
            <Icon name="close" />
          </button>
        </>
      ) : (
        <button className="slot-empty" onClick={() => input.current?.click()} disabled={busy}>
          <Icon name="upload" />
          <span className="slot-title">{busy ? 'Uploading…' : label}</span>
          <span className="slot-hint">Drop an image, a 1–6 s clip or a 3D model (.glb)</span>
        </button>
      )}
      <input
        ref={input}
        type="file"
        accept="image/png,image/jpeg,image/webp,video/mp4,video/quicktime,video/webm,.glb,model/gltf-binary"
        className="visually-hidden"
        tabIndex={-1}
        aria-label={`Choose ${label.toLowerCase()}`}
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) onFile(f)
          e.target.value = ''
        }}
      />
    </div>
  )
}

/** What Seam read in one shot, bound to the upload it came from so a late answer never lands on a new image. */
type Note = { upload: string; desc: string; ref: string; state: 'reading' | 'ready' | 'failed' | 'deferred'; edited: boolean }

function ShotNote({ label, note, onEdit }: { label: string; note: Note | null; onEdit: (text: string) => void }) {
  if (!note) return <div className="note-cell" />
  const placeholder =
    note.state === 'reading'
      ? 'Reading the shot…'
      : note.state === 'deferred'
        ? 'Seam reads this shot when you press Make. Or describe it here.'
      : note.state === 'failed'
        ? 'Could not read it. Describe it, or Seam tries again when you render.'
        : 'Empty: the prompt will not describe this shot'
  return (
    <label className={`note-cell is-${note.edited ? 'edited' : note.state}`}>
      <span className="note-label">Seam sees · {label}</span>
      <textarea rows={2} value={note.desc} maxLength={400} placeholder={placeholder} onChange={(e) => onEdit(e.target.value)} />
    </label>
  )
}

/** Request fields for one side: omitted while unread (the server reads it), the edit as-is once the user typed. */
function shotFields(side: 'a' | 'b', note: Note | null): ShotFields {
  if (!note || (note.state !== 'ready' && !note.edited)) return {}
  // An edited description keeps no label: the old label could contradict the new text.
  return { [`${side}_desc`]: note.desc.trim(), [`${side}_ref`]: note.edited ? '' : note.ref }
}

function statusText(job: Job): string {
  switch (job.status) {
    case 'queued':
      return job.extra?.backend === 'cloud' ? 'Waiting for Comfy Cloud' : 'Waiting for the GPU'  // the ComfyUI queue is shared
    case 'running':
      return job.progress > 0 ? `Rendering ${Math.round(job.progress * 100)}%` : 'Loading the model'
    case 'succeeded':
      return job.wall_s ? `Done in ${Math.round(job.wall_s)} s` : 'Done'
    case 'failed':
      return 'Failed'
    case 'cancelled':
      return 'Cancelled'
    case 'interrupted':
      return 'Interrupted — make a new take'
  }
}

const DATE = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric' })

/** One line on what the take is: the user's own direction, else the two shots Seam read. */
function caption(job: Job): string {
  const p = job.params
  if (job.error && job.status !== 'succeeded') return job.error
  if (p.prompt_extra) return p.prompt_extra
  const a = p.a_ref || p.a_desc
  const b = p.b_ref || p.b_desc
  return a && b ? `From ${a} to ${b}.` : 'From the first shot to the last shot.'
}

type HistoryProps = {
  jobs: Job[]
  seqs: Sequence[]
  focusId: string | null
  presets: Preset[]
  onOpen: (job: Job) => void
  onOpenSeq: (seq: Sequence) => void
  onRetry: (job: Job) => void
}

function History({ jobs, seqs, focusId, presets, onOpen, onOpenSeq, onRetry }: HistoryProps) {
  const [filter, setFilter] = useState('all')
  const preset = (id: string) => presets.find((p) => p.id === id)
  const effects = Array.from(new Set(jobs.map((j) => j.params.preset)))
  const shown = jobs.filter((j) => filter === 'all' || j.params.preset === filter)


  return (
    <section className="history" aria-label="Your transitions">
      {seqs.length > 0 && (
        <>
          <div className="history-head">
            <h2>Your sequences</h2>
            <span className="total" aria-label={`${seqs.length} sequences`}>{seqs.length}</span>
          </div>
          <ol className="cards cards-seq">
            {seqs.map((q) => {
              const done = q.status === 'succeeded' && !!q.output_url
              const effects = q.segments.map((id) => newer(jobs.find((j) => j.id === id), q.takes?.find((t) => t?.id === id)))
              return (
                <li key={q.id} className={`card is-${q.status}`}>
                  <button className="card-media" onClick={() => onOpenSeq(q)} aria-label={`Open sequence of ${q.shots.length} shots`}>
                    {done && q.poster_url ? (
                      <video src={q.output_url} poster={q.poster_url} muted loop playsInline preload="none"
                        onMouseEnter={(e) => void e.currentTarget.play().catch(() => {})}
                        onMouseLeave={(e) => e.currentTarget.pause()} />
                    ) : (
                      <span className="card-strip">
                        {q.shots.map((s, i) => <img key={i} src={s.first_url} alt="" />)}
                      </span>
                    )}
                    <span className={`chip on-media sock-${done ? 'video' : q.status === 'failed' || q.status === 'cancelled' ? 'error' : 'effect'}`}>
                      {done ? `${q.shots.length} shots` : q.status === 'assembling' ? 'Joining' : q.status}
                    </span>
                  </button>
                  <div className="card-body">
                    <div className="card-meta">
                      <span>Sequence · {q.shots.length} shots</span>
                      <time dateTime={new Date(q.created_at * 1000).toISOString()}>{DATE.format(q.created_at * 1000)}</time>
                    </div>
                    <p className="card-caption">{effects.map((t) => (t ? (preset(t.params.preset)?.name ?? t.params.preset) : '…')).join(' → ')}</p>
                    <div className="card-actions">
                      <button className="btn btn-quiet" onClick={() => onOpenSeq(q)}>Open</button>
                      {done && (
                        <a className="btn-icon" href={q.output_url} download={`seam-sequence-${q.id}.mp4`} title="Download video" aria-label="Download video">
                          <Icon name="download" />
                        </a>
                      )}
                    </div>
                  </div>
                </li>
              )
            })}
          </ol>
        </>
      )}
      <div className="history-head">
        <h2>Your transitions</h2>
        <span className="total" aria-label={`${jobs.length} takes`}>{jobs.length}</span>
      </div>
      {jobs.length === 0 ? (
        <p className="history-empty">Nothing yet. Your takes collect here, each one a ComfyUI workflow you can reopen.</p>
      ) : (
        <>
          <div className="filters" role="tablist" aria-label="Filter by effect">
            {['all', ...effects].map((f) => (
              <button key={f} role="tab" aria-selected={filter === f} onClick={() => setFilter(f)}>
                {f === 'all' ? 'All' : (preset(f)?.name ?? f)}
                <span className="count">{f === 'all' ? jobs.length : jobs.filter((j) => j.params.preset === f).length}</span>
              </button>
            ))}
          </div>
          <ol className="cards">
            {shown.map((j) => {
              const p = preset(j.params.preset)
              const name = p?.name ?? j.params.preset
              const done = j.status === 'succeeded' && !!j.output_url
              const retry = j.status === 'failed' || j.status === 'interrupted' || j.status === 'cancelled'
              return (
                <li key={j.id} className={`card is-${j.status}${j.id === focusId ? ' is-focus' : ''}`}>
                  <button className="card-media" onClick={() => onOpen(j)} aria-label={`Open ${name} take`}>
                    {done && j.poster_url ? (
                      <video
                        src={j.merged_url ?? j.output_url}
                        poster={j.poster_url}
                        muted
                        loop
                        playsInline
                        preload="none"
                        onMouseEnter={(e) => void e.currentTarget.play().catch(() => {})}
                        onMouseLeave={(e) => e.currentTarget.pause()}
                      />
                    ) : (
                      <span className="card-pending">
                        <span className="bar" style={{ ['--p' as string]: j.progress }} />
                      </span>
                    )}
                    <span className={`chip on-media sock-${done ? 'video' : retry ? 'error' : 'effect'}`}>
                      {done ? name : statusText(j)}
                    </span>
                    {j.merged_url && <span className="card-badge">With clips</span>}
                    {j.extra.sequence_id && <span className="card-badge">Sequence take {(j.extra.segment ?? 0) + 1}</span>}
                  </button>
                  <div className="card-body">
                    <div className="card-meta">
                      <span>{j.parent_id ? 'New take' : 'First take'} · seed {j.params.seed}</span>
                      <time dateTime={new Date(j.created_at * 1000).toISOString()}>{DATE.format(j.created_at * 1000)}</time>
                    </div>
                    <p className="card-caption">{caption(j)}</p>
                    <div className="card-actions">
                      <button className="btn btn-quiet" onClick={() => (retry ? onRetry(j) : onOpen(j))}>
                        {retry ? 'Try again' : 'Open'}
                      </button>
                      {done && (
                        <>
                          <a className="btn-icon" href={receiptUrl(j.id)} target="_blank" rel="noreferrer" title="How it was made" aria-label="How it was made">
                            <Icon name="receipt" />
                          </a>
                          <a className="btn-icon" href={comfyBundleUrl(j.id)} title="Open in ComfyUI: workflow + both shots" aria-label="Download as a ComfyUI workflow">
                            <Icon name="graph" />
                          </a>
                          <a className="btn-icon" href={j.merged_url ?? j.output_url} download={`seam-${j.params.preset}-${j.id}${j.merged_url ? '-full' : ''}.mp4`} title="Download video" aria-label="Download video">
                            <Icon name="download" />
                          </a>
                        </>
                      )}
                    </div>
                  </div>
                </li>
              )
            })}
          </ol>
        </>
      )}
    </section>
  )
}

/** One sequence hand-off in the editor: the effect and length of the take from shot i to shot i + 1. */
type Seg = { preset: string; seconds: number; extra: string; model?: string }

type ShotListProps = {
  shots: Upload[]
  segs: Seg[]
  sel: number
  busy: boolean
  name: (preset: string) => string
  onAdd: (files: File[]) => void
  onRemove: (i: number) => void
  onMove: (i: number, by: -1 | 1) => void
  onSelect: (i: number) => void
}

/** Sequence shots in order, with the hand-off between each pair: pick one to edit its effect below. */
function ShotList({ shots, segs, sel, busy, name, onAdd, onRemove, onMove, onSelect }: ShotListProps) {
  const input = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  return (
    <ol className="shotlist">
      {shots.map((s, i) => (
        <Fragment key={`${s.clip ?? s.id}-${i}`}>
          <li className="shot">
            <img src={uploadUrl(s.first ?? s.id)} alt="" />
            <span className="shot-name">
              Shot {i + 1}
              {s.clip && <span className="shot-clip">Clip {s.clip_seconds?.toFixed(1)} s</span>}
            </span>
            <span className="shot-tools">
              <button className="btn-icon btn-xs" disabled={i === 0} onClick={() => onMove(i, -1)} aria-label={`Move shot ${i + 1} earlier`} title="Earlier">
                <Icon name="up" />
              </button>
              <button className="btn-icon btn-xs" disabled={i === shots.length - 1} onClick={() => onMove(i, 1)} aria-label={`Move shot ${i + 1} later`} title="Later">
                <Icon name="down" />
              </button>
              <button className="btn-icon btn-xs" onClick={() => onRemove(i)} aria-label={`Remove shot ${i + 1}`} title="Remove">
                <Icon name="close" />
              </button>
            </span>
          </li>
          {i < shots.length - 1 && segs[i] && (
            <li className="handoff">
              <button aria-pressed={sel === i} onClick={() => onSelect(i)} title="Edit this transition with the Effect node">
                <span className="chip">{name(segs[i].preset)}</span>
                <span>{segs[i].seconds} s</span>
              </button>
            </li>
          )}
        </Fragment>
      ))}
      {shots.length < SEQ_MAX && (
        <li
          className={`shot-add${over ? ' is-over' : ''}`}
          onDragOver={(e) => {
            e.preventDefault()
            setOver(true)
          }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.preventDefault()
            setOver(false)
            onAdd(Array.from(e.dataTransfer.files ?? []).slice(0, SEQ_MAX - shots.length))
          }}
        >
          <button onClick={() => input.current?.click()} disabled={busy}>
            <Icon name="plus" />
            <span>{busy ? 'Uploading…' : shots.length ? 'Add a shot' : 'Add your shots'}</span>
            <span className="slot-hint">Images, 1–6 s clips or .glb · up to {SEQ_MAX}</span>
          </button>
          <input
            ref={input}
            type="file"
            multiple
            accept="image/png,image/jpeg,image/webp,video/mp4,video/quicktime,video/webm,.glb,model/gltf-binary"
            className="visually-hidden"
            tabIndex={-1}
            aria-label="Choose shots"
            onChange={(e) => {
              onAdd(Array.from(e.target.files ?? []).slice(0, SEQ_MAX - shots.length))
              e.target.value = ''
            }}
          />
        </li>
      )}
    </ol>
  )
}

function seqStatusText(seq: Sequence, takes: (Job | undefined)[]): string {
  const done = takes.filter((t) => t?.status === 'succeeded').length
  switch (seq.status) {
    case 'running': {
      const cur = takes.find((t) => t?.status === 'running')
      return cur ? `Take ${takes.indexOf(cur) + 1} of ${takes.length} · ${statusText(cur)}` : `${done} of ${takes.length} takes ready · waiting for the GPU`
    }
    case 'assembling':
      return 'Joining the takes'
    case 'succeeded':
      return `${seq.shots.length} shots · ${takes.length} transitions`
    case 'failed':
      return 'Failed'
    case 'cancelled':
      return 'Cancelled'
  }
}

/** The two shots side by side with the effect on the seam: what the take will travel between. */
function Diptych({ a, b, label, dim }: { a: string | null; b: string | null; label: string; dim?: boolean }) {
  return (
    <div className={`diptych${dim ? ' is-dim' : ''}`}>
      <div className="dip-side">{a ? <img src={uploadUrl(a)} alt="First shot" /> : <span>First shot</span>}</div>
      <div className="dip-seam">
        <span className="dip-label">{label}</span>
      </div>
      <div className="dip-side">{b ? <img src={uploadUrl(b)} alt="Last shot" /> : <span>Last shot</span>}</div>
    </div>
  )
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [healthError, setHealthError] = useState<string | null>(null)
  const onCloud = health?.backend === 'cloud'
  const [presets, setPresets] = useState<Preset[]>([])
  const [family, setFamily] = useState('all')
  const [preset, setPreset] = useState<string>('')
  const [a, setA] = useState<Upload | null>(null)
  const [b, setB] = useState<Upload | null>(null)
  const [uploading, setUploading] = useState<'a' | 'b' | null>(null)
  const [extra, setExtra] = useState('')
  const [seed, setSeed] = useState('')
  const [seconds, setSeconds] = useState(5)
  const [modelPick, setModelPick] = useState('')  // '': the effect's own model
  const [models, setModels] = useState<ModelRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [focusId, setFocusId] = useState<string | null>(null)
  const [showFull, setShowFull] = useState(true)
  const [notes, setNotes] = useState<{ a: Note | null; b: Note | null }>({ a: null, b: null })
  const { jobs, seqs, hydrate } = useLive()
  const [growing, setGrowing] = useState(false)
  const growLock = useRef(false)  // state updates lag a fast second drop; this does not
  const [story, setStory] = useState('')  // sequence mode: the idea of the whole film
  const [editingTake, setEditingTake] = useState<number | null>(null)  // the transition whose New take is open
  const [seqEditing, setSeqEditing] = useState(false)
  const seqEditLock = useRef(false)
  const [mode, setMode] = useState<'pair' | 'seq'>('pair')
  const [shots, setShots] = useState<Upload[]>([])
  const [segs, setSegs] = useState<Seg[]>([])
  const [sel, setSel] = useState(0)
  const [seqBusy, setSeqBusy] = useState(0)  // shots still uploading
  const [focusSeqId, setFocusSeqId] = useState<string | null>(null)
  const [estimate, setEstimate] = useState<number | null>(null)
  const [examples, setExamples] = useState<Example[]>([])
  const [model, setModel] = useState<{ file: File; side: 'a' | 'b' | 'seq' } | null>(null)
  const root = useRef<HTMLDivElement>(null)
  const doneCount = jobs.filter((j) => j.status === 'succeeded').length
  useEffect(() => {
    setEstimate(null)
    api.estimate(seconds, modelPick || undefined).then((e) => setEstimate(e.seconds)).catch(() => {})
  }, [doneCount, seconds, modelPick])

  const updateNote = useCallback((which: 'a' | 'b', upload: string, change: (n: Note) => Note) => {
    setNotes((prev) => {
      const n = prev[which]
      return n && n.upload === upload ? { ...prev, [which]: change(n) } : prev
    })
  }, [])

  const read = useCallback(
    (which: 'a' | 'b', upload: string) => {
      setNotes((prev) => ({ ...prev, [which]: { upload, desc: '', ref: '', state: 'reading', edited: false } }))
      api
        .describe(upload)
        .then((t) => updateNote(which, upload, (n) => (n.edited ? n : { ...n, ...t, state: t.deferred ? 'deferred' : t.desc ? 'ready' : 'failed' })))
        .catch(() => updateNote(which, upload, (n) => (n.edited ? n : { ...n, state: 'failed' })))
    },
    [updateNote],
  )

  useEffect(() => {
    api.examples().then(setExamples).catch(() => {})
    api.health().then(setHealth).catch((e) => setHealthError(String(e.message ?? e)))
    api.presets()
      .then((ps) => {
        setPresets(ps)
        setPreset((cur) => (ps.some((p) => p.id === cur) ? cur : (ps[0]?.id ?? '')))
      })
      .catch((e) => setError(String(e.message ?? e)))
  }, [])

  const families = useMemo(() => ['all', ...Array.from(new Set(presets.map((p) => p.category)))], [presets])
  const shown = presets.filter((p) => family === 'all' || p.category === family)
  const current = presets.find((p) => p.id === preset)
  const focus = jobs.find((j) => j.id === focusId) ?? null
  const fseq = seqs.find((q) => q.id === focusSeqId) ?? null
  const fseqTakes = fseq ? fseq.segments.map((id) => newer(jobs.find((j) => j.id === id), fseq.takes?.find((t) => t?.id === id))) : []
  const fseqActive = !!fseq && SEQ_ACTIVE.has(fseq.status)
  const fseqDone = fseq?.status === 'succeeded' && !!fseq.output_url
  const presetName = (id: string) => presets.find((p) => p.id === id)?.name ?? id
  const running = jobs.some((j) => ACTIVE.has(j.status))
  const focusActive = !!focus && ACTIVE.has(focus.status)
  const focusDone = focus?.status === 'succeeded' && !!focus.output_url
  const effectLabel = preset === SURPRISE ? 'Surprise me' : (current?.name ?? 'Pick an effect')
  const lengthsOf = (p?: Preset) => allowedLengths(p, modelPick, models)
  const fxOptions = useMemo(() => presets.map((p) => ({ id: p.id, name: p.name, seconds: p.seconds ?? [5], model: p.model })), [presets])
  // every length any effect offers; the picked effect (or, for Surprise me, at least one effect) must offer it
  const lengths = useMemo(() => Array.from(new Set(presets.flatMap(lengthsOf))).sort((x, y) => x - y), [presets, modelPick, models]) // eslint-disable-line react-hooks/exhaustive-deps
  const offers = (s: number) => (preset === SURPRISE ? presets.some((p) => lengthsOf(p).includes(s)) : lengthsOf(current).includes(s))
  useEffect(() => {
    if (presets.length && !offers(seconds)) setSeconds(preset === SURPRISE ? 5 : lengthsOf(current)[0])
  }, [preset, presets, modelPick, models]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {  // what can render depends on the backend; a pick that cannot goes back to the default
    if (!health) return
    api.models().then((r) => {
      setModels(r.models)
      setModelPick((m) => (r.models.find((x) => x.id === m)?.available === false ? '' : m))
    }).catch(() => {})
  }, [health?.backend]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (mode !== 'seq' || !segs[sel]) return
    setModelPick(segs[sel].model ?? '')
    setPreset(segs[sel].preset)
    setSeconds(segs[sel].seconds)
    setExtra(segs[sel].extra)
  }, [mode, sel]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (mode === 'seq') setSegs((prev) => prev.map((g, i) => (i === sel ? { preset, seconds, extra, model: modelPick } : g)))
  }, [preset, seconds, extra, modelPick]) // eslint-disable-line react-hooks/exhaustive-deps

  const addShots = useCallback(async (files: File[]) => {
    setError(null)
    setSeqBusy((n) => n + 1)
    try {
      for (const file of files) {  // one at a time, so shots land in the order they were picked
        if (isModel(file)) {
          setModel({ file, side: 'seq' })  // the 3D studio adds it when its shot is taken
          continue
        }
        try {
          const up = await api.upload(file, 'mid')  // a clip comes back with both ends
          setShots((prev) => (prev.length >= SEQ_MAX ? prev : [...prev, up]))
        } catch (e) {
          setError(`${file.name} could not be used: ${(e as Error).message}`)
        }
      }
    } finally {
      setSeqBusy((n) => n - 1)
    }
  }, [])
  const addShot = useCallback((file: File) => addShots([file]), [addShots])
  // one hand-off per pair of shots: a new shot gets the effect being edited, a removed one takes its hand-off along
  useEffect(() => {
    const n = Math.max(0, shots.length - 1)
    setSegs((prev) => (prev.length === n ? prev : prev.length > n ? prev.slice(0, n)
      : [...prev, ...Array.from({ length: n - prev.length }, () => ({ preset, seconds, extra: '', model: modelPick }))]))
    setSel((v) => Math.min(v, Math.max(0, n - 1)))
  }, [shots.length]) // eslint-disable-line react-hooks/exhaustive-deps
  const removeShot = (i: number) => {
    // the hand-off leaving the removed shot goes (into it, for the last shot); its neighbours now meet
    const next = segs.filter((_, k) => k !== (i === shots.length - 1 ? i - 1 : i))
    const at = Math.min(sel, Math.max(0, next.length - 1))
    setSegs(next)
    setSel(at)
    if (next[at]) {
      setPreset(next[at].preset)
      setModelPick(next[at].model ?? '')
      setSeconds(next[at].seconds)
      setExtra(next[at].extra)
    }
    setShots((prev) => prev.filter((_, k) => k !== i))
  }
  const moveShot = (i: number, by: -1 | 1) =>
    setShots((prev) => {
      const next = [...prev]
      ;[next[i], next[i + by]] = [next[i + by], next[i]]
      return next
    })

  const upload = useCallback(async (which: 'a' | 'b', file: File) => {
    setError(null)
    if (isModel(file)) {
      setModel({ file, side: which })  // the 3D studio turns it into an orbit clip or a still, then uploads that
      return
    }
    setUploading(which)
    try {
      const up = await api.upload(file, which)
      if (which === 'a') setA(up)
      else setB(up)
      read(which, up.id)
    } catch (e) {
      setError(`That file could not be used: ${(e as Error).message}`)
    } finally {
      setUploading(null)
    }
  }, [read])

  // Paste an image: fills the first empty slot.
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const file = Array.from(e.clipboardData?.files ?? []).find((f) => f.type.startsWith('image/') || f.type.startsWith('video/'))
      if (file) {
        if (mode === 'seq') addShot(file)
        else upload(a ? 'b' : 'a', file)
      }
    }
    window.addEventListener('paste', onPaste)
    return () => window.removeEventListener('paste', onPaste)
  }, [a, upload, mode, addShot])



  // A finished take opens from the seam outward.
  useGSAP(
    () => {
      if (!focusDone && !fseqDone) return
      gsap.matchMedia().add(MOTION, () => {
        gsap.fromTo(
          '.out-video',
          { clipPath: 'inset(0% 50% 0% 50%)' },
          { clipPath: 'inset(0% 0% 0% 0%)', duration: 1.1, ease: 'expo.out', clearProps: 'clipPath' },
        )
      })
    },
    { scope: root, dependencies: [focus?.id, focusDone, fseq?.output_url] },
  )

  const makeSequence = async () => {
    setError(null)
    try {
      const seq = await api.createSequence({
        story: story.trim(),
        shots: shots.map((u) => (u.clip && u.first && u.last ? { clip: u.clip, first: u.first, last: u.last } : { image: u.id })),
        segments: segs.map((g) => {
          const pool = presets.filter((p) => allowedLengths(p, g.model ?? '', models).includes(g.seconds))
          return {
            preset: g.preset === SURPRISE ? (pool[Math.floor(Math.random() * pool.length)]?.id ?? '') : g.preset,
            model: g.model || undefined,
            seconds: g.seconds,
            prompt_extra: g.extra.trim(),
            seed: seed.trim() ? Number(seed) : undefined,
          }
        }),
      })
      setFocusId(null)
      setFocusSeqId(seq.id)
      if (window.matchMedia('(max-width: 960px)').matches) document.querySelector('.output')?.scrollIntoView({ behavior: 'smooth' })
    } catch (e) {
      setError((e as Error).message)
    }
  }

  /** A new take of transition i: same direction (a new seed) or the changed effect / length / direction / seed. */
  const remakeSegment = async (seq: Sequence, i: number, body: Remake): Promise<boolean> => {
    if (seqEditLock.current) return false
    seqEditLock.current = true
    setSeqEditing(true)
    setError(null)
    try {
      hydrate(await api.refineSegment(seq.id, i + 1, body))
      return true
    } catch (e) {
      setError(`Could not start that take: ${(e as Error).message}`)
      return false
    } finally {
      seqEditLock.current = false
      setSeqEditing(false)
    }
  }

  /** Storyboard Apply: remove / reorder shots and save the story; only new hand-offs render. */
  const editSequence = async (seq: Sequence, body: EditIn): Promise<boolean> => {
    if (seqEditLock.current) return false
    seqEditLock.current = true
    setSeqEditing(true)
    setError(null)
    setEditingTake(null)
    try {
      hydrate(await api.editSequence(seq.id, body))
      return true
    } catch (e) {
      setError(`Could not apply those changes: ${(e as Error).message}`)
      return false
    } finally {
      seqEditLock.current = false
      setSeqEditing(false)
    }
  }

  /** The next shot after a finished take (it becomes a sequence) or a finished sequence: only the new transition renders. */
  const grow = async (from: { job: Job } | { seq: Sequence }, file: File, effect: string, direction: string, pick = ''): Promise<boolean> => {
    if (growLock.current) return false
    growLock.current = true
    setError(null)
    setGrowing(true)
    try {
      const up = await api.upload(file, 'mid')
      const pool = presets.filter((p) => allowedLengths(p, pick, models).includes(seconds))
      const source = pool.length ? pool : presets  // none at this length on this model: the length moves below
      const chosen = effect === SURPRISE ? (source[Math.floor(Math.random() * source.length)]?.id ?? '') : effect
      const allowed = allowedLengths(presets.find((p) => p.id === chosen), pick, models)
      const body = {
        shot: up.clip && up.first && up.last ? { clip: up.clip, first: up.first, last: up.last } : { image: up.id },
        segment: {
          preset: chosen,
          model: pick || undefined,
          seconds: allowed.includes(seconds) ? seconds : allowed[0],
          prompt_extra: direction,  // this transition's own direction, not the Direction panel's
          seed: seed.trim() ? Number(seed) : undefined,
        },
      }
      const seq = 'job' in from ? await api.continueTake(from.job.id, body) : await api.extendSequence(from.seq.id, body)
      hydrate(seq)
      setFocusId(null)
      setFocusSeqId(seq.id)
      return true
    } catch (e) {
      const msg = (e as Error).message
      setError(/method not allowed|not found/i.test(msg)
        ? 'Could not add that shot: the Seam server is older than this page. Restart `seam serve` and try again.'
        : `Could not add that shot: ${msg}`)
      return false
    } finally {
      growLock.current = false
      setGrowing(false)
    }
  }
  // effects offered next to the new shot: the ones qualified at the current length (Surprise me when there are several)
  const nextEffects = useMemo(() => {
    const pool = presets.filter((p) => (p.seconds ?? [5]).includes(seconds)).map((p) => ({ id: p.id, name: p.name, model: p.model }))
    return pool.length > 1 ? [...pool, { id: SURPRISE, name: 'Surprise me' }] : pool
  }, [presets, seconds])
  // the Effect node's pick, else the effect the story used last, else none (the user picks it next to the shot)
  const nextDefault = (last?: string) =>
    [preset, last ?? ''].find((id) => !!id && nextEffects.some((x) => x.id === id)) ?? ''

  const openSeq = (seq: Sequence) => {
    setFocusId(null)
    setFocusSeqId(seq.id)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const make = async () => {
    if (mode === 'seq') return makeSequence()
    if (!a || !b) return
    setError(null)
    try {
      const pool = presets.filter((p) => lengthsOf(p).includes(seconds))
      const pick = preset === SURPRISE ? pool[Math.floor(Math.random() * pool.length)]?.id ?? '' : preset
      const job = await api.create({
        image_a: a.id,
        image_b: b.id,
        preset: pick,
        model: modelPick || undefined,
        seconds,
        clip_a: a.clip,
        clip_b: b.clip,
        prompt_extra: extra.trim(),
        seed: seed.trim() ? Number(seed) : undefined,
        ...shotFields('a', notes.a),
        ...shotFields('b', notes.b),
      })
      setFocusSeqId(null)
      setFocusId(job.id)
      // Stacked layout: the output sits below the controls, so bring it into view.
      if (window.matchMedia('(max-width: 960px)').matches) document.querySelector('.output')?.scrollIntoView({ behavior: 'smooth' })
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const refine = async (job: Job, changes: { preset?: string } = {}) => {
    setError(null)
    try {
      const next = await api.refine(job.id, changes)
      setFocusSeqId(null)
      setFocusId(next.id)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const tryExample = async (ex: Example) => {
    setError(null)
    try {
      const got = await api.loadExample(ex.id)
      setA(got.a)
      setB(got.b)
      if (presets.some((p) => p.id === got.preset)) setPreset(got.preset)
      setFocusId(null)
      read('a', got.a.id)
      read('b', got.b.id)
    } catch (e) {
      setError(`That example could not be loaded: ${(e as Error).message}`)
    }
  }

  const reuse = (job: Job) => {
    const clip = (c?: string | null) => (c ? { clip: c, clip_url: uploadUrl(c) } : {})
    setA({ id: job.params.image_a, url: uploadUrl(job.params.image_a), width: 0, height: 0, ...clip(job.params.clip_a) })
    setB({ id: job.params.image_b, url: uploadUrl(job.params.image_b), width: 0, height: 0, ...clip(job.params.clip_b) })
    setPreset(job.params.preset)
    setModelPick(job.params.model && job.params.model !== presets.find((p) => p.id === job.params.preset)?.model ? job.params.model : '')
    setSeconds(job.params.seconds ?? 5)
    for (const side of ['a', 'b'] as const) {
      const upload = job.params[`image_${side}`]
      const desc = job.params[`${side}_desc`]
      if (desc) {
        const ref = job.params[`${side}_ref`] ?? ''
        setNotes((prev) => ({ ...prev, [side]: { upload, desc, ref, state: 'ready', edited: false } }))
      } else read(side, upload)
    }
    setMode('pair')
    setFocusSeqId(null)
    setFocusId(job.id)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const videoUrl = focus && (showFull && focus.merged_url ? focus.merged_url : focus.output_url)

  return (
    <ModelsContext.Provider value={models}>
    <div className="app" ref={root}>
      <header className="top">
        <a className="brand" href="/" aria-label="Seam home">
          <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
            <rect x="4" y="7" width="9" height="18" rx="2.5" />
            <rect x="19" y="7" width="9" height="18" rx="2.5" />
            <rect x="15" y="3" width="2" height="26" rx="1" />
          </svg>
          <span className="brand-tag">Seam</span>
        </a>
        <p className="tagline">Two shots in, one transition out. Open models, your GPU or Comfy Cloud, every take a ComfyUI workflow.</p>
        <div className="top-end">
          <BackendControl health={health} healthError={healthError} onChange={(backend) => setHealth((h) => (h ? { ...h, backend } : h))} />
          <a className="btn-icon" href={REPO} target="_blank" rel="noreferrer" title="Seam on GitHub" aria-label="Seam on GitHub">
            <Icon name="github" />
          </a>
        </div>
      </header>

      <div className="studio">
        <aside className="rail" aria-label="Make a transition">
          <div className="rail-scroll">
          <Node wire="shots" title="Shots" socket="image"
            meta={mode === 'seq' ? `${shots.length} of ${SEQ_MAX}` : a && b ? 'Ready' : 'Image or clip'} className="shots">
            <div className="mode">
              <div className="seg seg-sm" role="tablist" aria-label="What to make">
                <button role="tab" aria-selected={mode === 'pair'} onClick={() => setMode('pair')}>Two shots</button>
                <button role="tab" aria-selected={mode === 'seq'} onClick={() => setMode('seq')}>Sequence</button>
              </div>
            </div>
            {mode === 'seq' ? (
              <ShotList shots={shots} segs={segs} sel={sel} busy={seqBusy > 0}
                name={(id) => (id === SURPRISE ? 'Surprise me' : presetName(id))}
                onAdd={addShots} onRemove={removeShot} onMove={moveShot} onSelect={setSel} />
            ) : (<>
            <div className="slots">
              <Slot
                label="First shot"
                side="a"
                value={a}
                busy={uploading === 'a'}
                onFile={(f) => upload('a', f)}
                onClear={() => {
                  setA(null)
                  setNotes((prev) => ({ ...prev, a: null }))
                }}
              />
              <Slot
                label="Last shot"
                side="b"
                value={b}
                busy={uploading === 'b'}
                onFile={(f) => upload('b', f)}
                onClear={() => {
                  setB(null)
                  setNotes((prev) => ({ ...prev, b: null }))
                }}
              />
            </div>
            {!a && !b && examples.length > 0 && (
              <div className="examples">
                <span className="examples-label">Or try an example</span>
                <div className="examples-row">
                  {examples.map((ex) => (
                    <button key={ex.id} className="example" onClick={() => tryExample(ex)} title={`${ex.title} · ${presets.find((p) => p.id === ex.preset)?.name ?? ex.preset}`}>
                      <span className="example-pair">
                        <img src={ex.a_url} alt="" />
                        <img src={ex.b_url} alt="" />
                      </span>
                      <span className="example-name">{ex.title}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
            {(notes.a || notes.b) && (
              <div className="notes">
                <ShotNote label="first" note={notes.a} onEdit={(t) => notes.a && updateNote('a', notes.a.upload, (n) => ({ ...n, desc: t, edited: true }))} />
                <ShotNote label="last" note={notes.b} onEdit={(t) => notes.b && updateNote('b', notes.b.upload, (n) => ({ ...n, desc: t, edited: true }))} />
              </div>
            )}
            </>)}
          </Node>
          <Node wire="effect" title={mode === 'seq' && segs.length ? `Effect · ${sel + 1} → ${sel + 2}` : 'Effect'} socket="effect"
            meta={`${effectLabel} · ${seconds} s`} className="effects-node">
            {families.length > 2 && (
              <div className="seg" role="tablist" aria-label="Effect family">
                {families.map((f) => (
                  <button key={f} role="tab" aria-selected={family === f} onClick={() => setFamily(f)}>
                    {f === 'all' ? 'All' : f === 'vfx' ? 'VFX' : f[0].toUpperCase() + f.slice(1)}
                  </button>
                ))}
              </div>
            )}
            <div className="effects" role="listbox" aria-label="Effects">
              {presets.length > 1 && (
                <button role="option" aria-selected={preset === SURPRISE} className="effect effect-surprise" onClick={() => setPreset(SURPRISE)}>
                  <span className="effect-art">
                    <Icon name="dice" />
                  </span>
                  <span className="effect-name">Surprise me</span>
                  <span className="effect-meta">One of {presets.length}</span>
                </button>
              )}
              {shown.map((p) => (
                <button key={p.id} role="option" aria-selected={preset === p.id} className="effect" onClick={() => setPreset(p.id)}>
                  <span className="effect-art">
                    {p.preview_url ? (
                      <video
                        src={p.preview_url}
                        poster={p.poster_url}
                        muted
                        loop
                        playsInline
                        preload="none"
                        onMouseEnter={(e) => void e.currentTarget.play().catch(() => {})}
                        onMouseLeave={(e) => e.currentTarget.pause()}
                      />
                    ) : p.poster_url ? (
                      <img src={p.poster_url} alt="" />
                    ) : null}
                  </span>
                  <span className="effect-name">{p.name}</span>
                  <span className="effect-meta">{models.find((m) => m.id === modelPick)?.label ?? MODEL_LABEL[p.model ?? ''] ?? ''}</span>
                </button>
              ))}
            </div>
            {lengths.length > 1 && (
              <div className="length">
                <span className="length-label">Length</span>
                <div className="seg seg-sm" role="radiogroup" aria-label="Take length">
                  {lengths.map((s) => (
                    <button
                      key={s}
                      role="radio"
                      aria-checked={seconds === s}
                      aria-selected={seconds === s}
                      disabled={!offers(s)}
                      title={offers(s) ? undefined : `${effectLabel} is tuned for ${lengthsOf(current).join(' or ')} s takes`}
                      onClick={() => setSeconds(s)}
                    >
                      {s} s
                    </button>
                  ))}
                </div>
              </div>
            )}
            <ModelSelect value={modelPick} onChange={setModelPick} effectModel={preset === SURPRISE ? undefined : current?.model} />
          </Node>
          <Node wire="direction" title="Direction" socket="text" meta="Optional" className="direction">
            {mode === 'seq' && (
              <label className="story-field">
                <span>The whole sequence</span>
                <textarea rows={2} value={story} maxLength={300} placeholder="One idea for the whole film: who, where, the mood"
                  onChange={(e) => setStory(e.target.value)} />
              </label>
            )}
            <details className="more">
              <summary>Describe the moment, set a seed</summary>
              <label>
                <span>What happens during the change</span>
                <textarea rows={2} value={extra} maxLength={600} placeholder="She turns toward the camera and smiles, warm evening light" onChange={(e) => setExtra(e.target.value)} />
              </label>
              <label>
                <span>Seed</span>
                <input value={seed} inputMode="numeric" placeholder="Random" onChange={(e) => setSeed(e.target.value.replace(/\D/g, ''))} />
              </label>
            </details>
          </Node>
          </div>
          <div className="make">
            <button data-wire="make" className="btn btn-primary btn-make"
              disabled={!health || (mode === 'seq' ? shots.length < 2 || seqBusy > 0 : !a || !b)} onClick={make}>
              {mode === 'seq' ? `Make sequence${segs.length ? ` · ${segs.length} transition${segs.length > 1 ? 's' : ''}` : ''}` : running ? 'Add to queue' : 'Make transition'}
            </button>
            <p className="make-note">
              {mode === 'seq'
                ? shots.length < 2
                  ? 'Add at least two shots, in the order they play.'
                  : onCloud
                    ? 'Each transition renders on Comfy Cloud, then Seam joins them · uses your credits'
                    : 'Each transition renders on your GPU, then Seam joins them · no credits'
                : !a || !b
                ? 'Add a first and a last shot to start.'
                : onCloud
                  ? `${estimate ? `About ${estimate} s once it starts · ` : ''}Your shots go to Comfy Cloud · uses your credits`
                  : estimate
                    ? `About ${estimate} s once it starts · no credits, runs on your GPU`
                    : 'Runs on your GPU · no credits'}
            </p>
            {error && (
              <p className="error" role="alert">
                {error}
              </p>
            )}
          </div>
        </aside>

        <main className="canvas">
          {fseq ? (
          <Node
            title="Output"
            socket="video"
            className={`output is-${fseq.status}`}
            label="Output"
            wire="output"
            meta={
              <>
                {seqStatusText(fseq, fseqTakes)}
                <button className="btn-icon btn-xs" onClick={() => setFocusSeqId(null)} title="Close this sequence" aria-label="Close this sequence">
                  <Icon name="close" />
                </button>
              </>
            }
          >
            <div className="out-stage">
              {fseqDone ? (
                <Player key={fseq.output_url} src={fseq.output_url!} poster={fseq.poster_url} />
              ) : (
                <div className={`seq-stage${fseqActive ? ' is-dim' : ''}`}>
                  {fseq.shots.map((s, i) => <img key={i} src={s.first_url} alt={`Shot ${i + 1}`} />)}
                </div>
              )}
              {fseqActive && (
                <div className="progress" aria-live="polite">
                  <span className="progress-status">{seqStatusText(fseq, fseqTakes)}</span>
                  <span className="bar" style={{ ['--p' as string]: (fseqTakes.reduce((n, t) => n + (t?.status === 'succeeded' ? 1 : (t?.progress ?? 0)), 0)) / Math.max(1, fseqTakes.length) }} />
                  <button className="btn btn-quiet btn-sm" onClick={() => api.cancelSequence(fseq.id)}>
                    Cancel
                  </button>
                </div>
              )}
              {(fseq.status === 'failed' || fseq.status === 'cancelled') && (
                <div className="progress is-error" role="alert">
                  <span className="progress-status">{seqStatusText(fseq, fseqTakes)}</span>
                  {fseq.error && <span className="progress-error">{fseq.error}</span>}
                  {fseqTakes.every((t) => t?.status === 'succeeded') && (
                    <button className="btn btn-quiet btn-sm" onClick={() => api.assembleSequence(fseq.id).catch((e) => setError(e.message))}>
                      Join again
                    </button>
                  )}
                </div>
              )}
            </div>
            <Storyboard key={`board-${fseq.id}`} seq={fseq} takes={fseqTakes} effects={fxOptions} busy={seqEditing}
              onApply={(body) => editSequence(fseq, body)} />
            <ol className="seq-takes" aria-label="Transitions in this sequence">
              {fseqTakes.map((t, i) => (
                <li key={t?.id ?? i} className={`seq-take is-${t?.status ?? 'missing'}${editingTake === i ? ' is-editing' : ''}`}>
                  <span className="seq-take-media">
                    {t?.poster_url ? <img src={t.poster_url} alt="" /> : <span className="bar" style={{ ['--p' as string]: t?.progress ?? 0 }} />}
                  </span>
                  <span className="seq-take-text">
                    <span className="seq-take-title">
                      {i + 1} → {i + 2} · {t ? presetName(t.params.preset) : '…'} · {t?.params.seconds ?? 5} s
                      {t && (t.params.story ?? '') !== (fseq.story ?? '') && (
                        <span className="tag tag-quiet" title="Made before the sequence's story changed; a new take uses the current one">older story</span>
                      )}
                    </span>
                    <span className="seq-take-status">
                      {t ? statusText(t) : 'Not found'}{t?.error && t.status !== 'succeeded' ? ` · ${t.error}` : ''}
                      {t?.params.prompt_extra ? <span className="seq-take-extra"> · “{t.params.prompt_extra}”</span> : null}
                    </span>
                  </span>
                  <span className="seq-take-actions">
                    {t?.status === 'succeeded' && (
                      <button className="btn btn-quiet btn-sm" onClick={() => { setFocusSeqId(null); setFocusId(t.id) }}>Watch</button>
                    )}
                    <button className="btn btn-quiet btn-sm" disabled={fseqActive || !t} aria-expanded={editingTake === i}
                      title={fseqActive ? 'Wait until the sequence settles' : 'A new take of this transition: keep or change its effect and direction'}
                      onClick={() => setEditingTake(editingTake === i ? null : i)}>
                      <Icon name="refresh" /> New take
                    </button>
                  </span>
                  {editingTake === i && t && (
                    <TakeEditor key={t.id} take={t} effects={fxOptions} story={fseq.story ?? ''} busy={seqEditing}
                      onMake={(body) => remakeSegment(fseq, i, body)} onClose={() => setEditingTake(null)} />
                  )}
                </li>
              ))}
            </ol>
            {fseq.shots.length < SEQ_MAX && (
              <NextShot
                key={`next-${fseq.id}`}
                label={`Add shot ${fseq.shots.length + 1}`}
                fromUrl={fseq.shots[fseq.shots.length - 1]?.last_url}
                busy={growing}
                disabled={fseq.status !== 'succeeded'}
                blocked={fseq.status === 'succeeded' ? undefined : 'Add the next shot once this sequence has finished.'}
                effects={nextEffects}
                defaultEffect={nextDefault(fseqTakes[fseqTakes.length - 1]?.params.preset)}
                seconds={seconds}
                onMake={(f, fx, dir, m) => grow({ seq: fseq }, f, fx, dir, m)}
              />
            )}
            <footer className="out-bar">
              <span className="out-meta">
                <span className="tag">Sequence</span>
                <span>{fseq.width}×{fseq.height}</span>
              </span>
              <span className="out-actions">
                {fseqDone && (
                  <a className="btn-icon" href={sequenceReceiptUrl(fseq.id)} target="_blank" rel="noreferrer" title="How it was made" aria-label="How it was made">
                    <Icon name="receipt" />
                  </a>
                )}
                {fseqDone && (
                  <a className="btn btn-primary" href={fseq.output_url} download={`seam-sequence-${fseq.id}.mp4`}>
                    <Icon name="download" /> Download
                  </a>
                )}
              </span>
            </footer>
          </Node>
          ) : (
          <Node
            title="Output"
            socket="video"
            className={`output is-${focus?.status ?? 'idle'}`}
            label="Output"
            wire="output"
            meta={
              <>
                {focus ? statusText(focus) : a && b ? 'Ready to make' : 'Waiting for shots'}
                {focus && (
                  <button className="btn-icon btn-xs" onClick={() => setFocusId(null)} title="Close this take" aria-label="Close this take">
                    <Icon name="close" />
                  </button>
                )}
              </>
            }
          >
            <div className="out-stage">
              {focusDone && videoUrl ? (
                <Player key={`${focus!.id}-${showFull}`} src={videoUrl} poster={focus!.poster_url} />
              ) : a || b || focus ? (
                <Diptych
                  a={focus?.params.image_a ?? a?.id ?? null}
                  b={focus?.params.image_b ?? b?.id ?? null}
                  label={focus ? presetName(focus.params.preset) : effectLabel}
                  dim={focusActive}
                />
              ) : (
                <div className="out-empty">
                  <p className="out-empty-title">Your transition lands here</p>
                  <ol className="out-steps">
                    <li>Drop a first and a last shot</li>
                    <li>Pick an effect, or let Seam surprise you</li>
                    <li>{onCloud ? 'Make it on Comfy Cloud, with your credits' : 'Make it: about a minute on your GPU'}</li>
                  </ol>
                </div>
              )}
              {focusActive && (
                <div className="progress" aria-live="polite">
                  <span className="progress-status">{statusText(focus!)}</span>
                  <span className="bar" style={{ ['--p' as string]: focus!.progress }} />
                  <button className="btn btn-quiet btn-sm" onClick={() => api.cancel(focus!.id)}>
                    Cancel
                  </button>
                </div>
              )}
              {focus && !focusActive && !focusDone && (
                <div className="progress is-error" role="alert">
                  <span className="progress-status">{statusText(focus)}</span>
                  {focus.error && <span className="progress-error">{focus.error}</span>}
                  <button className="btn btn-quiet btn-sm" onClick={() => refine(focus)}>
                    Try again
                  </button>
                </div>
              )}
            </div>
            {focus && (
              <footer className="out-bar">
                <span className="out-meta">
                  <span className="tag">{presetName(focus.params.preset)}</span>
                  <span>{models.find((m) => m.id === focus.params.model)?.label ?? MODEL_LABEL[focus.params.model ?? ''] ?? focus.params.model}</span>
                  {focus.params.model !== presets.find((p) => p.id === focus.params.preset)?.model && <span>Not reviewed with this effect</span>}
                  <span>Seed {focus.params.seed}</span>
                </span>
                <span className="out-actions">
                  {focus.merged_url && (
                    <span className="seg seg-sm" role="tablist" aria-label="Show">
                      <button role="tab" aria-selected={showFull} onClick={() => setShowFull(true)}>
                        With clips
                      </button>
                      <button role="tab" aria-selected={!showFull} onClick={() => setShowFull(false)}>
                        Transition
                      </button>
                    </span>
                  )}
                  {focusDone && (
                    <a className="btn-icon" href={receiptUrl(focus.id)} target="_blank" rel="noreferrer" title="How it was made" aria-label="How it was made">
                      <Icon name="receipt" />
                    </a>
                  )}
                  {focusDone && (
                    <a className="btn btn-outline" href={comfyBundleUrl(focus.id)} title="The same take as a ComfyUI workflow, with both shots">
                      <Icon name="graph" /> <span className="btn-label">Open in ComfyUI</span>
                    </a>
                  )}
                  {!focusActive && (
                    <button className="btn btn-quiet" onClick={() => refine(focus)}>
                      <Icon name="refresh" /> New take
                    </button>
                  )}
                  {focusDone && videoUrl && (
                    <a className="btn btn-primary" href={videoUrl} download={`seam-${focus.params.preset}-${focus.id}${showFull && focus.merged_url ? '-full' : ''}.mp4`}>
                      <Icon name="download" /> Download
                    </a>
                  )}
                </span>
              </footer>
            )}
          </Node>
          )}

          {!fseq && focusDone && focus && !focus.extra.sequence_id && (
            <NextShot key={focus.id} label="Continue with a new shot" fromUrl={uploadUrl(focus.params.image_b)} busy={growing}
              effects={nextEffects} defaultEffect={nextDefault(focus.params.preset)} seconds={seconds}
              onMake={(f, fx, dir, m) => grow({ job: focus }, f, fx, dir, m)} />
          )}

          <History jobs={jobs} seqs={seqs} focusId={focusId} presets={presets} onOpen={reuse} onOpenSeq={openSeq} onRetry={(j) => refine(j)} />
        </main>
      </div>
      <Wires root={root} running={running} />
      {model && (
        <Suspense fallback={null}>
          <GlbStudio
            file={model.file}
            side={model.side === 'seq' ? 'a' : model.side}
            label={model.side === 'a' ? 'First shot' : model.side === 'b' ? 'Last shot' : `Shot ${shots.length + 1}`}
            onShot={(f) => (model.side === 'seq' ? addShot(f) : upload(model.side, f))}
            onClose={() => setModel(null)}
          />
        </Suspense>
      )}
    </div>
    </ModelsContext.Provider>
  )
}
