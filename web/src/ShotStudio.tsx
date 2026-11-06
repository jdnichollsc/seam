import { ModelsLink, unavailableNote } from './Models'
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Icon } from './Icon'
import { api, subscribe, type GeneratedShot, type ImageModel, type Orbit, type ShotImage, type Upload } from './api'

/** Generate shots: a prompt makes images, and a collection keeps each with its prompt, ready to use again, like Midjourney's. */
const ASPECTS = ['16:9', '9:16', '1:1', '4:3', '3:4', '21:9']
const QUALITIES = [
  ['draft', 'Draft'],
  ['standard', 'Standard'],
  ['high', 'High'],
]
const COUNTS = [1, 2, 3, 4]
type View = 'all' | 'starred' | 'trash'
export type ShotTarget = 'first' | 'last' | 'seq'
export type ShotStudio = ReturnType<typeof useShotStudio>

export function useShotStudio(active: boolean, backend?: string) {
  const [shots, setShots] = useState<GeneratedShot[]>([])
  const [models, setModels] = useState<ImageModel[]>([])
  const [model, setModel] = useState('shot')
  const [error, setError] = useState<string | null>(null)
  const [prompt, setPrompt] = useState('')
  const [aspect, setAspect] = useState('16:9')
  const [quality, setQuality] = useState('standard')
  const [count, setCount] = useState(1)
  const [seed, setSeed] = useState('')
  const [orbitUnavailable, setOrbitUnavailable] = useState<string | null>(null)

  const merge = useCallback((shot: GeneratedShot) => {
    setShots((prev) => [shot, ...prev.filter((s) => s.id !== shot.id)].sort((a, b) => b.created_at - a.created_at))
  }, [])
  useEffect(() => {
    if (!active) return
    const load = () => {
      api.shots().then((r) => {
        setShots(r.shots)
        setModels(r.models)
        setOrbitUnavailable(r.orbit_unavailable)
        setModel((now) => (r.models.find((m) => m.id === now)?.available !== false ? now : r.models.find((m) => m.available !== false)?.id ?? now))
      }).catch((e) => setError((e as Error).message))
    }
    return subscribe({ shot: merge, open: load, download: (d) => d.status === 'done' && load() })  // new models: Shots can do more
  }, [active, backend, merge])  // another backend can change what Shots can do

  const attempt = useCallback(async (action: () => Promise<unknown>) => {
    setError(null)
    try {
      await action()
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])
  const generate = () =>
    attempt(async () => {
      merge(await api.createShot({ prompt: prompt.trim(), aspect, quality, count, model, ...(seed.trim() ? { seed: Number(seed) } : {}) }))
    })
  const reusePrompt = (s: GeneratedShot) => {
    setPrompt(s.prompt)
    setAspect(s.aspect)
    setQuality(s.quality)
    setCount(s.count)
    setSeed('')
    const same = models.find((m) => m.label === s.model && m.available !== false)
    if (same) setModel(same.id)
  }
  const reload = () => attempt(async () => setShots((await api.shots()).shots))

  const current = models.find((m) => m.id === model)
  const unavailable = current?.available === false ? `${current.label}: ${current.reason}` : null
  return { shots, models, model, setModel, current, backend, unavailable, orbitUnavailable, error, setError, prompt, setPrompt, aspect, setAspect, quality, setQuality, count, setCount, seed, setSeed, generate, reusePrompt, attempt, merge, reload }
}

function Seg({ label, options, value, onChange }: { label: string; options: (string | number)[][]; value: string | number; onChange: (v: never) => void }) {
  return (
    <div className="seg seg-sm" role="radiogroup" aria-label={label}>
      {options.map(([id, name]) => (
        <button key={id} role="radio" aria-selected={value === id} aria-checked={value === id} onClick={() => onChange(id as never)}>{name}</button>
      ))}
    </div>
  )
}

export function ShotForm({ studio, tabs }: { studio: ShotStudio; tabs: ReactNode }) {
  const s = studio
  const busy = s.shots.some((x) => x.status === 'queued' || x.status === 'running')
  const empty = !s.prompt.trim()
  const limit = s.current?.max_prompt ?? 4000
  const over = s.prompt.length > limit
  return (
    <>
      <div className="rail-scroll">
        <section className="node" aria-label="Generate shots">
          <header className="node-head">
            <span className="chip sock-text">Generate shots</span>
            <span className="node-meta">{s.backend === 'cloud' ? 'Comfy Cloud' : 'Your GPU'}</span>
          </header>
          <div className="shot-form">
            {tabs}
            <label className="shot-field">
              <span>Model</span>
              <select value={s.model} onChange={(e) => s.setModel(e.target.value)}>
                {s.models.map((m) => <option key={m.id} value={m.id} disabled={m.available === false}>{m.label}{unavailableNote(m)}</option>)}
              </select>
              {s.models.some((m) => m.available === false) && <ModelsLink />}
            </label>
            <label className="shot-field">
              <span>Prompt <small className={over ? 'shot-over' : undefined}>{s.prompt.length}/{limit}</small></span>
              <textarea rows={6} value={s.prompt}
                placeholder="Framing, environment, light, lens, style"
                onChange={(e) => s.setPrompt(e.target.value)} />
            </label>
            <div className="shot-field"><span>Aspect</span><Seg label="Aspect" options={ASPECTS.map((a) => [a, a])} value={s.aspect} onChange={s.setAspect} /></div>
            <div className="shot-field"><span>Quality</span><Seg label="Quality" options={QUALITIES} value={s.quality} onChange={s.setQuality} /></div>
            <div className="shot-field"><span>How many</span><Seg label="How many" options={COUNTS.map((c) => [c, c])} value={s.count} onChange={s.setCount} /></div>
            <label className="shot-field">
              <span>Seed</span>
              <input value={s.seed} inputMode="numeric" placeholder="Random" onChange={(e) => s.setSeed(e.target.value.replace(/\D/g, ''))} />
            </label>
          </div>
        </section>
      </div>
      <div className="make">
        <button className="btn btn-primary btn-make" disabled={empty || over || !!s.unavailable} onClick={s.generate}>
          {busy ? 'Add to queue' : s.count > 1 ? `Generate ${s.count} shots` : 'Generate shot'}
        </button>
        <p className="make-note">{s.unavailable ?? (empty ? 'Write a prompt first' : over ? `${s.current?.label} takes up to ${limit} characters` : s.backend === 'cloud' ? 'Runs on Comfy Cloud · uses your credits' : 'Runs on your GPU · no credits')}</p>
        {s.error && <p className="error" role="alert">{s.error}</p>}
      </div>
    </>
  )
}

/** The orbit decoded once into its frames, so the frame shown is the frame sent: drag across it or use the slider. */
function OrbitScrubber({ orbit, alt, frame, setFrame }: { orbit: Orbit; alt: string; frame: number; setFrame: (i: number) => void }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const [frames, setFrames] = useState<ImageBitmap[]>([])
  const drag = useRef<{ x: number; start: number } | null>(null)
  useEffect(() => {
    let stop = false
    const out: ImageBitmap[] = []
    const video = document.createElement('video')
    video.muted = true
    video.preload = 'auto'
    video.src = orbit.url ?? ''
    const seek = (t: number) => new Promise<void>((done) => { video.onseeked = () => done(); video.currentTime = t })
    video.onloadeddata = async () => {
      for (let i = 0; i < orbit.frames && !stop; i++) {
        await seek((i + 0.5) / 24)  // the middle of frame i at 24 fps
        out.push(await createImageBitmap(video))
      }
      if (!stop) setFrames(out)
    }
    return () => { stop = true; video.removeAttribute('src'); video.load() }
  }, [orbit.url, orbit.frames])
  useEffect(() => {
    const c = canvas.current
    const bmp = frames[frame]
    if (!c || !bmp) return
    c.width = bmp.width
    c.height = bmp.height
    c.getContext('2d')?.drawImage(bmp, 0, 0)
  }, [frames, frame])
  if (!frames.length) return <div className="orbit-loading">Reading the orbit…</div>
  const last = frames.length - 1
  return (
    <div className="orbit">
      <canvas ref={canvas} role="img" aria-label={`${alt}, orbit frame ${frame + 1} of ${frames.length}`}
        onPointerDown={(e) => { drag.current = { x: e.clientX, start: frame }; e.currentTarget.setPointerCapture(e.pointerId) }}
        onPointerMove={(e) => {
          if (!drag.current) return
          const step = (e.clientX - drag.current.x) / e.currentTarget.clientWidth * frames.length  // one width of the picture = one turn
          setFrame((((Math.round(drag.current.start + step)) % frames.length) + frames.length) % frames.length)
        }}
        onPointerUp={() => { drag.current = null }} />
      <input type="range" min={0} max={last} value={frame} aria-label="Orbit angle" onChange={(e) => setFrame(Number(e.target.value))} />
      <span className="orbit-angle">{Math.round((frame / last) * 360)}°</span>
    </div>
  )
}

/** The orbit, large: turn it, then use the frame you see as a first or last shot or in a sequence. */
function OrbitDialog({ orbit, alt, onUse, onClose }: { orbit: Orbit; alt: string; onUse: (t: ShotTarget, frame: number) => void; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [frame, setFrame] = useState(0)
  useEffect(() => {
    dialog.current?.showModal()
  }, [])
  const angle = Math.round((frame / (orbit.frames - 1)) * 360)
  return (
    <dialog ref={dialog} className="glb orbit-dialog" onClose={onClose} aria-label="360° orbit of this shot">
      <header className="glb-head">
        <span className="chip sock-image">Orbit 360°</span>
        <span className="glb-file">{orbit.width}×{orbit.height} · {orbit.frames} frames</span>
        <button className="btn-icon btn-xs" onClick={onClose} aria-label="Close"><Icon name="close" /></button>
      </header>
      <div className="orbit-stage"><OrbitScrubber orbit={orbit} alt={alt} frame={frame} setFrame={setFrame} /></div>
      <footer className="glb-foot">
        <p className="glb-hint">Drag across the picture or use the slider. The frame you see is the one you use.</p>
        <div className="shot-use">
          <span>Use {angle}° as</span>
          <button className="btn btn-quiet" onClick={() => onUse('first', frame)}>First</button>
          <button className="btn btn-quiet" onClick={() => onUse('last', frame)}>Last</button>
          <button className="btn btn-quiet" onClick={() => onUse('seq', frame)}>Sequence</button>
        </div>
      </footer>
    </dialog>
  )
}

function ImageCard({ studio, shot, image, onUse }: { studio: ShotStudio; shot: GeneratedShot; image: ShotImage; onUse: (t: ShotTarget, u: Upload) => void }) {
  const s = studio
  const [copied, setCopied] = useState(false)
  const [open, setOpen] = useState(false)
  const edit = (body: { starred?: boolean; trashed?: boolean }) =>
    s.attempt(async () => s.merge(await api.editShotImage(shot.id, image.name, body)))
  const orbit = image.orbit
  const orbitDone = orbit?.status === 'succeeded' && !!orbit.url
  const orbitBusy = orbit?.status === 'queued' || orbit?.status === 'running'
  const canOrbit = !s.orbitUnavailable && s.backend !== 'cloud'
  const use = (t: ShotTarget, frame?: number) =>
    s.attempt(async () => onUse(t, await api.useShotImage(shot.id, image.name, frame)))
  const copy = () =>
    s.attempt(async () => {
      await navigator.clipboard.writeText(shot.prompt)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  return (
    <li className="card">
      <div className="shot-media">
        <a href={image.url} target="_blank" rel="noreferrer"><img src={image.url} alt={shot.prompt} loading="lazy" /></a>
        {orbitDone && !image.deleted_at && (
          <button className="orbit-open" onClick={() => setOpen(true)} title="Open the 360° orbit" aria-label="Open the 360° orbit">360°</button>
        )}
      </div>
      {open && orbit && (
        <OrbitDialog orbit={orbit} alt={shot.prompt} onClose={() => setOpen(false)}
          onUse={(t, frame) => { setOpen(false); use(t, frame) }} />
      )}
      <div className="card-body">
        <p className="card-caption" title={shot.prompt}>{shot.prompt}</p>
        <div className="card-meta"><span>{shot.model} · {shot.aspect} · {shot.quality} · {shot.width}×{shot.height}</span><span>seed {shot.seed}</span></div>
        {image.deleted_at ? (
          <div className="card-actions"><button className="btn btn-quiet" onClick={() => edit({ trashed: false })}>Restore</button></div>
        ) : (
          <>
            {canOrbit && orbitBusy && orbit && (
              <div className="shot-orbit" aria-live="polite">
                <span>{orbit.status === 'queued' ? 'Orbit 360° · queued' : orbit.progress > 0 ? `Orbit 360° · ${Math.round(orbit.progress * 100)}%` : 'Orbit 360° · loading the model'}</span>
                <span className="bar" style={{ ['--p' as string]: orbit.progress }} />
                <button className="btn btn-quiet" onClick={() => s.attempt(async () => s.merge(await api.cancelOrbit(orbit.id)))}>Stop</button>
              </div>
            )}
            {canOrbit && orbit?.status === 'failed' && <p className="shot-over" title={orbit.error ?? ''}>The orbit failed. Try it again.</p>}
            <div className="shot-use">
              <span>Use as</span>
              <button className="btn btn-quiet" onClick={() => use('first')}>First</button>
              <button className="btn btn-quiet" onClick={() => use('last')}>Last</button>
              <button className="btn btn-quiet" onClick={() => use('seq')}>Sequence</button>
            </div>
            <div className="card-actions">
              <button className="btn-icon" title="Use this prompt and its settings" aria-label="Use this prompt" onClick={() => s.reusePrompt(shot)}><Icon name="pencil" /></button>
              <button className="btn-icon" title="Variations: same settings, new seed" aria-label="Make variations" disabled={!!s.unavailable}
                onClick={() => s.attempt(async () => s.merge(await api.shotVariation(shot.id)))}><Icon name="dice" /></button>
              <button className={`btn-icon${copied ? ' is-on' : ''}`} title={copied ? 'Copied' : 'Copy prompt'} aria-label="Copy prompt" onClick={copy}><Icon name="copy" /></button>
              <a className="btn-icon" href={image.url} download={image.name} title="Download image" aria-label="Download image"><Icon name="download" /></a>
              <button className={`btn-icon${image.starred ? ' is-on' : ''}`} aria-pressed={image.starred} title={image.starred ? 'Remove the star' : 'Star'}
                aria-label={image.starred ? 'Remove the star' : 'Star this image'} onClick={() => edit({ starred: !image.starred })}><Icon name="star" /></button>
              {!orbitBusy && (  // shown when it cannot run too, so the orbit is never a hidden feature
                <button className="btn-icon" disabled={!canOrbit}
                  title={s.backend === 'cloud' ? 'Orbit 360°: your GPU only (switch to Your GPU)' : s.orbitUnavailable
                    ? `Orbit 360°: ${s.orbitUnavailable}` : orbitDone ? 'Orbit 360° again (replaces this one when done)' : 'Orbit 360°: a full turn around this image, a few minutes on your GPU'}
                  aria-label="Make a 360° orbit" onClick={() => s.attempt(async () => s.merge(await api.orbitShotImage(shot.id, image.name)))}><Icon name="orbit" /></button>
              )}
              <button className="btn-icon" title="Move to the trash" aria-label="Move image to the trash" onClick={() => edit({ trashed: true })}><Icon name="trash" /></button>
            </div>
          </>
        )}
      </div>
    </li>
  )
}

export function ShotCollection({ studio, onUse }: { studio: ShotStudio; onUse: (t: ShotTarget, u: Upload) => void }) {
  const s = studio
  const [view, setView] = useState<View>('all')
  const [search, setSearch] = useState('')
  const words = search.toLowerCase().trim()
  const found = s.shots.filter((x) => !words || x.prompt.toLowerCase().includes(words))
  const pending = view === 'all' ? found.filter((x) => x.images.length === 0) : []
  const shows = (i: ShotImage) => (view === 'trash' ? !!i.deleted_at : !i.deleted_at && (view === 'all' || i.starred))
  const cards = found.flatMap((shot) => shot.images.filter(shows).map((image) => ({ shot, image })))
  const total = s.shots.reduce((n, x) => n + x.images.filter((i) => !i.deleted_at).length, 0)
  return (
    <section className="node" aria-label="Your collection">
      <header className="node-head">
        <span className="chip sock-image">Collection</span>
        <span className="node-meta">{total} {total === 1 ? 'shot' : 'shots'}</span>
      </header>
      <div className="collection-tools">
        <input type="search" aria-label="Search prompts" placeholder="Search prompts" value={search} onChange={(e) => setSearch(e.target.value)} />
        <Seg label="Show" options={[['all', 'All'], ['starred', 'Starred'], ['trash', 'Trash']]} value={view} onChange={setView} />
        {view === 'trash' && cards.length > 0 && (
          <button className="btn btn-quiet" onClick={() => s.attempt(async () => { await api.purgeShots(); s.reload() })}>Empty trash</button>
        )}
      </div>
      {cards.length === 0 && pending.length === 0 && (
        <p className="shot-empty">
          {view === 'trash' ? 'Trashed shots stay for 7 days.' : view === 'starred' ? 'Star a shot to keep it here.' : words ? 'No prompt has those words.' : 'Write a prompt and generate your first shot.'}
        </p>
      )}
      <ol className="shot-grid">
        {pending.map((x) => (
          <li key={x.id} className="card shot-pending">
            <p className="card-caption" title={x.prompt}>{x.prompt}</p>
            <p className={x.error ? 'error' : 'make-note'} role={x.error ? 'alert' : undefined}>{x.error ?? `${x.status}… ${x.aspect} · ${x.quality} · ${x.count} ${x.count > 1 ? 'shots' : 'shot'}`}</p>
            <div className="card-actions">
              {x.status === 'queued' || x.status === 'running' ? (
                <button className="btn btn-quiet" onClick={() => s.attempt(async () => s.merge(await api.cancelShot(x.id)))}>Cancel</button>
              ) : (
                <>
                  <button className="btn btn-quiet" onClick={() => s.reusePrompt(x)}>Use prompt</button>
                  <button className="btn btn-quiet" onClick={() => s.attempt(async () => { await api.dismissShot(x.id); s.reload() })}>Dismiss</button>
                </>
              )}
            </div>
          </li>
        ))}
        {cards.map(({ shot, image }) => <ImageCard key={image.name} studio={s} shot={shot} image={image} onUse={onUse} />)}
      </ol>
    </section>
  )
}
