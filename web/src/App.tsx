import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import gsap from 'gsap'
import { useGSAP } from '@gsap/react'
import {
  api,
  comfyBundleUrl,
  receiptUrl,
  subscribe,
  uploadUrl,
  type Health,
  type Job,
  type Preset,
  type ShotFields,
  type Example,
  type Upload,
} from './api'

gsap.registerPlugin(useGSAP)

// three.js loads only when someone drops a 3D model.
const GlbStudio = lazy(() => import('./GlbStudio'))
const isModel = (f: File) => /\.glb$/i.test(f.name) || f.type === 'model/gltf-binary'

const MODEL_LABEL: Record<string, string> = { l25: 'LTX-2.5', l23: 'LTX-2.3', h3: 'MiniMax H3' }
const ACTIVE = new Set(['queued', 'running'])
/** Effect picker value for "pick one of the qualified effects for me" (resolved when the take is made). */
const SURPRISE = '__surprise__'
const MOTION = '(prefers-reduced-motion: no-preference)'
const REPO = 'https://github.com/jdnichollsc/seam'

function useJobs() {
  const [jobs, setJobs] = useState<Job[]>([])
  useEffect(() => {
    api.jobs().then(setJobs).catch(() => {})
    return subscribe((job) =>
      setJobs((prev) => {
        const rest = prev.filter((j) => j.id !== job.id)
        return [job, ...rest].sort((a, b) => b.created_at - a.created_at)
      }),
    )
  }, [])
  return jobs
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
  dice: 'M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zM8.5 8.5h.01M15.5 8.5h.01M12 12h.01M8.5 15.5h.01M15.5 15.5h.01',
}

