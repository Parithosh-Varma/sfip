import React from 'react';
import { api, usePoll, verdictClass } from '../api.js';

function BudgetBar({ name, b }) {
  const pct = b.limit ? Math.min(100, (100 * b.used) / b.limit) : 0;
  const warn = pct > 85;
  return (
    <div className="budget">
      <div className="budget-label">
        <span>{name}</span>
        <span className="budget-num">{b.used} / {b.limit}</span>
      </div>
      <div className="bar"><div className={warn ? 'fill warn' : 'fill'} style={{ width: `${pct}%` }} /></div>
    </div>
  );
}

export default function Overview({ invId, active }) {
  const [detail, setDetail] = useStateSafe(null);
  usePoll(async () => setDetail(await api.investigation(invId)), 2500);

  const budgets = detail?.budgets || active?.budgets || {};
  const verdicts = detail?.verdicts || active?.verdicts || {};
  const queue = detail?.queue || active?.queue || {};
  const counters = detail?.counters || {};
  const events = active?.recent_events || [];
  const phase = detail?.investigation?.state?.phase || (detail?.investigation?.status === 'running' ? '…' : '—');
  const llmPlan = detail?.investigation?.state?.llm_plan;

  // heartbeat: how long since the last recorded activity + average throughput,
  // so a plateau in one counter never reads as a stalled run
  const lastTs = events.length ? events[0].ts : detail?.investigation?.updated_at;
  const lastAge = lastTs ? Math.max(0, Math.round((Date.now() - new Date(lastTs).getTime()) / 1000)) : null;
  const started = detail?.investigation?.started_at;
  const runMin = started ? Math.max(1 / 60, (Date.now() - new Date(started).getTime()) / 60000) : null;
  const rate = runMin ? (int(counters.jobs_done) / runMin) : null;

  return (
    <div className="grid">
      <section className="card">
        <h2>Controller</h2>
        <div className="kv">
          <div><span>phase</span><b>{phase}</b></div>
          <div><span>heartbeat</span><b>
            {lastAge == null ? '—' : lastAge < 8 ? `active ${lastAge}s ago` : `idle ${fmtAge(lastAge)}`}
            {rate != null && ` · ~${rate.toFixed(1)} jobs/min`}
          </b></div>
          <div><span>jobs done</span><b>{int(counters.jobs_done)}</b></div>
          <div><span>queue</span><b>
            {Object.entries(queue).map(([k, v]) => `${k}:${v}`).join(' · ') || 'empty'}
          </b></div>
          <div><span>LLM calls</span><b>{int(counters.llm_calls)}</b></div>
        </div>
        {llmPlan && (
          <div className="llm-plan">
            <div className="llm-plan-title">advisory focus (LLM)</div>
            <code>{llmPlan.focus}</code> — {llmPlan.rationale}
          </div>
        )}
      </section>

      <section className="card">
        <h2>Budgets</h2>
        {Object.entries(budgets).map(([k, b]) => <BudgetBar key={k} name={k} b={b} />)}
      </section>

      <section className="card">
        <h2>Verified candidates</h2>
        {Object.keys(verdicts).length === 0 && <div className="muted">none yet</div>}
        {Object.entries(verdicts).map(([v, n]) => (
          <div key={v} className="verdict-row">
            <span className={`badge ${verdictClass(v)}`}>{v}</span>
            <span className="verdict-n">{n}</span>
          </div>
        ))}
        <div className="kv" style={{ marginTop: 10 }}>
          <div><span>papers</span><b>{int(counters.papers)}</b></div>
          <div><span>figures</span><b>{int(counters.figures)}</b></div>
          <div><span>comparisons</span><b>{int(counters.comparisons)}</b></div>
          <div><span>downloads (MB)</span><b>{(int(counters.bytes_downloaded) / 1048576).toFixed(1)}</b></div>
        </div>
      </section>

      <section className="card wide">
        <h2>Recent activity</h2>
        <div className="feed">
          {events.length === 0 && <div className="muted">waiting for the first event…</div>}
          {events.map((e) => (
            <div key={e.id} className={`ev ev-${e.level}`}>
              <span className="ev-ts">{fmtTs(e.ts)}</span>
              <span className="ev-actor">{e.actor}</span>
              <span className="ev-msg">{e.message}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

/* tiny local state helper to avoid importing hooks twice */
import { useState as useStateSafe } from 'react';

export function fmtTs(ts) {
  try { return new Date(ts).toLocaleTimeString(); } catch { return ts; }
}
export function fmtAge(s) {
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}
function int(x) { return x == null ? 0 : Math.round(Number(x)); }
