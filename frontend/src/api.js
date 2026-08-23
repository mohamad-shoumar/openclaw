// Thin fetch wrapper. Vite proxies /api to the FastAPI server in dev.

async function request(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    headers: { 'content-type': 'application/json' },
    ...options,
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      detail = body.detail || detail
    } catch {
      // non-JSON error body; keep the status text
    }
    throw new Error(detail)
  }
  return response.json()
}

export const api = {
  stats: () => request('/stats'),
  jobs: (status) => request(`/jobs?status=${encodeURIComponent(status)}`),
  job: (slug) => request(`/jobs/${encodeURIComponent(slug)}`),

  approve: (slug, reason = '', note = '') =>
    request(`/jobs/${encodeURIComponent(slug)}/approve`, {
      method: 'POST',
      body: JSON.stringify({ reason, note }),
    }),

  reject: (slug, reason, rules = [], note = '') =>
    request(`/jobs/${encodeURIComponent(slug)}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason, rules, note }),
    }),

  propose: (slug, reason) =>
    request(`/jobs/${encodeURIComponent(slug)}/propose`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),

  setApplied: (slug, applied, notes = '') =>
    request(`/jobs/${encodeURIComponent(slug)}/applied`, {
      method: 'POST',
      body: JSON.stringify({ applied, notes }),
    }),

  requeue: (slug) =>
    request(`/jobs/${encodeURIComponent(slug)}/requeue`, { method: 'POST' }),

  rules: () => request('/feedback'),
  addRule: (kind, value, reason) =>
    request('/feedback', { method: 'POST', body: JSON.stringify({ kind, value, reason }) }),
  deleteRule: (id) => request(`/feedback/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  startRun: () => request('/runs', { method: 'POST' }),
  currentRun: () => request('/runs/current'),

  artifacts: (slug) => request(`/jobs/${encodeURIComponent(slug)}/artifacts`),
  buildArtifacts: (slug) =>
    request(`/jobs/${encodeURIComponent(slug)}/artifacts`, { method: 'POST' }),
}