function Icon({ name }: { name: keyof typeof ICONS }) {
  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d={ICONS[name]} />
    </svg>
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
type Note = { upload: string; desc: string; ref: string; state: 'reading' | 'ready' | 'failed'; edited: boolean }

function ShotNote({ label, note, onEdit }: { label: string; note: Note | null; onEdit: (text: string) => void }) {
  if (!note) return <div className="note-cell" />
  const placeholder =
    note.state === 'reading'
      ? 'Reading the shot…'
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
      return 'Waiting for the GPU'  // the ComfyUI queue is shared
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
  focusId: string | null
  presets: Preset[]
  onOpen: (job: Job) => void
  onRetry: (job: Job) => void
}

function History({ jobs, focusId, presets, onOpen, onRetry }: HistoryProps) {
  const [filter, setFilter] = useState('all')
  const preset = (id: string) => presets.find((p) => p.id === id)
  const effects = Array.from(new Set(jobs.map((j) => j.params.preset)))
  const shown = jobs.filter((j) => filter === 'all' || j.params.preset === filter)


  return (
    <section className="history" aria-label="Your transitions">
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
  const [presets, setPresets] = useState<Preset[]>([])
  const [family, setFamily] = useState('all')
  const [preset, setPreset] = useState<string>('')
  const [a, setA] = useState<Upload | null>(null)
  const [b, setB] = useState<Upload | null>(null)
  const [uploading, setUploading] = useState<'a' | 'b' | null>(null)
  const [extra, setExtra] = useState('')
  const [seed, setSeed] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [focusId, setFocusId] = useState<string | null>(null)
  const [showFull, setShowFull] = useState(true)
  const [notes, setNotes] = useState<{ a: Note | null; b: Note | null }>({ a: null, b: null })
  const jobs = useJobs()
  const [estimate, setEstimate] = useState<number | null>(null)
  const [examples, setExamples] = useState<Example[]>([])
  const [model, setModel] = useState<{ file: File; side: 'a' | 'b' } | null>(null)
  const root = useRef<HTMLDivElement>(null)
  const doneCount = jobs.filter((j) => j.status === 'succeeded').length
  useEffect(() => {
    api.estimate().then((e) => setEstimate(e.seconds)).catch(() => {})
  }, [doneCount])

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
        .then((t) => updateNote(which, upload, (n) => (n.edited ? n : { ...n, ...t, state: t.desc ? 'ready' : 'failed' })))
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
  const presetName = (id: string) => presets.find((p) => p.id === id)?.name ?? id
  const running = jobs.some((j) => ACTIVE.has(j.status))
  const focusActive = !!focus && ACTIVE.has(focus.status)
  const focusDone = focus?.status === 'succeeded' && !!focus.output_url
  const effectLabel = preset === SURPRISE ? 'Surprise me' : (current?.name ?? 'Pick an effect')

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
      if (file) upload(a ? 'b' : 'a', file)
    }
    window.addEventListener('paste', onPaste)
    return () => window.removeEventListener('paste', onPaste)
  }, [a, upload])



  // A finished take opens from the seam outward.
  useGSAP(
    () => {
      if (!focusDone) return
      gsap.matchMedia().add(MOTION, () => {
        gsap.fromTo(
          '.out-video',
          { clipPath: 'inset(0% 50% 0% 50%)' },
          { clipPath: 'inset(0% 0% 0% 0%)', duration: 1.1, ease: 'expo.out', clearProps: 'clipPath' },
        )
      })
    },
    { scope: root, dependencies: [focus?.id, focusDone] },
  )

  const make = async () => {
    if (!a || !b) return
    setError(null)
    try {
      const pick = preset === SURPRISE ? presets[Math.floor(Math.random() * presets.length)]?.id ?? '' : preset
      const job = await api.create({
        image_a: a.id,
        image_b: b.id,
        preset: pick,
        clip_a: a.clip,
        clip_b: b.clip,
        prompt_extra: extra.trim(),
        seed: seed.trim() ? Number(seed) : undefined,
        ...shotFields('a', notes.a),
        ...shotFields('b', notes.b),
      })
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
    for (const side of ['a', 'b'] as const) {
      const upload = job.params[`image_${side}`]
      const desc = job.params[`${side}_desc`]
      if (desc) {
        const ref = job.params[`${side}_ref`] ?? ''
        setNotes((prev) => ({ ...prev, [side]: { upload, desc, ref, state: 'ready', edited: false } }))
      } else read(side, upload)
    }
    setFocusId(job.id)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const videoUrl = focus && (showFull && focus.merged_url ? focus.merged_url : focus.output_url)

  return (
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
        <p className="tagline">Two shots in, one transition out. Open models, your own GPU, every take a ComfyUI workflow.</p>
        <div className="top-end">
          <span className={`engine ${health ? 'is-up' : healthError ? 'is-down' : ''}`} role="status">
            {health ? 'Engine ready' : healthError ? 'Engine offline · run seam serve' : 'Connecting…'}
          </span>
          <a className="btn-icon" href={REPO} target="_blank" rel="noreferrer" title="Seam on GitHub" aria-label="Seam on GitHub">
            <Icon name="github" />
          </a>
        </div>
      </header>

      <div className="studio">
        <aside className="rail" aria-label="Make a transition">
          <div className="rail-scroll">
          <Node wire="shots" title="Shots" socket="image" meta={a && b ? 'Ready' : 'Image or clip'} className="shots">
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
          </Node>
          <Node wire="effect" title="Effect" socket="effect" meta={effectLabel} className="effects-node">
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
                  <span className="effect-meta">{MODEL_LABEL[p.model ?? ''] ?? ''}</span>
                </button>
              ))}
            </div>
          </Node>
          <Node wire="direction" title="Direction" socket="text" meta="Optional" className="direction">
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
            <button data-wire="make" className="btn btn-primary btn-make" disabled={!a || !b || !health} onClick={make}>
              {running ? 'Add to queue' : 'Make transition'}
            </button>
            <p className="make-note">
              {!a || !b
                ? 'Add a first and a last shot to start.'
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
                    <li>Make it: about a minute on your GPU</li>
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
                  <span>{MODEL_LABEL[focus.params.model ?? ''] ?? focus.params.model}</span>
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

          <History jobs={jobs} focusId={focusId} presets={presets} onOpen={reuse} onRetry={(j) => refine(j)} />
        </main>
      </div>
      <Wires root={root} running={running} />
      {model && (
        <Suspense fallback={null}>
          <GlbStudio
            file={model.file}
            side={model.side}
            label={model.side === 'a' ? 'First shot' : 'Last shot'}
            onShot={(f) => upload(model.side, f)}
            onClose={() => setModel(null)}
          />
        </Suspense>
      )}
    </div>
  )
}
