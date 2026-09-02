import { useEffect, useState } from 'react'
import { api } from '../api'

function Chip({ children, tone = 'neutral' }) {
  const tones = {
    neutral: 'bg-neutral-800 text-neutral-300',
    good: 'bg-emerald-900/60 text-emerald-300',
    warn: 'bg-amber-900/60 text-amber-300',
    bad: 'bg-red-900/60 text-red-300',
    accent: 'bg-accent/20 text-accent',
  }
  return <span className={`rounded px-2 py-0.5 text-[11px] ${tones[tone]}`}>{children}</span>
}

// "5+ years" and "5 - 5 years" are different requirements. The closed-range
// render hid the open-ended ones, so it read as an exact match to the ceiling.
function experienceLabel(job) {
  const min = job.experience_required_min
  const max = job.experience_required_max
  if (min == null && max == null) return 'not stated'
  if (job.experience_open_ended) return `${min}+ years`
  if (max != null && min != null && max !== min) return `${min}-${max} years`
  return `${min ?? max} years`
}

function scopeTone(scope) {
  if (scope === 'global') return 'good'
  if (scope === 'open') return 'accent'
  if (scope === 'restricted') return 'bad'
  return 'neutral'
}

export default function JobDetail({ job, onApprove, onReject, onChanged, busy }) {
  const [tab, setTab] = useState('description')
  const [artifacts, setArtifacts] = useState(null)
  const [artifactBusy, setArtifactBusy] = useState(false)
  const [artifactError, setArtifactError] = useState('')

  // Reset the pane whenever a different job is selected.
  useEffect(() => {
    setTab('description')
    setArtifacts(null)
    setArtifactError('')
  }, [job?.job_slug])

  async function loadArtifacts() {
    setArtifactBusy(true)
    setArtifactError('')
    try {
      setArtifacts(await api.artifacts(job.job_slug))
    } catch (err) {
      setArtifactError(err.message)
    } finally {
      setArtifactBusy(false)
    }
  }

  async function generate() {
    setArtifactBusy(true)
    setArtifactError('')
    try {
      await api.buildArtifacts(job.job_slug)
      setArtifacts(await api.artifacts(job.job_slug))
      onChanged?.()
    } catch (err) {
      setArtifactError(err.message)
    } finally {
      setArtifactBusy(false)
    }
  }

  async function toggleApplied() {
    await api.setApplied(job.job_slug, !job.applied_at)
    onChanged?.()
  }

  if (!job) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-neutral-600">
        Select a job
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-neutral-800 px-5 py-4">
        <h1 className="text-lg font-semibold leading-snug text-neutral-100">{job.title}</h1>
        <p className="mt-0.5 text-sm text-neutral-400">{job.company}</p>

        <div className="mt-3 flex flex-wrap gap-1.5">
          <Chip tone={scopeTone(job.remote_scope)}>remote: {job.remote_scope}</Chip>
          {job.posted_at ? <Chip>posted {job.posted_at}</Chip> : <Chip tone="warn">no date</Chip>}
          {job.location_raw && <Chip>{job.location_raw}</Chip>}
          <Chip>{job.discovery_source}</Chip>
          {job.seniority_title !== 'unknown' && <Chip>{job.seniority_title}</Chip>}
          {job.applied_at && <Chip tone="good">applied</Chip>}
        </div>

        {job.is_aggregator && (
          <div className="mt-3 rounded border border-amber-800/60 bg-amber-950/40 px-3 py-2 text-xs text-amber-300">
            <strong>Aggregator domain</strong> ({job.apply_host}). The apply URL is not the
            employer's own site - verify the posting is real before applying.
          </div>
        )}

        {job.country_restrictions?.length > 0 && (
          <div className="mt-2 rounded border border-red-900/60 bg-red-950/30 px-3 py-2 text-xs text-red-300">
            <strong>Location restrictions detected:</strong>{' '}
            {job.country_restrictions.join('; ')}
          </div>
        )}

        <div className="mt-4 flex flex-wrap gap-2">
          <a
            href={job.apply_url || job.job_url}
            target="_blank"
            rel="noreferrer"
            className="rounded bg-accent px-3 py-1.5 text-xs font-semibold text-neutral-950 hover:bg-accent-dark"
          >
            Open posting ↗ <span className="opacity-60">o</span>
          </a>
          {job.review_status === 'pending_review' && (
            <>
              <button
                onClick={onApprove}
                disabled={busy}
                className="rounded bg-emerald-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-600 disabled:opacity-40"
              >
                Approve <span className="opacity-60">a</span>
              </button>
              <button
                onClick={onReject}
                disabled={busy}
                className="rounded border border-red-800 px-3 py-1.5 text-xs font-medium text-red-300 hover:bg-red-950 disabled:opacity-40"
              >
                Reject <span className="opacity-60">r</span>
              </button>
            </>
          )}
          {job.review_status === 'approved' && (
            <button
              onClick={toggleApplied}
              className={`rounded px-3 py-1.5 text-xs font-medium ${
                job.applied_at
                  ? 'border border-neutral-700 text-neutral-300 hover:bg-neutral-800'
                  : 'bg-emerald-700 text-white hover:bg-emerald-600'
              }`}
            >
              {job.applied_at ? 'Mark not applied' : 'Mark as applied'}
            </button>
          )}
          {job.review_status !== 'pending_review' && (
            <button
              onClick={async () => {
                await api.requeue(job.job_slug)
                onChanged?.()
              }}
              className="rounded border border-neutral-700 px-3 py-1.5 text-xs text-neutral-300 hover:bg-neutral-800"
            >
              Back to queue
            </button>
          )}
        </div>

        {job.approval_reason && (
          <p className="mt-3 text-xs text-neutral-500">
            <span className="text-neutral-400">Decision:</span> {job.approval_reason}
          </p>
        )}
      </div>

      <div className="flex gap-1 border-b border-neutral-800 px-4 py-2">
        {['description', 'signals', 'artifacts'].map((name) => (
          <button
            key={name}
            onClick={() => {
              setTab(name)
              if (name === 'artifacts' && !artifacts) loadArtifacts()
            }}
            className={`rounded px-2.5 py-1 text-xs capitalize ${
              tab === name
                ? 'bg-neutral-800 text-neutral-100'
                : 'text-neutral-500 hover:text-neutral-300'
            }`}
          >
            {name}
          </button>
        ))}
      </div>

      <div className="pane flex-1 px-5 py-4">
        {tab === 'description' && (
          <div className="desc text-[13px] leading-relaxed text-neutral-300">
            {job.description_text || <span className="text-neutral-600">No description captured.</span>}
          </div>
        )}

        {tab === 'signals' && (
          <div className="space-y-4 text-xs">
            <section>
              <h3 className="mb-1.5 font-medium text-neutral-300">Matched tech</h3>
              <div className="flex flex-wrap gap-1">
                {job.required_tech?.map((t) => (
                  <Chip key={t} tone="good">{t}</Chip>
                ))}
                {job.preferred_tech?.map((t) => (
                  <Chip key={t}>{t}</Chip>
                ))}
                {!job.required_tech?.length && !job.preferred_tech?.length && (
                  <span className="text-neutral-600">none extracted</span>
                )}
              </div>
            </section>

            <section>
              <h3 className="mb-1.5 font-medium text-neutral-300">Experience</h3>
              <p className="text-neutral-400">{experienceLabel(job)}</p>
            </section>

            {job.evidence_snippets?.length > 0 && (
              <section>
                <h3 className="mb-1.5 font-medium text-neutral-300">Why it was classified this way</h3>
                <ul className="space-y-1">
                  {job.evidence_snippets.map((snippet, index) => (
                    <li key={index} className="rounded bg-neutral-900 px-2.5 py-1.5">
                      <span className="mono text-accent">{snippet.field}</span>
                      <span className="ml-2 text-neutral-400">{snippet.snippet}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {job.rejection_reasons?.length > 0 && (
              <section>
                <h3 className="mb-1.5 font-medium text-neutral-300">Rejection reasons</h3>
                <ul className="list-inside list-disc space-y-0.5 text-red-300/80">
                  {job.rejection_reasons.map((reason, index) => (
                    <li key={index}>{reason}</li>
                  ))}
                </ul>
              </section>
            )}

            <section>
              <h3 className="mb-1.5 font-medium text-neutral-300">Apply URL</h3>
              <p className="mono break-all text-neutral-500">{job.apply_url || job.job_url}</p>
            </section>
          </div>
        )}

        {tab === 'artifacts' && (
          <div className="space-y-3 text-xs">
            {artifactBusy && <p className="text-neutral-500">Working…</p>}
            {artifactError && (
              <div className="rounded border border-red-800/60 bg-red-950/40 px-3 py-2 text-red-300">
                {artifactError}
              </div>
            )}

            {job.review_status !== 'approved' ? (
              <p className="text-neutral-500">
                Approve the job first - artifacts are only generated for approved roles.
              </p>
            ) : (
              <>
                <button
                  onClick={generate}
                  disabled={artifactBusy}
                  className="rounded bg-accent px-3 py-1.5 font-medium text-neutral-950 hover:bg-accent-dark disabled:opacity-40"
                >
                  {artifacts?.files?.resume ? 'Regenerate' : 'Generate'} resume + cover letter
                </button>

                {artifacts?.pdfs?.length > 0 && (
                  <p className="text-neutral-500">
                    PDFs: <span className="mono">{artifacts.pdfs.join(', ')}</span> in{' '}
                    <span className="mono">{artifacts.artifact_dir}</span>
                  </p>
                )}

                {['resume', 'cover_letter'].map((key) =>
                  artifacts?.files?.[key] ? (
                    <details key={key} open={key === 'cover_letter'}>
                      <summary className="cursor-pointer py-1 font-medium capitalize text-neutral-300">
                        {key.replace('_', ' ')}
                      </summary>
                      <pre className="desc mt-1 max-h-96 overflow-auto rounded bg-neutral-950 p-3 text-[11px] leading-relaxed text-neutral-400">
                        {artifacts.files[key]}
                      </pre>
                    </details>
                  ) : null,
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
