import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import JobDetail from './components/JobDetail'
import JobList from './components/JobList'
import RejectModal from './components/RejectModal'
import RulesPanel from './components/RulesPanel'

const TABS = [
  { key: 'pending_review', label: 'Pending' },
  { key: 'approved', label: 'Approved' },
  { key: 'rejected', label: 'Rejected' },
]

export default function App() {
  const [tab, setTab] = useState('pending_review')
  const [jobs, setJobs] = useState([])
  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [toast, setToast] = useState('')
  const [showRules, setShowRules] = useState(false)
  const [showReject, setShowReject] = useState(false)
  const [run, setRun] = useState(null)
  const pollRef = useRef(null)

  const flash = useCallback((message) => {
    setToast(message)
    setTimeout(() => setToast(''), 5000)
  }, [])

  const loadStats = useCallback(async () => {
    try {
      setStats(await api.stats())
    } catch {
      // stats are decoration; a failure here must not blank the queue
    }
  }, [])

  const loadJobs = useCallback(
    async (status, keepSelection) => {
      setLoading(true)
      setError('')
      try {
        const result = await api.jobs(status)
        setJobs(result.jobs)
        setSelected((current) => {
          if (keepSelection && result.jobs.some((job) => job.job_slug === current)) return current
          return result.jobs[0]?.job_slug ?? null
        })
      } catch (err) {
        setError(err.message)
        setJobs([])
      } finally {
        setLoading(false)
      }
    },
    [],
  )

  useEffect(() => {
    loadJobs(tab, false)
    loadStats()
  }, [tab, loadJobs, loadStats])

  useEffect(() => {
    if (!selected) {
      setDetail(null)
      return
    }
    let cancelled = false
    api
      .job(selected)
      .then((data) => !cancelled && setDetail(data))
      .catch((err) => !cancelled && setError(err.message))
    return () => {
      cancelled = true
    }
  }, [selected])

  const refreshAll = useCallback(async () => {
    await Promise.all([loadJobs(tab, true), loadStats()])
    if (selected) {
      try {
        setDetail(await api.job(selected))
      } catch {
        // job may have moved out of this tab
      }
    }
  }, [tab, selected, loadJobs, loadStats])

  // --- pipeline run polling -------------------------------------------------

  const pollRun = useCallback(() => {
    clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const state = await api.currentRun()
        setRun(state)
        if (state.status !== 'running') {
          clearInterval(pollRef.current)
          if (state.status === 'done') {
            flash(
              `Run finished: ${state.summary?.accepted_jobs ?? 0} accepted of ${
                state.summary?.total_unique_jobs ?? 0
              } unique jobs.`,
            )
          } else if (state.status === 'error') {
            setError(state.error)
          }
          refreshAll()
        }
      } catch {
        clearInterval(pollRef.current)
      }
    }, 2000)
  }, [flash, refreshAll])

  useEffect(() => {
    // Recover the banner if a run was already going when the page loaded.
    api.currentRun().then((state) => {
      setRun(state)
      if (state.status === 'running') pollRun()
    }).catch(() => {})
    return () => clearInterval(pollRef.current)
  }, [pollRun])

  async function startRun() {
    setError('')
    try {
      setRun(await api.startRun())
      pollRun()
    } catch (err) {
      setError(err.message)
    }
  }

  // --- actions -------------------------------------------------------------

  const moveSelection = useCallback(
    (delta) => {
      setSelected((current) => {
        const index = jobs.findIndex((job) => job.job_slug === current)
        const next = Math.min(Math.max((index === -1 ? 0 : index) + delta, 0), jobs.length - 1)
        return jobs[next]?.job_slug ?? current
      })
    },
    [jobs],
  )

  const approve = useCallback(async () => {
    if (!detail || detail.review_status !== 'pending_review') return
    setBusy(true)
    try {
      await api.approve(detail.job_slug)
      flash(`Approved ${detail.company}. Generate artifacts from the Artifacts tab.`)
      await refreshAll()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }, [detail, flash, refreshAll])

  async function onRejected(result) {
    setShowReject(false)
    setBusy(false)
    const added = result.rules_added?.length || 0
    const extra = result.also_revoked?.length || 0
    flash(
      added
        ? `Rejected. Added ${added} rule${added === 1 ? '' : 's'}${
            extra ? `, which also revoked ${extra} other queued job${extra === 1 ? '' : 's'}` : ''
          }.`
        : 'Rejected.',
    )
    await refreshAll()
  }

  // --- keyboard ------------------------------------------------------------

  useEffect(() => {
    function onKey(event) {
      if (showReject || showRules) return
      const tag = event.target.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if (event.metaKey || event.ctrlKey || event.altKey) return

      if (event.key === 'j') { event.preventDefault(); moveSelection(1) }
      else if (event.key === 'k') { event.preventDefault(); moveSelection(-1) }
      else if (event.key === 'a') { event.preventDefault(); approve() }
      else if (event.key === 'r') {
        event.preventDefault()
        if (detail?.review_status === 'pending_review') setShowReject(true)
      } else if (event.key === 'o') {
        event.preventDefault()
        const url = detail?.apply_url || detail?.job_url
        if (url) window.open(url, '_blank', 'noreferrer')
      } else if (event.key === '?') {
        event.preventDefault()
        setShowRules(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [moveSelection, approve, detail, showReject, showRules])

  const pendingCount = stats?.by_review_status?.pending_review ?? 0

  return (
    <div className="flex h-screen flex-col">
      {/* top bar */}
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-neutral-800 px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm font-semibold text-neutral-100">OpenClaw Review</span>
          <span className="rounded bg-accent px-1.5 py-0.5 text-[11px] font-medium text-neutral-950">
            {pendingCount} pending
          </span>
          {stats?.applied > 0 && (
            <span className="rounded bg-emerald-900/60 px-1.5 py-0.5 text-[11px] text-emerald-300">
              {stats.applied} applied
            </span>
          )}
          {stats?.last_run && (
            <span className="text-[11px] text-neutral-600">
              last run {stats.last_run.run_id}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="hidden text-[11px] text-neutral-600 sm:inline">
            <kbd className="rounded bg-neutral-800 px-1">j</kbd>
            <kbd className="ml-0.5 rounded bg-neutral-800 px-1">k</kbd> move
            <kbd className="ml-1.5 rounded bg-neutral-800 px-1">a</kbd> approve
            <kbd className="ml-0.5 rounded bg-neutral-800 px-1">r</kbd> reject
            <kbd className="ml-0.5 rounded bg-neutral-800 px-1">o</kbd> open
          </span>
          <button
            onClick={() => setShowRules(true)}
            className="rounded border border-neutral-700 px-2.5 py-1 text-xs text-neutral-300 hover:bg-neutral-800"
          >
            Rules {stats?.rules ? `(${stats.rules})` : ''}
          </button>
          <button
            onClick={startRun}
            disabled={run?.status === 'running'}
            className="rounded bg-accent px-2.5 py-1 text-xs font-medium text-neutral-950 hover:bg-accent-dark disabled:opacity-50"
          >
            {run?.status === 'running' ? 'Fetching…' : 'Refresh jobs'}
          </button>
        </div>
      </header>

      {run?.status === 'running' && (
        <div className="border-b border-accent/30 bg-accent/10 px-4 py-1.5 text-xs text-accent">
          Fetching every configured source. This takes a few minutes - you can keep reviewing.
        </div>
      )}
      {toast && (
        <div className="border-b border-emerald-800/50 bg-emerald-950/40 px-4 py-1.5 text-xs text-emerald-300">
          {toast}
        </div>
      )}
      {error && (
        <div className="flex items-center justify-between border-b border-red-800/50 bg-red-950/40 px-4 py-1.5 text-xs text-red-300">
          <span>{error}</span>
          <button onClick={() => setError('')} className="text-red-400 hover:text-red-200">
            dismiss
          </button>
        </div>
      )}

      {/* two panes */}
      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
        <div className="flex min-h-0 flex-col border-b border-neutral-800 lg:border-b-0 lg:border-r">
          <div className="flex gap-1 border-b border-neutral-800 px-3 py-2">
            {TABS.map((entry) => (
              <button
                key={entry.key}
                onClick={() => setTab(entry.key)}
                className={`rounded px-2 py-1 text-xs ${
                  tab === entry.key
                    ? 'bg-neutral-800 text-neutral-100'
                    : 'text-neutral-500 hover:text-neutral-300'
                }`}
              >
                {entry.label}{' '}
                <span className="opacity-60">{stats?.by_review_status?.[entry.key] ?? 0}</span>
              </button>
            ))}
          </div>
          <div className="pane max-h-[38vh] flex-1 lg:max-h-none">
            <JobList jobs={jobs} selected={selected} onSelect={setSelected} loading={loading} />
          </div>
        </div>

        <div className="min-h-0">
          <JobDetail
            job={detail}
            busy={busy}
            onApprove={approve}
            onReject={() => setShowReject(true)}
            onChanged={refreshAll}
          />
        </div>
      </div>

      {showReject && detail && (
        <RejectModal job={detail} onClose={() => setShowReject(false)} onDone={onRejected} />
      )}
      {showRules && (
        <RulesPanel onClose={() => setShowRules(false)} onChanged={refreshAll} />
      )}
    </div>
  )
}
