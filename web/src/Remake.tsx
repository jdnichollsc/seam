import { useEffect, useRef, useState } from 'react'
import { api, type RemakeOut, type Upload } from './api'
import { Icon } from './Icon'

const VIDEO = ['video/mp4', 'video/quicktime', 'video/webm']
const MAX_BYTES = 200 * 1024 * 1024

const clock = (t: number) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`

/** A video the user made or may reuse, read into shots: its cuts become the shots, every hand-off a cut. Nothing
 *  renders here; "Open as sequence" only fills the form. */
export function RemakeDialog({ shotsInForm, onOpen, onClose }: { shotsInForm: number; onOpen: (shots: Upload[]) => void; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const shown = useRef(true)
  const [file, setFile] = useState<File | null>(null)
  const [footage, setFootage] = useState(false)
  const [over, setOver] = useState(false)
  const [reading, setReading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState<RemakeOut | null>(null)
  const [keep, setKeep] = useState<boolean[]>([])
  const [replacing, setReplacing] = useState(false)
  useEffect(() => {
    dialog.current?.showModal()
    return () => {
      shown.current = false  // Cancel only closes: the server still finishes, and its answer is dropped
    }
  }, [])

  const pick = (f: File | undefined) => {
    setError(null)
    if (!f) return
    if (!VIDEO.includes(f.type)) return setError('A reference is a video: mp4, mov or webm.')
    if (f.size > MAX_BYTES) return setError('That video is larger than 200 MB.')
    setFile(f)
  }
  const read = async () => {
    if (!file) return
    setError(null)
    setReading(true)
    try {
      const out = await api.remake(file, footage)
      if (!shown.current) return
      setDraft(out)
      setKeep(out.beats.map(() => true))
    } catch (e) {
      if (shown.current) setError((e as Error).message)
    } finally {
      if (shown.current) setReading(false)
    }
  }
  const kept = draft ? draft.beats.filter((_, i) => keep[i]).map((b) => b.shot) : []
  const open = () => {
    if (shotsInForm > 0 && !replacing) return setReplacing(true)
    onOpen(kept)
  }

  return (
    <dialog ref={dialog} className="glb models-dialog remake-dialog" onClose={onClose} aria-label="Start from a video">
      <header className="glb-head">
        <span className="chip sock-image">Start from a video</span>
        <span className="glb-file">{draft ? `${draft.beats.length} shots from ${file?.name}` : file?.name ?? 'Its cuts become your shots'}</span>
        <button className="btn-icon btn-xs" onClick={onClose} aria-label="Close"><Icon name="close" /></button>
      </header>
      <div className="models-body">
        {!draft ? (
          <>
            <div
              className={`remake-drop${over ? ' is-over' : ''}`}
              onDragOver={(e) => {
                e.preventDefault()
                setOver(true)
              }}
              onDragLeave={() => setOver(false)}
              onDrop={(e) => {
                e.preventDefault()
                setOver(false)
                pick(e.dataTransfer.files?.[0])
              }}
            >
              <button className="btn btn-quiet" disabled={reading} onClick={() => input.current?.click()}>
                <Icon name="plus" /> {file ? 'Choose another video' : 'Choose a video'}
              </button>
              <span className="slot-hint">or drop it here · mp4, mov or webm · up to 3 min, 200 MB</span>
              <input ref={input} type="file" accept={VIDEO.join(',')} className="visually-hidden" tabIndex={-1} aria-label="Choose a video"
                onChange={(e) => {
                  pick(e.target.files?.[0])
                  e.target.value = ''
                }} />
            </div>
            <fieldset className="remake-mode" disabled={reading}>
              <legend className="visually-hidden">Shots</legend>
              <label><input type="radio" name="remake-mode" checked={!footage} onChange={() => setFootage(false)} /> Re-create with stills</label>
              <label><input type="radio" name="remake-mode" checked={footage} onChange={() => setFootage(true)} /> Use the video's own footage</label>
            </fieldset>
            <p className="get-note">Use a video you made or have permission to reuse.</p>
            {reading ? (
              <div className="shot-orbit" aria-live="polite">
                <span>Reading the video…</span>
                <button className="btn btn-quiet" onClick={onClose}>Cancel</button>
              </div>
            ) : (
              <button className="btn" disabled={!file} onClick={read}>Find its shots</button>
            )}
          </>
        ) : (
          <>
            <p className="get-note">
              {draft.found - 1} cut{draft.found === 2 ? '' : 's'} found in {draft.source.duration.toFixed(1)} s, so {draft.found} shots.
              {' '}Each shot runs from one cut to the next{draft.footage ? '' : '; a still stands for the whole shot'}.
            </p>
            {draft.notes.map((n) => <p key={n} className="get-note">{n}</p>)}
            <ul className="remake-beats">
              {draft.beats.map((b, i) => (
                <li key={b.url}>
                  <label className={keep[i] ? '' : 'is-off'}>
                    <img src={b.url} alt="" />
                    <span>
                      <input type="checkbox" checked={keep[i]} onChange={(e) => {
                        setReplacing(false)
                        setKeep(keep.map((k, j) => (j === i ? e.target.checked : k)))
                      }} />
                      Shot {i + 1}
                    </span>
                    <small>{clock(b.start)}-{clock(b.end)} · {(b.end - b.start).toFixed(1)} s{b.shot.clip ? ' · clip' : ''}</small>
                  </label>
                </li>
              ))}
            </ul>
            <p className="get-note">Every hand-off is a cut. Turn the ones you want into effects in the form; nothing renders yet.</p>
            {replacing ? (
              <div className="remake-replace" role="alert">
                <span>This replaces the {shotsInForm} shot{shotsInForm === 1 ? '' : 's'} in the form.</span>
                <button className="btn" onClick={open}>Replace</button>
                <button className="btn btn-quiet" onClick={() => setReplacing(false)}>Keep mine</button>
              </div>
            ) : (
              <div className="remake-actions">
                <button className="btn" disabled={kept.length < 2} onClick={open}>Open as sequence</button>
                {kept.length < 2 && <span className="get-note">A sequence needs at least 2 shots.</span>}
                <button className="btn btn-quiet" onClick={() => {
                  setDraft(null)
                  setReplacing(false)
                }}>Back</button>
              </div>
            )}
          </>
        )}
        {error && <p className="error">{error}</p>}
      </div>
    </dialog>
  )
}
