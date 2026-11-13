export type Preset = {
  id: string
  name: string
  category: string
  camera: string
  model?: string
  /** take lengths this effect is qualified at */
  seconds?: number[]
  preview_url?: string
  poster_url?: string
}

/** An image, or a clip: then `id` is the frame the transition uses and `clip` the video around it.
 *  Uploaded for a sequence (role mid), a clip also carries both ends: `first` (landed on) and `last` (left from). */
/** One image of a generation in the Shots collection; star and trash are per image. */
/** A 360° orbit of one image (ORB360): the newest for that image. */
export type Orbit = { id: string; status: string; error: string | null; width: number; height: number; frames: number; progress: number; url: string | null }
export type ShotImage = { name: string; url: string; starred: boolean; deleted_at: number | null; orbit: Orbit | null }
/** One generation: a prompt and settings, and the images it made. */
export type ImageModel = { id: string; label: string; available: boolean | null; reason: string; max_prompt: number; ready_elsewhere?: string | null }

export type GeneratedShot = {
  id: string
  status: string
  prompt: string
  aspect: string
  quality: string
  width: number
  height: number
  steps: number
  model: string
  seed: number
  count: number
  images: ShotImage[]
  error: string | null
  parent_id: string | null
  created_at: number
}

export type Upload = {
  id: string
  url: string
  width: number
  height: number
  clip?: string
  clip_url?: string
  clip_seconds?: number
  first?: string
  last?: string
}

export type SequenceStatus = 'running' | 'assembling' | 'succeeded' | 'failed' | 'cancelled'

/** Shots joined by one hand-off each; `segments` holds the current take (job id) of each, or null for a cut. */
export type Sequence = {
  id: string
  status: SequenceStatus
  error: string | null
  width: number
  height: number
  created_at: number
  updated_at: number
  shots: { first: string; last: string; first_url: string; last_url: string; clip?: string; clip_url?: string; hold?: number }[]
  segments: (string | null)[]  // null: a cut (nothing generated)
  output_url?: string
  poster_url?: string
  sheet_url?: string
  /** the idea of the whole sequence; each take keeps the story it was made with (params.story) */
  story?: string
  /** REST only (live events omit it): the current takes, in segment order */
  takes?: (Job | null)[]
  /** in the trash since then */
  deleted_at?: number | null
}

export type SegmentIn = { preset: string; model?: string; seconds?: number; seed?: number; prompt_extra?: string }
/** A model a transition can pick. available: false disabled with a reason, null when it could not be checked. */
export type ModelRow = { id: string; label: string; kind: 'open' | 'partner'; seconds: number[]; available: boolean | null; reason: string; ready_elsewhere?: string | null }
export type ModelAvailability = { state: 'ready' | 'missing' | 'needs_nodes' | 'not_offered' | 'not_checked'; reason: string }
export type DownloadState = { set: string; status: 'running' | 'done' | 'failed' | 'cancelled'; file: string | null; done: number; total: number; error: string | null }
export type DownloadSet = {
  id: string; label: string; enables: string[]; default: boolean; size: number; missing: number; remaining: number; gated: boolean; command: string
  blocker?: string
  files: { folder: string; name: string; size: number; gated: boolean; present: boolean }[]
  licenses: { name: string; url: string; note: string }[]; accepted: boolean; state: DownloadState | null
}
export type DownloadsState = {
  backend: BackendName; blocker: string | null; free: number | null; sets: DownloadSet[]
  models: { id: string; label: string; kind: string; feature: string; set: string | null; local: ModelAvailability; cloud: ModelAvailability }[]
}
export type ShotIn = { image: string } | { clip: string; first: string; last: string }
/** order: current shot numbers in the new order; transitions: number in the NEW order → its effect (new hand-offs) */
export type EditIn = { order: number[]; transitions?: Record<string, SegmentIn>; story?: string; expected_updated_at?: number }

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'

/** What the helper did: `action` is applied to the form at once, `offer` waits for the user's click. */
export type HelperAction =
  | { name: 'pick_effect'; effect: string; label?: string }
  | { name: 'set_length'; seconds: number }
  | { name: 'set_direction'; text: string }
  | { name: 'open_take'; job_id: string }
export type RefineBody = {
  preset?: string
  model?: string
  seconds?: number
  seed?: number
  prompt_extra?: string
  prompt_override?: string
  expected_backend?: string
  expected_updated_at?: number
}
/** A new take resolved on the server: the label says what `body` renders, and only a click sends it. */
export type NewTakeOffer = { name: 'new_take'; label: string; job_id?: string; sequence_id?: string; part?: number; body: RefineBody }
export type HelperOffer = (Extract<HelperAction, { name: 'pick_effect' }> & { label: string }) | NewTakeOffer
export type Selection = { job_id?: string; sequence_id?: string }
export type ChatReply = { say: string; action?: HelperAction; offer?: HelperOffer }
export type HelperTool = { name: string; description: string; args: Record<string, string>; kind: string }

