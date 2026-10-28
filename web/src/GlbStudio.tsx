import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js'

/** Seconds of the recorded orbit and how far the camera travels: a known move, like a turntable. */
const ORBIT_SECONDS = 3
const ORBIT_DEGREES = 120
const FPS = 24
const SIZE = { w: 1280, h: 720 }

type Props = {
  file: File
  label: string
  side: 'a' | 'b'
  onShot: (f: File) => void
  onClose: () => void
}

/**
 * A 3D model as a shot. The model is framed on a neutral studio set; "Record orbit" renders a 3 s camera orbit as a
 * clip (the first shot ends on the orbit's last frame, the last shot starts on its first), "Use this angle" a still.
 * Both go through the same upload path as any image or clip.
 */
export default function GlbStudio({ file, label, side, onShot, onClose }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  const api = useRef<{ still: () => Promise<Blob>; orbit: () => Promise<Blob> } | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'recording' | 'error'>('loading')
  const [error, setError] = useState('')

  useEffect(() => {
    dialog.current?.showModal()
    const el = host.current
    if (!el) return
    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true })
    renderer.setPixelRatio(1)
    renderer.setSize(SIZE.w, SIZE.h, false)
    renderer.toneMapping = THREE.ACESFilmicToneMapping
    renderer.outputColorSpace = THREE.SRGBColorSpace
    el.appendChild(renderer.domElement)

    const scene = new THREE.Scene()
    scene.background = new THREE.Color('#2a2132')
    const pmrem = new THREE.PMREMGenerator(renderer)
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture
    const key = new THREE.DirectionalLight('#ffffff', 1.4)
    key.position.set(3, 5, 4)
    scene.add(key)
    const floor = new THREE.Mesh(new THREE.CircleGeometry(1, 64), new THREE.MeshStandardMaterial({ color: '#19131e', roughness: 0.9 }))
    floor.rotation.x = -Math.PI / 2
    scene.add(floor)

    const camera = new THREE.PerspectiveCamera(35, SIZE.w / SIZE.h, 0.01, 1000)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    let raf = 0
    const loop = () => {
      controls.update()
      renderer.render(scene, camera)
      raf = requestAnimationFrame(loop)
    }

    const url = URL.createObjectURL(file)
    new GLTFLoader().load(
      url,
      (gltf) => {
        const model = gltf.scene
        // Frame it: centre on the floor, camera at a 3/4 view sized to the model.
        const box = new THREE.Box3().setFromObject(model)
        const size = box.getSize(new THREE.Vector3())
        const centre = box.getCenter(new THREE.Vector3())
        model.position.sub(new THREE.Vector3(centre.x, box.min.y, centre.z))
        scene.add(model)
        const radius = Math.max(size.x, size.y, size.z)
        floor.scale.setScalar(radius * 1.6)
        controls.target.set(0, size.y * 0.45, 0)
        camera.position.set(radius * 1.35, size.y * 0.7 + radius * 0.4, radius * 1.85)
        camera.near = radius / 100
        camera.far = radius * 100
        camera.updateProjectionMatrix()
        loop()
        setState('ready')
      },
      undefined,
      (e) => {
        setError(`Could not read this model: ${(e as Error).message ?? e}`)
        setState('error')
      },
    )

    const still = () =>
      new Promise<Blob>((resolve, reject) => {
        renderer.render(scene, camera)
        renderer.domElement.toBlob((b) => (b ? resolve(b) : reject(new Error('no frame'))), 'image/png')
      })

    // A turntable move around the current view, rendered frame by frame into a recorded canvas stream.
    const orbit = () =>
      new Promise<Blob>((resolve, reject) => {
        cancelAnimationFrame(raf)
        controls.enabled = false
        const stream = renderer.domElement.captureStream(FPS)
        const type = ['video/webm;codecs=vp9', 'video/webm'].find((t) => MediaRecorder.isTypeSupported(t)) ?? 'video/webm'
        const rec = new MediaRecorder(stream, { mimeType: type, videoBitsPerSecond: 8_000_000 })
        const chunks: Blob[] = []
        rec.ondataavailable = (e) => e.data.size && chunks.push(e.data)
        rec.onstop = () => {
          controls.enabled = true
          loop()
          resolve(new Blob(chunks, { type: 'video/webm' }))
        }
        rec.onerror = () => reject(new Error('recording failed'))
        const start = camera.position.clone().sub(controls.target)
        const sweep = THREE.MathUtils.degToRad(ORBIT_DEGREES)
        // Paced by the clock, not by frame count: a slow GPU drops frames but the clip still lasts ORBIT_SECONDS.
        let t0 = 0
        rec.start()
        const tick = (now: number) => {
          if (!t0) t0 = now
          const k = Math.min((now - t0) / 1000 / ORBIT_SECONDS, 1)
          const p = start.clone().applyAxisAngle(new THREE.Vector3(0, 1, 0), sweep * k)
          camera.position.copy(controls.target).add(p)
          camera.lookAt(controls.target)
          renderer.render(scene, camera)
          if (k < 1) requestAnimationFrame(tick)
          else setTimeout(() => rec.stop(), 120)
        }
        requestAnimationFrame(tick)
      })

    api.current = { still, orbit }
    return () => {
      cancelAnimationFrame(raf)
      URL.revokeObjectURL(url)
      controls.dispose()
      pmrem.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [file])

  const base = file.name.replace(/\.glb$/i, '') || 'model'
  const take = async (kind: 'still' | 'orbit') => {
    if (!api.current) return
    setState('recording')
    try {
      const blob = await (kind === 'still' ? api.current.still() : api.current.orbit())
      onShot(new File([blob], kind === 'still' ? `${base}.png` : `${base}-orbit.webm`, { type: blob.type }))
      onClose()
    } catch (e) {
      setError((e as Error).message)
      setState('error')
    }
  }

  return (
    <dialog ref={dialog} className="glb" onClose={onClose} aria-label={`3D model for the ${label.toLowerCase()}`}>
      <header className="glb-head">
        <span className="chip sock-image">{label} · 3D model</span>
        <span className="glb-file">{file.name}</span>
        <button className="btn-icon btn-xs" onClick={onClose} aria-label="Close">
          <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M6 6l12 12M18 6 6 18" />
          </svg>
        </button>
      </header>
      <div className="glb-stage" ref={host}>
        {state === 'loading' && <span className="glb-status">Loading the model…</span>}
        {state === 'recording' && <span className="glb-status">Recording…</span>}
      </div>
      {error && <p className="error">{error}</p>}
      <footer className="glb-foot">
        <p className="glb-hint">
          Drag to frame it. The orbit turns the camera {ORBIT_DEGREES}° in {ORBIT_SECONDS} s; the transition
          {side === 'a' ? ' leaves from where the orbit ends.' : ' lands where the orbit starts.'}
        </p>
        <button className="btn btn-quiet" disabled={state !== 'ready'} onClick={() => take('still')}>
          Use this angle
        </button>
        <button className="btn btn-primary" disabled={state !== 'ready'} onClick={() => take('orbit')}>
          Record orbit
        </button>
      </footer>
    </dialog>
  )
}
