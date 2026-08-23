import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * Every learned rule, with its hit count so dead or over-eager rules are
 * visible and prunable. Removing a rule un-blocks future runs immediately.
 */
export default function RulesPanel({ onClose, onChanged }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [draft, setDraft] = useState({ kind: 'company', value: '', reason: '' })

  async function load() {
    try {
      setData(await api.rules())
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  useEffect(() => {
    function onKey(event) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function remove(id) {
    setBusy(true)
    try {
      await api.deleteRule(id)
      await load()
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function add(event) {
    event.preventDefault()
    if (!draft.value.trim()) return
    setBusy(true)
    setError('')
    try {
      await api.addRule(draft.kind, draft.value.trim(), draft.reason.trim())
      setDraft({ kind: draft.kind, value: '', reason: '' })
      await load()
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4"
      onClick={(event) => event.target === event.currentTarget && onClose()}
    >
      <div className="my-8 w-full max-w-3xl rounded-lg border border-neutral-800 bg-neutral-900 shadow-2xl">
        <div className="flex items-center justify-between border-b border-neutral-800 px-5 py-3">
          <h2 className="text-sm font-semibold text-neutral-100">
            Feedback rules {data ? `(${data.count})` : ''}
          </h2>
          <button onClick={onClose} className="text-xs text-neutral-500 hover:text-neutral-300">
            close
          </button>
        </div>

        <div className="space-y-4 px-5 py-4">
          {error && (
            <div className="rounded border border-red-800/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
              {error}
            </div>
          )}

          {!data ? (
            <p className="text-xs text-neutral-500">Loading…</p>
          ) : data.rules.length === 0 ? (
            <p className="text-xs text-neutral-500">No rules yet.</p>
          ) : (
            <div className="overflow-x-auto rounded border border-neutral-800">
              <table className="w-full text-left text-xs">
                <thead className="bg-neutral-950 text-neutral-500">
                  <tr>
                    <th className="px-3 py-2 font-medium">Rule</th>
                    <th className="px-3 py-2 font-medium">Kind</th>
                    <th className="px-3 py-2 font-medium">Value</th>
                    <th className="px-3 py-2 font-medium">Hits</th>
                    <th className="px-3 py-2 font-medium">Reason</th>
                    <th className="px-3 py-2"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-800">
                  {data.rules.map((rule) => (
                    <tr key={rule.id} className="hover:bg-neutral-950/60">
                      <td className="mono whitespace-nowrap px-3 py-2 text-neutral-500">{rule.id}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-neutral-400">{rule.kind}</td>
                      <td className="mono max-w-[16rem] truncate px-3 py-2 text-accent">
                        {rule.value}
                      </td>
                      <td
                        className={`px-3 py-2 ${
                          rule.hits === 0
                            ? 'text-neutral-600'
                            : rule.hits > 40
                              ? 'text-amber-400'
                              : 'text-neutral-300'
                        }`}
                      >
                        {rule.hits}
                      </td>
                      <td className="max-w-[18rem] px-3 py-2 text-neutral-500">{rule.reason}</td>
                      <td className="px-3 py-2 text-right">
                        <button
                          onClick={() => remove(rule.id)}
                          disabled={busy}
                          className="rounded px-2 py-0.5 text-neutral-500 hover:bg-red-950 hover:text-red-300 disabled:opacity-40"
                        >
                          remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="text-[11px] text-neutral-600">
            Hits = jobs in the stored archive this rule blocks. 0 means the rule never fires and can
            probably go. A very high count on a company rule is worth double-checking.
          </p>

          <form onSubmit={add} className="space-y-2 rounded border border-neutral-800 p-3">
            <div className="text-xs font-medium text-neutral-400">Add a rule manually</div>
            <div className="flex flex-wrap gap-2">
              <select
                value={draft.kind}
                onChange={(event) => setDraft({ ...draft, kind: event.target.value })}
                className="rounded border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-xs text-neutral-200"
              >
                {Object.keys(data?.kinds || { company: 1 }).map((kind) => (
                  <option key={kind} value={kind}>
                    {kind}
                  </option>
                ))}
              </select>
              <input
                value={draft.value}
                onChange={(event) => setDraft({ ...draft, value: event.target.value })}
                placeholder="value"
                className="min-w-[10rem] flex-1 rounded border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-xs text-neutral-200 placeholder:text-neutral-600"
              />
              <input
                value={draft.reason}
                onChange={(event) => setDraft({ ...draft, reason: event.target.value })}
                placeholder="reason"
                className="min-w-[10rem] flex-1 rounded border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-xs text-neutral-200 placeholder:text-neutral-600"
              />
              <button
                type="submit"
                disabled={busy || !draft.value.trim()}
                className="rounded bg-accent px-3 py-1.5 text-xs font-medium text-neutral-950 hover:bg-accent-dark disabled:opacity-40"
              >
                Add
              </button>
            </div>
            {data?.kinds?.[draft.kind] && (
              <p className="text-[11px] text-neutral-600">{data.kinds[draft.kind]}</p>
            )}
          </form>
        </div>
      </div>
    </div>
  )
}