export type Trash = { days: number; jobs: Job[]; sequences: Sequence[] }

export type Job = {
  starred?: boolean
  /** in the trash since then */
  deleted_at?: number | null
  id: string
  status: JobStatus
  progress: number
  updated_at: number
  parent_id: string | null
  error: string | null
  wall_s: number | null
  created_at: number
  params: {
    image_a: string
    image_b: string
    preset: string
    seconds?: number
    seed: number
    model?: string
    prompt_extra?: string
    a_desc?: string | null
    b_desc?: string | null
    a_ref?: string | null
    b_ref?: string | null
    clip_a?: string | null
    clip_b?: string | null
    prompt_override?: string
    story?: string
  }
  output_url?: string
  merged_url?: string
  poster_url?: string
  sheet_url?: string
  extra: { width?: number; height?: number; duration?: number; prompt?: string; sequence_id?: string; segment?: number; backend?: BackendName; trashed_with?: string }
}

/** What Seam sees in a shot: a description and a short label, both used in the prompt. */
export type ShotText = { desc: string; ref: string }

/** Omit a side to let the server read the shot itself; send '' for a generic prompt. */
export type ShotFields = { a_desc?: string; b_desc?: string; a_ref?: string; b_ref?: string }

export type Health = { ok: boolean; model: string; comfy_base_url: string; backend: BackendName; presets: number }
export type BackendName = 'local' | 'cloud'
/** Where takes render. The API key itself never comes back from the server. */
export type BackendState = {
  backend: BackendName
  source: string
  keys_url: string
  cloud: { key_configured: boolean; key_source: string | null }
}

/** A shipped example pair: both shots and the effect it was qualified with. */
export type Example = { id: string; title: string; preset: string; a_url: string; b_url: string }

/** Typical render time on this machine (median of recent takes); null before the first one. */
export type Estimate = { seconds: number | null; samples: number }

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, init)
  if (!r.ok) {
    let detail = r.statusText
    try {
      const body = await r.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
    } catch {
      /* keep statusText */
    }
    throw new Error(detail)
  }
  return (r.status === 204 ? undefined : r.json()) as Promise<T>
}

