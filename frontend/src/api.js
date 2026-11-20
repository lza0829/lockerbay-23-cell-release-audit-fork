async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  bay: () => req('/api/bay'),
  occupancies: () => req('/api/occupancies'),
  events: () => req('/api/events'),
  quarantine: () => req('/api/quarantine'),
  proofs: () => req('/api/occupancy-proofs'),
  settings: () => req('/api/settings'),
  setOverdue: (overdue_hours) =>
    req('/api/settings/overdue-hours', { method: 'PUT', body: JSON.stringify({ overdue_hours }) }),
  ingest: () => req('/api/ingest/run', { method: 'POST', body: '{}' }),
  reclaimCandidates: (as_of) =>
    req(`/api/reclaim/candidates${as_of ? `?as_of=${encodeURIComponent(as_of)}` : ''}`),
  reclaimScan: (body = {}) => req('/api/reclaim/scan', { method: 'POST', body: JSON.stringify(body) }),
  reclaimOne: (id, body = {}) =>
    req(`/api/reclaim/occupancies/${encodeURIComponent(id)}`, { method: 'POST', body: JSON.stringify(body) }),
  reclaimEdges: () => req('/api/reclaim/edges'),
  reclaimTrace: (id) => req(`/api/occupancies/${encodeURIComponent(id)}/reclaim-trace`),
  loadReclaimFixture: (name) => req(`/api/reclaim/fixtures/${name}`, { method: 'POST', body: '{}' }),
  cellReleaseAudits: () => req('/api/cell-release-audits'),
}
