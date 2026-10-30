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

/** Shots joined by one transition per hand-off; `segments` holds the current take (job id) of each. */
export type Sequence = {
  id: string
  status: SequenceStatus
  error: string | null
  width: number
  height: number
  created_at: number
  updated_at: number
  shots: { first: string; last: string; first_url: string; last_url: string; clip?: string; clip_url?: string }[]
  segments: string[]
  output_url?: string
  poster_url?: string
  sheet_url?: string
  /** the idea of the whole sequence; each take keeps the story it was made with (params.story) */
  story?: string
  /** REST only (live events omit it): the current takes, in segment order */
  takes?: (Job | null)[]
}

export type SegmentIn = { preset: string; seconds?: number; seed?: number; prompt_extra?: string }
export type ShotIn = { image: string } | { clip: string; first: string; last: string }
/** order: current shot numbers in the new order; transitions: number in the NEW order → its effect (new hand-offs) */
export type EditIn = { order: number[]; transitions?: Record<string, SegmentIn>; story?: string; expected_updated_at?: number }

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'

export type Job = {
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
  extra: { width?: number; height?: number; duration?: number; prompt?: string; sequence_id?: string; segment?: number }
}

/** What Seam sees in a shot: a description and a short label, both used in the prompt. */
export type ShotText = { desc: string; ref: string }

/** Omit a side to let the server read the shot itself; send '' for a generic prompt. */
export type ShotFields = { a_desc?: string; b_desc?: string; a_ref?: string; b_ref?: string }

export type Health = { ok: boolean; model: string; comfy_base_url: string; presets: number }

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
  return r.json() as Promise<T>
}

export const api = {
  health: () => call<Health>('/api/health'),
  presets: () => call<Preset[]>('/api/presets'),
  jobs: () => call<Job[]>('/api/jobs?limit=60'),
  upload: (file: File, role: 'a' | 'b' | 'mid') => {
    const body = new FormData()
    body.append('file', file)
    return call<Upload>(`/api/uploads?role=${role}`, { method: 'POST', body })
  },
  describe: (uploadId: string) => call<ShotText>(`/api/describe/${uploadId}`, { method: 'POST' }),
  create: (
    body: { image_a: string; image_b: string; preset: string; seconds?: number; seed?: number; prompt_extra?: string; clip_a?: string; clip_b?: string } & ShotFields,
  ) =>
    call<Job>('/api/transitions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  refine: (id: string, body: { preset?: string; seconds?: number; seed?: number; prompt_extra?: string }) =>
    call<Job>(`/api/jobs/${id}/refine`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  cancel: (id: string) => call<Job>(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  sequences: () => call<Sequence[]>('/api/sequences?limit=30'),
  createSequence: (body: { shots: ShotIn[]; segments: SegmentIn[]; story?: string }) =>
    call<Sequence>('/api/sequences', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  /** `segment` is 1-based, as the server and the agent tools count. */
  refineSegment: (id: string, segment: number, body: { preset?: string; seconds?: number; prompt_extra?: string; seed?: number } = {}) =>
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
  estimate: (seconds = 5) => call<Estimate>(`/api/estimate?seconds=${seconds}`),
  examples: () => call<Example[]>('/api/examples'),
  loadExample: (id: string) => call<{ a: Upload; b: Upload; preset: string }>(`/api/examples/${id}`, { method: 'POST' }),
}

/** How a take was made (JSON) and the same take as a ComfyUI workflow bundle (zip). */
export const receiptUrl = (id: string) => `/api/jobs/${id}/receipt`
export const comfyBundleUrl = (id: string) => `/api/jobs/${id}/comfyui.zip`
export const sequenceReceiptUrl = (id: string) => `/api/sequences/${id}/receipt`

type Listeners = { job?: (job: Job) => void; sequence?: (seq: Sequence) => void; open?: () => void }

/** Live job and sequence updates over server-sent events; `open` fires on connect and on every reconnect (updates
 *  sent while disconnected are lost, so fetch the current state then). Returns an unsubscribe function. */
export function subscribe(on: Listeners): () => void {
  const es = new EventSource('/api/events')
  es.addEventListener('open', () => on.open?.())
  if (on.job) es.addEventListener('job', (e) => on.job!(JSON.parse((e as MessageEvent).data) as Job))
  if (on.sequence) es.addEventListener('sequence', (e) => on.sequence!(JSON.parse((e as MessageEvent).data) as Sequence))
  return () => es.close()
}

export const uploadUrl = (id: string) => `/uploads/${id}`
