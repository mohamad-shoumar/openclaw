import { useEffect, useRef } from 'react'

function Badge({ children, tone = 'neutral' }) {
  const tones = {
    neutral: 'bg-neutral-800 text-neutral-300',
    good: 'bg-emerald-900/60 text-emerald-300',
    warn: 'bg-amber-900/60 text-amber-300',
    bad: 'bg-red-900/60 text-red-300',
    accent: 'bg-accent/20 text-accent',
  }
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${tones[tone]}`}>
      {children}
    </span>
  )
}

function scopeTone(scope) {
  if (scope === 'global') return 'good'
  if (scope === 'open') return 'accent'
  if (scope === 'restricted') return 'bad'
  return 'neutral'
}

function ageLabel(job) {
  if (job.age_days === null || job.age_days === undefined) return '-'
  if (job.age_days === 0) return 'today'
  return `${job.age_days}d`
}

export default function JobList({ jobs, selected, onSelect, loading }) {
  const activeRef = useRef(null)

  // Keep the keyboard-selected row visible when navigating with j/k.
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'nearest' })
  }, [selected])

  if (loading) {
    return <div className="p-4 text-sm text-neutral-500">Loading…</div>
  }
  if (!jobs.length) {
    return (
      <div className="p-6 text-center text-sm text-neutral-500">
        Nothing here.
        <div className="mt-1 text-xs text-neutral-600">
          Try another tab, or hit Refresh to fetch new jobs.
        </div>
      </div>
    )
  }

  return (
    <ul className="divide-y divide-neutral-800">
      {jobs.map((job) => {
        const isActive = job.job_slug === selected
        return (
          <li
            key={job.job_slug}
            ref={isActive ? activeRef : null}
            onClick={() => onSelect(job.job_slug)}
            className={`cursor-pointer px-3 py-2.5 transition-colors ${
              isActive
                ? 'border-l-2 border-accent bg-accent/10'
                : 'border-l-2 border-transparent hover:bg-neutral-900'
            }`}
          >
            <div className="flex items-start justify-between gap-2">
              <span className="truncate text-sm font-medium text-neutral-100">
                {job.company || '(no company)'}
              </span>
              <span className="shrink-0 text-[10px] text-neutral-500">{ageLabel(job)}</span>
            </div>
            <div className="mt-0.5 line-clamp-2 text-sm text-neutral-400">{job.title}</div>
            <div className="mt-1.5 flex flex-wrap gap-1">
              <Badge tone={scopeTone(job.remote_scope)}>{job.remote_scope}</Badge>
              <Badge>{job.discovery_source}</Badge>
              {job.is_aggregator && <Badge tone="warn">aggregator</Badge>}
              {job.applied_at && <Badge tone="good">applied</Badge>}
            </div>
          </li>
        )
      })}
    </ul>
  )
}