export const api = {
  health: () => call<Health>('/api/health'),
  backend: () => call<BackendState>('/api/backend'),
  setBackend: (backend: BackendName) =>
    call<BackendState>('/api/backend', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ backend }) }),
  setCloudKey: (api_key: string) =>
    call<BackendState>('/api/backend/key', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ api_key }) }),
  clearCloudKey: () => call<BackendState>('/api/backend/key', { method: 'DELETE' }),
  presets: () => call<Preset[]>('/api/presets'),
  downloads: () => call<DownloadsState>('/api/downloads'),
  startDownload: (set: string) =>
    call<DownloadSet>(`/api/downloads/${set}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ accept: true }) }),
  cancelDownload: (set: string) => call<{ ok: boolean }>(`/api/downloads/${set}/cancel`, { method: 'POST' }),
  jobs: () => call<Job[]>('/api/jobs?limit=60'),
  upload: (file: File, role: 'a' | 'b' | 'mid') => {
    const body = new FormData()
    body.append('file', file)
    return call<Upload>(`/api/uploads?role=${role}`, { method: 'POST', body })
  },
  describe: (uploadId: string) => call<ShotText & { deferred?: boolean }>(`/api/describe/${uploadId}`, { method: 'POST' }),
  create: (
    body: { image_a: string; image_b: string; preset: string; model?: string; seconds?: number; seed?: number; prompt_extra?: string; clip_a?: string; clip_b?: string } & ShotFields,
  ) =>
    call<Job>('/api/transitions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  refine: (id: string, body: RefineBody) =>
    call<Job>(`/api/jobs/${id}/refine`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  cancel: (id: string) => call<Job>(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  star: (id: string, starred: boolean) =>
    call<Job>(`/api/jobs/${id}/star`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ starred }),
    }),
  trashJob: (id: string) => call<Job>(`/api/jobs/${id}/trash`, { method: 'POST' }),
  restoreJob: (id: string) => call<Job>(`/api/jobs/${id}/restore`, { method: 'POST' }),
  trashSequence: (id: string) => call<Sequence>(`/api/sequences/${id}/trash`, { method: 'POST' }),
  restoreSequence: (id: string) => call<Sequence>(`/api/sequences/${id}/restore`, { method: 'POST' }),
  trash: () => call<Trash>('/api/trash'),
  emptyTrash: () => call<{ removed: number; kept: number }>('/api/trash', { method: 'DELETE' }),
  tools: () => call<{ tools: HelperTool[]; chat_unavailable: string | null }>('/api/tools'),
  callTool: (name: string, args: Record<string, unknown>) =>
    call<ChatReply>(`/api/tools/${name}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(args),
    }),
  chat: (message: string, selection?: Selection) =>
    call<ChatReply>('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, selection }),
    }),
  sequences: () => call<Sequence[]>('/api/sequences?limit=30'),
  createSequence: (body: { shots: ShotIn[]; segments: SegmentIn[]; story?: string }) =>
    call<Sequence>('/api/sequences', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  shots: () => call<{ shots: GeneratedShot[]; unavailable: string | null; models: ImageModel[]; orbit_unavailable: string | null }>('/api/shots'),
  orbitShotImage: (id: string, name: string) => call<GeneratedShot>(`/api/shots/${id}/images/${name}/orbit`, { method: 'POST' }),
  cancelOrbit: (id: string) => call<GeneratedShot>(`/api/orbits/${id}/cancel`, { method: 'POST' }),
  createShot: (body: { prompt: string; aspect: string; quality: string; count: number; seed?: number; model: string }) =>
    call<GeneratedShot>('/api/shots', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  shotVariation: (id: string) => call<GeneratedShot>(`/api/shots/${id}/variations`, { method: 'POST' }),
  cancelShot: (id: string) => call<GeneratedShot>(`/api/shots/${id}/cancel`, { method: 'POST' }),
  dismissShot: (id: string) => call<void>(`/api/shots/${id}`, { method: 'DELETE' }),
  editShotImage: (id: string, name: string, body: { starred?: boolean; trashed?: boolean }) =>
    call<GeneratedShot>(`/api/shots/${id}/images/${name}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  useShotImage: (id: string, name: string, frame?: number) =>
    call<Upload>(`/api/shots/${id}/images/${name}/use${frame === undefined ? '' : `?frame=${frame}`}`, { method: 'POST' }),
  purgeShots: () => call<{ removed: number }>('/api/shots/purge', { method: 'POST' }),
  roughCut: (body: { shots: ShotIn[]; segments: SegmentIn[] }) =>
    call<{ url: string; seconds: number }>('/api/rough-cut', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  /** `segment` is 1-based, as the server and the agent tools count. */
  refineSegment: (id: string, segment: number, body: RefineBody = {}) =>
    call<Sequence>(`/api/sequences/${id}/segments/${segment}/refine`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  /** Remove / reorder shots and set the story in one step (1-based shot and transition numbers). */
  editSequence: (id: string, body: EditIn) =>
    call<Sequence>(`/api/sequences/${id}/edit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  /** Grow a finished sequence by one shot, or turn a finished two-shot take into a sequence with one more shot. */
  extendSequence: (id: string, body: { shot: ShotIn; segment: SegmentIn }) =>
    call<Sequence>(`/api/sequences/${id}/shots`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  continueTake: (jobId: string, body: { shot: ShotIn; segment: SegmentIn }) =>
    call<Sequence>(`/api/jobs/${jobId}/continue`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  cancelSequence: (id: string) => call<Sequence>(`/api/sequences/${id}/cancel`, { method: 'POST' }),
  assembleSequence: (id: string) => call<Sequence>(`/api/sequences/${id}/assemble`, { method: 'POST' }),
  estimate: (seconds = 5, model?: string) => call<Estimate>(`/api/estimate?seconds=${seconds}${model ? `&model=${model}` : ''}`),
  models: (refresh = false) => call<{ backend: BackendName; models: ModelRow[] }>(`/api/models${refresh ? '?refresh=true' : ''}`),
  examples: () => call<Example[]>('/api/examples'),
  loadExample: (id: string) => call<{ a: Upload; b: Upload; preset: string }>(`/api/examples/${id}`, { method: 'POST' }),
}

/** How a take was made (JSON) and the same take as a ComfyUI workflow bundle (zip). */
export const receiptUrl = (id: string) => `/api/jobs/${id}/receipt`
export const comfyBundleUrl = (id: string) => `/api/jobs/${id}/comfyui.zip`
export const sequenceReceiptUrl = (id: string) => `/api/sequences/${id}/receipt`

type Listeners = { job?: (job: Job) => void; sequence?: (seq: Sequence) => void; shot?: (shot: GeneratedShot) => void; download?: (d: DownloadState) => void; open?: () => void }

/** Live job and sequence updates over server-sent events; `open` fires on connect and on every reconnect (updates
 *  sent while disconnected are lost, so fetch the current state then). Returns an unsubscribe function. */
export function subscribe(on: Listeners): () => void {
  const es = new EventSource('/api/events')
  es.addEventListener('open', () => on.open?.())
  if (on.job) es.addEventListener('job', (e) => on.job!(JSON.parse((e as MessageEvent).data) as Job))
  if (on.shot) es.addEventListener('shot', (e) => on.shot!(JSON.parse((e as MessageEvent).data) as GeneratedShot))
  if (on.sequence) es.addEventListener('sequence', (e) => on.sequence!(JSON.parse((e as MessageEvent).data) as Sequence))
  if (on.download) es.addEventListener('download', (e) => on.download!(JSON.parse((e as MessageEvent).data) as DownloadState))
  return () => es.close()
}

export const uploadUrl = (id: string) => `/uploads/${id}`
