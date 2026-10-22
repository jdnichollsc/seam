import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, subscribe, uploadUrl, type Health, type Job, type Preset, type ShotFields, type Upload } from './api'

const MODEL_LABEL: Record<string, string> = { l25: 'LTX-2.5', l23: 'LTX-2.3', h3: 'MiniMax H3' }
const ACTIVE = new Set(['queued', 'running'])

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

type SlotProps = {
  label: string
  hint: string
  value: Upload | null
  busy: boolean
  onFile: (f: File) => void
  onClear: () => void
}

function Slot({ label, hint, value, busy, onFile, onClear }: SlotProps) {
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
          <img src={uploadUrl(value.id)} alt={`${label} shot`} />
          <button className="slot-clear" onClick={onClear} aria-label={`Remove ${label.toLowerCase()} shot`}>
            Remove
          </button>
        </>
      ) : (
        <button className="slot-empty" onClick={() => input.current?.click()} disabled={busy}>
          <span className="slot-title">{busy ? 'Uploading…' : label}</span>
          <span className="slot-hint">{hint}</span>
        </button>
      )}
      <input
        ref={input}
        type="file"
        accept="image/png,image/jpeg,image/webp"
        className="visually-hidden"
        tabIndex={-1}
        aria-label={`Choose ${label.toLowerCase()} image`}
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
        ? 'Could not read it. Describe it here, or Seam tries again when you render.'
        : 'Empty: the prompt will not describe this shot'
  return (
    <label className={`note-cell is-${note.edited ? 'edited' : note.state}`}>
      <span className="note-label">Seam sees · {label}</span>
      <input value={note.desc} maxLength={400} placeholder={placeholder} onChange={(e) => onEdit(e.target.value)} />
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
  const [notes, setNotes] = useState<{ a: Note | null; b: Note | null }>({ a: null, b: null })
  const jobs = useJobs()

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

  const upload = useCallback(async (which: 'a' | 'b', file: File) => {
    setError(null)
    setUploading(which)
    try {
      const up = await api.upload(file)
      if (which === 'a') setA(up)
      else setB(up)
      read(which, up.id)
    } catch (e) {
      setError(`That image could not be used: ${(e as Error).message}`)
    } finally {
      setUploading(null)
    }
  }, [read])

  // Paste an image: fills the first empty slot.
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const file = Array.from(e.clipboardData?.files ?? []).find((f) => f.type.startsWith('image/'))
      if (file) upload(a ? 'b' : 'a', file)
    }
    window.addEventListener('paste', onPaste)
    return () => window.removeEventListener('paste', onPaste)
  }, [a, upload])

  const make = async () => {
    if (!a || !b) return
    setError(null)
    try {
      const job = await api.create({
        image_a: a.id,
        image_b: b.id,
        preset,
        prompt_extra: extra.trim(),
        seed: seed.trim() ? Number(seed) : undefined,
        ...shotFields('a', notes.a),
        ...shotFields('b', notes.b),
      })
      setFocusId(job.id)
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

  const reuse = (job: Job) => {
    setA({ id: job.params.image_a, url: uploadUrl(job.params.image_a), width: 0, height: 0 })
    setB({ id: job.params.image_b, url: uploadUrl(job.params.image_b), width: 0, height: 0 })
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

  const seamState = focus ? focus.status : 'idle'
  const running = jobs.some((j) => ACTIVE.has(j.status))

  return (
    <div className="app">
      <header className="top">
        <h1 className="wordmark">Seam</h1>
        <p className="tagline">Two shots in, one transition out. Rendered on your own GPU with open models.</p>
        <span className={`engine ${health ? 'is-up' : healthError ? 'is-down' : ''}`} role="status">
          {health
            ? 'Engine ready'
            : healthError
              ? 'Engine offline — start it with `seam serve`'
              : 'Connecting…'}
        </span>
      </header>

      <section className={`stage seam-${seamState}`} aria-label="Shots">
        <Slot
          label="First shot"
          hint="Drop, paste or choose an image"
          value={a}
          busy={uploading === 'a'}
          onFile={(f) => upload('a', f)}
          onClear={() => {
            setA(null)
            setNotes((prev) => ({ ...prev, a: null }))
          }}
        />
        <div className="seam" aria-hidden={!focus}>
          <span className="seam-line" />
          <span className="seam-label">{focus ? presetName(focus.params.preset) : current?.name ?? 'Pick an effect'}</span>
        </div>
        <Slot
          label="Last shot"
          hint="Where the transition lands"
          value={b}
          busy={uploading === 'b'}
          onFile={(f) => upload('b', f)}
          onClear={() => {
            setB(null)
            setNotes((prev) => ({ ...prev, b: null }))
          }}
        />
        {focus && ACTIVE.has(focus.status) && (
          <div className="rendering" aria-live="polite">
            <span className="rendering-status">{statusText(focus)}</span>
            <div className="leader" style={{ ['--p' as string]: focus.progress }} />
            <button onClick={() => api.cancel(focus.id)}>Cancel</button>
          </div>
        )}
        {focus && !ACTIVE.has(focus.status) && (
          <div className="result" aria-live="polite">
            {focus.status === 'succeeded' && focus.output_url ? (
              <video key={focus.id} src={focus.output_url} poster={focus.poster_url} controls autoPlay muted loop playsInline />
            ) : (
              <div className="result-wait">
                <p className="result-status">{statusText(focus)}</p>
                {focus.error && <p className="result-error">{focus.error}</p>}
                <div className="leader" style={{ ['--p' as string]: focus.progress }} />
              </div>
            )}
            <div className="result-bar">
              <span className="result-meta">
                <span>{presetName(focus.params.preset)}</span>
                <span>{MODEL_LABEL[focus.params.model ?? ''] ?? focus.params.model}</span>
                <span>Seed {focus.params.seed}</span>
              </span>
              <span className="result-actions">
                <button onClick={() => refine(focus)}>New take</button>
                {focus.output_url && (
                  <a href={focus.output_url} download={`seam-${focus.params.preset}-${focus.id}.mp4`}>
                    Download
                  </a>
                )}
                <button className="ghost" onClick={() => setFocusId(null)}>
                  Back to shots
                </button>
              </span>
            </div>
          </div>
        )}
      </section>

      {(notes.a || notes.b) && (
        <section className="notes" aria-label="What Seam sees in the shots">
          <ShotNote label="first shot" note={notes.a} onEdit={(t) => notes.a && updateNote('a', notes.a.upload, (n) => ({ ...n, desc: t, edited: true }))} />
          <span />
          <ShotNote label="last shot" note={notes.b} onEdit={(t) => notes.b && updateNote('b', notes.b.upload, (n) => ({ ...n, desc: t, edited: true }))} />
        </section>
      )}

      <section className="effects" aria-label="Effects">
        <div className="effects-head">
          <h2>Effect</h2>
          <div className="families" role="tablist">
            {families.map((f) => (
              <button key={f} role="tab" aria-selected={family === f} className={family === f ? 'on' : ''} onClick={() => setFamily(f)}>
                {f === 'all' ? 'All' : f === 'vfx' ? 'VFX' : f[0].toUpperCase() + f.slice(1)}
              </button>
            ))}
          </div>
        </div>
        <div className="reel" role="listbox" aria-label="Effects">
          {shown.map((p) => (
            <button
              key={p.id}
              role="option"
              aria-selected={preset === p.id}
              className={`frame${preset === p.id ? ' on' : ''}`}
              onClick={() => setPreset(p.id)}
            >
              {p.poster_url ? <img src={p.poster_url} alt="" /> : <span className={`glyph g-${p.category}`} />}
              <span className="frame-name">{p.name}</span>
              <span className="frame-meta">{MODEL_LABEL[p.model ?? ''] ?? ''}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="make">
        <details className="more">
          <summary>Direction and seed</summary>
          <label>
            Extra direction
            <input value={extra} maxLength={600} placeholder="e.g. slower, warmer light, end on her smile" onChange={(e) => setExtra(e.target.value)} />
          </label>
          <label>
            Seed
            <input value={seed} inputMode="numeric" placeholder="random" onChange={(e) => setSeed(e.target.value.replace(/\D/g, ''))} />
          </label>
        </details>
        <button className="primary" disabled={!a || !b || !health} onClick={make}>
          {running ? 'Add to queue' : 'Make transition'}
        </button>
        {!a || !b ? <p className="note">Add a first and a last shot to start.</p> : null}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
      </section>

      <section className="history" aria-label="Your transitions">
        <h2>Your transitions</h2>
        {jobs.length === 0 ? (
          <p className="note">Nothing yet. Your first transition takes about a minute.</p>
        ) : (
          <ol className="takes">
            {jobs.map((j) => (
              <li key={j.id} className={`take is-${j.status}${j.id === focusId ? ' on' : ''}`}>
                <button className="take-thumb" onClick={() => reuse(j)} aria-label={`Open ${presetName(j.params.preset)} take`}>
                  {j.poster_url ? <img src={j.poster_url} alt="" /> : <span className="take-pending" />}
                </button>
                <div className="take-body">
                  <strong>{presetName(j.params.preset)}</strong>
                  <span>{statusText(j)}</span>
                  {j.parent_id && <span className="take-from">New take</span>}
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  )
}
