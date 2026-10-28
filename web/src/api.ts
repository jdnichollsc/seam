export type Preset = {
  id: string
  name: string
  category: string
  camera: string
  model?: string
  preview_url?: string
  poster_url?: string
}

/** An image, or a clip: then `id` is the frame the transition uses and `clip` the video around it. */
export type Upload = { id: string; url: string; width: number; height: number; clip?: string; clip_url?: string; clip_seconds?: number }

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'

export type Job = {
  id: string
  status: JobStatus
  progress: number
  parent_id: string | null
  error: string | null
  wall_s: number | null
  created_at: number
  params: {
    image_a: string
    image_b: string
    preset: string
    seed: number
    model?: string
    prompt_extra?: string
    a_desc?: string | null
    b_desc?: string | null
    a_ref?: string | null
    b_ref?: string | null
    clip_a?: string | null
    clip_b?: string | null
  }
  output_url?: string
  merged_url?: string
  poster_url?: string
  sheet_url?: string
  extra: { width?: number; height?: number; duration?: number; prompt?: string }
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
  upload: (file: File, role: 'a' | 'b') => {
    const body = new FormData()
    body.append('file', file)
    return call<Upload>(`/api/uploads?role=${role}`, { method: 'POST', body })
  },
  describe: (uploadId: string) => call<ShotText>(`/api/describe/${uploadId}`, { method: 'POST' }),
  create: (
    body: { image_a: string; image_b: string; preset: string; seed?: number; prompt_extra?: string; clip_a?: string; clip_b?: string } & ShotFields,
  ) =>
    call<Job>('/api/transitions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  refine: (id: string, body: { preset?: string; seed?: number; prompt_extra?: string }) =>
    call<Job>(`/api/jobs/${id}/refine`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  cancel: (id: string) => call<Job>(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  estimate: () => call<Estimate>('/api/estimate'),
  examples: () => call<Example[]>('/api/examples'),
  loadExample: (id: string) => call<{ a: Upload; b: Upload; preset: string }>(`/api/examples/${id}`, { method: 'POST' }),
}

/** How a take was made (JSON) and the same take as a ComfyUI workflow bundle (zip). */
export const receiptUrl = (id: string) => `/api/jobs/${id}/receipt`
export const comfyBundleUrl = (id: string) => `/api/jobs/${id}/comfyui.zip`

/** Live job updates over server-sent events; returns an unsubscribe function. */
export function subscribe(onJob: (job: Job) => void): () => void {
  const es = new EventSource('/api/events')
  es.addEventListener('job', (e) => onJob(JSON.parse((e as MessageEvent).data) as Job))
  return () => es.close()
}

export const uploadUrl = (id: string) => `/uploads/${id}`
