import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { Icon } from './Icon'
import { api, subscribe, type GeneratedShot, type ImageModel, type ShotImage, type Upload } from './api'

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

  const merge = useCallback((shot: GeneratedShot) => {
    setShots((prev) => [shot, ...prev.filter((s) => s.id !== shot.id)].sort((a, b) => b.created_at - a.created_at))
  }, [])
  useEffect(() => {
    if (!active) return
    const load = () => {
      api.shots().then((r) => {
        setShots(r.shots)
        setModels(r.models)
        setModel((now) => (r.models.find((m) => m.id === now)?.available !== false ? now : r.models.find((m) => m.available !== false)?.id ?? now))
      }).catch((e) => setError((e as Error).message))
    }
    return subscribe({ shot: merge, open: load })
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
  return { shots, models, model, setModel, current, backend, unavailable, error, setError, prompt, setPrompt, aspect, setAspect, quality, setQuality, count, setCount, seed, setSeed, generate, reusePrompt, attempt, merge, reload }
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
                {s.models.map((m) => <option key={m.id} value={m.id} disabled={m.available === false}>{m.label}{m.available === false ? ` (${m.reason})` : ''}</option>)}
              </select>
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

function ImageCard({ studio, shot, image, onUse }: { studio: ShotStudio; shot: GeneratedShot; image: ShotImage; onUse: (t: ShotTarget, u: Upload) => void }) {
  const s = studio
  const [copied, setCopied] = useState(false)
  const edit = (body: { starred?: boolean; trashed?: boolean }) =>
    s.attempt(async () => s.merge(await api.editShotImage(shot.id, image.name, body)))
  const use = (t: ShotTarget) => s.attempt(async () => onUse(t, await api.useShotImage(shot.id, image.name)))
  const copy = () =>
    s.attempt(async () => {
      await navigator.clipboard.writeText(shot.prompt)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  return (
    <li className="card">
      <a className="shot-media" href={image.url} target="_blank" rel="noreferrer"><img src={image.url} alt={shot.prompt} loading="lazy" /></a>
      <div className="card-body">
        <p className="card-caption" title={shot.prompt}>{shot.prompt}</p>
        <div className="card-meta"><span>{shot.model} · {shot.aspect} · {shot.quality} · {shot.width}×{shot.height}</span><span>seed {shot.seed}</span></div>
        {image.deleted_at ? (
          <div className="card-actions"><button className="btn btn-quiet" onClick={() => edit({ trashed: false })}>Restore</button></div>
        ) : (
          <>
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
