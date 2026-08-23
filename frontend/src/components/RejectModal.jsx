import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

const KIND_LABEL = {
  apply_domain: 'Block domain',
  company: 'Block company',
  title_pattern: 'Block title phrase',
  source: 'Block source',
  required_domain: 'Block must-have domain',
  work_authorization: 'Block work-auth region',
  job: 'Block this job only',
}

function confidenceTone(confidence) {
  if (confidence === 'high') return 'text-emerald-400'
  if (confidence === 'low') return 'text-amber-400'
  return 'text-neutral-400'
}

/**
 * Reject flow. The reason is free text; an LLM maps it onto the existing rule
 * dimensions and we show the blast radius so the human confirms with the damage
 * visible. Nothing is written until Confirm.
 */
export default function RejectModal({ job, onClose, onDone }) {
  const [reason, setReason] = useState('')
  const [proposals, setProposals] = useState(null)
  const [picked, setPicked] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const inputRef = useRef(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  useEffect(() => {
    function onKey(event) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function analyze() {
    if (!reason.trim()) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await api.propose(job.job_slug, reason)
      setProposals(result.proposals)
      // Pre-select confident, non-duplicate proposals; the human can uncheck.
      const next = {}
      result.proposals.forEach((proposal, index) => {
        next[index] = !proposal.already_exists && proposal.confidence !== 'low'
      })
      setPicked(next)
      if (!result.llm_used) {
        setNotice(
          result.error
            ? `No LLM available, used heuristic fallback. (${result.error})`
            : 'Used heuristic fallback.',
        )
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function confirm(withRules) {
    setBusy(true)
    setError('')
    try {
      const rules =
        withRules && proposals
          ? proposals
              .filter((_, index) => picked[index])
              .map((proposal) => ({
                kind: proposal.kind,
                value: proposal.value,
                reason: proposal.reason || reason,
              }))
          : []
      const result = await api.reject(job.job_slug, reason, rules)
      onDone(result)
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  const selectedCount = proposals ? Object.values(picked).filter(Boolean).length : 0

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 sm:items-center"
      onClick={(event) => event.target === event.currentTarget && onClose()}
    >
      <div className="w-full max-w-2xl rounded-lg border border-neutral-800 bg-neutral-900 shadow-2xl">
        <div className="border-b border-neutral-800 px-5 py-3">
          <h2 className="text-sm font-semibold text-neutral-100">Reject job</h2>
          <p className="mt-0.5 truncate text-xs text-neutral-500">
            {job.company} - {job.title}
          </p>
        </div>

        <div className="space-y-4 px-5 py-4">
          <div>
            <label className="mb-1.5 block text-xs font-medium text-neutral-400">
              Why is this a bad fit? Plain English.
            </label>
            <textarea
              ref={inputRef}
              rows={2}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) analyze()
              }}
              placeholder="e.g. this site just reposts other boards, it's not the employer"
              className="w-full resize-none rounded border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-accent focus:outline-none"
            />
            <div className="mt-1 text-[11px] text-neutral-600">
              <kbd className="rounded bg-neutral-800 px-1">⌘↵</kbd> to analyze
            </div>
          </div>

          {!proposals && (
            <button
              onClick={analyze}
              disabled={busy || !reason.trim()}
              className="w-full rounded bg-accent px-3 py-2 text-sm font-medium text-neutral-950 hover:bg-accent-dark disabled:opacity-40"
            >
              {busy ? 'Analyzing…' : 'Analyze reason'}
            </button>
          )}

          {notice && (
            <div className="rounded border border-amber-800/60 bg-amber-950/40 px-3 py-2 text-xs text-amber-300">
              {notice}
            </div>
          )}

          {proposals && (
            <div className="space-y-2">
              <div className="text-xs font-medium text-neutral-400">
                Proposed rules - uncheck anything you don't want
              </div>
              {proposals.map((proposal, index) => (
                <label
                  key={index}
                  className="flex cursor-pointer items-start gap-3 rounded border border-neutral-800 bg-neutral-950 px-3 py-2.5 hover:border-neutral-700"
                >
                  <input
                    type="checkbox"
                    checked={!!picked[index]}
                    onChange={(event) =>
                      setPicked({ ...picked, [index]: event.target.checked })
                    }
                    className="mt-0.5 h-4 w-4 accent-accent"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="text-xs font-medium text-neutral-300">
                        {KIND_LABEL[proposal.kind] || proposal.kind}
                      </span>
                      <span className="mono truncate rounded bg-neutral-800 px-1.5 py-0.5 text-[11px] text-accent">
                        {proposal.value}
                      </span>
                      <span className={`text-[10px] ${confidenceTone(proposal.confidence)}`}>
                        {proposal.confidence}
                      </span>
                    </span>
                    <span className="mt-1 block text-[11px] text-neutral-500">
                      {proposal.explanation}
                    </span>
                    <span className="mt-1 block text-[11px]">
                      {proposal.already_exists ? (
                        <span className="text-neutral-500">Rule already exists.</span>
                      ) : (
                        <span
                          className={
                            proposal.blast_radius > 40 ? 'text-amber-400' : 'text-neutral-400'
                          }
                        >
                          Blocks {proposal.blast_radius} stored job
                          {proposal.blast_radius === 1 ? '' : 's'} + all future runs
                          {proposal.blast_radius > 40 ? ' - that is a lot, check it' : ''}
                        </span>
                      )}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          )}

          {error && (
            <div className="rounded border border-red-800/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
              {error}
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-neutral-800 px-5 py-3">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded px-3 py-1.5 text-xs text-neutral-400 hover:bg-neutral-800 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={() => confirm(false)}
            disabled={busy || !reason.trim()}
            className="rounded border border-neutral-700 px-3 py-1.5 text-xs text-neutral-300 hover:bg-neutral-800 disabled:opacity-40"
          >
            Reject without rule
          </button>
          {proposals && (
            <button
              onClick={() => confirm(true)}
              disabled={busy}
              className="rounded bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-40"
            >
              {busy
                ? 'Working…'
                : `Confirm & reject${selectedCount ? ` (+${selectedCount} rule${selectedCount === 1 ? '' : 's'})` : ''}`}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
