import React, { useState } from 'react';
import { api, usePoll, verdictClass } from '../api.js';

const VERDICTS = ['', 'exact_duplicate', 'high', 'moderate', 'weak', 'likely_false_positive'];

export default function Candidates({ invId }) {
  const [verdict, setVerdict] = useState('');
  const [minScore, setMinScore] = useState(0);
  const [data, setData] = useState({ total: 0, items: [] });
  const [sel, setSel] = useState(null);
  const [detail, setDetail] = useState(null);

  usePoll(async () => {
    const d = await api.candidates(invId, {
      ...(verdict ? { verdict } : {}),
      min_score: minScore,
      limit: 100,
    });
    setData(d);
  }, 3000);

  async function open(id) {
    setSel(id);
    setDetail(await api.candidate(id));
  }

  return (
    <div className="cands">
      <div className="cands-list card">
        <div className="filters">
          <select value={verdict} onChange={(e) => setVerdict(e.target.value)}>
            {VERDICTS.map((v) => <option key={v} value={v}>{v || 'all verdicts'}</option>)}
          </select>
          <label>min score
            <input type="number" min="0" max="1" step="0.05" value={minScore}
                   onChange={(e) => setMinScore(Number(e.target.value))} />
          </label>
          <span className="muted">{data.total} total</span>
        </div>
        {data.items.length === 0 && <div className="muted">no candidates yet — the loop promotes pairs here automatically</div>}
        {data.items.map((c) => (
          <button key={c.id} className={`cand-row ${sel === c.id ? 'sel' : ''}`} onClick={() => open(c.id)}>
            <span className={`badge ${verdictClass(c.verdict)}`}>{c.verdict || c.stage}</span>
            <span className="cand-id">#{c.id}</span>
            <span className="cand-pair">fig {c.figure_a} ↔ fig {c.figure_b}</span>
            <span className="cand-score">{(c.score ?? 0).toFixed(3)}</span>
          </button>
        ))}
      </div>
      <div className="cands-detail card">
        {!detail && <div className="muted">select a candidate to inspect evidence</div>}
        {detail && <Detail d={detail} />}
      </div>
    </div>
  );
}

function Detail({ d }) {
  const c = d.candidate;
  const sig = JSON.parse(c.signals_json || '{}');
  const flags = JSON.parse(c.flags_json || '[]');
  const interp = c.interpretation_json ? JSON.parse(c.interpretation_json) : null;
  return (
    <div>
      <h2>candidate #{c.id} <span className={`badge ${verdictClass(c.verdict)}`}>{c.verdict}</span></h2>
      <div className="kv">
        <div><span>score</span><b>{(c.score ?? 0).toFixed(4)}</b></div>
        <div><span>best transform</span><b>{c.best_transform || '—'}</b></div>
        <div><span>uncertainty</span><b>{c.uncertainty ?? '—'}</b></div>
        <div><span>flags</span><b>{flags.join(', ') || 'none'}</b></div>
      </div>
      <div className="pair-imgs">
        <Fig side="A" f={d.figure_a} p={d.paper_a} ev={d.figure_a_evidence || []} />
        <Fig side="B" f={d.figure_b} p={d.paper_b} ev={d.figure_b_evidence || []} />
      </div>
      <h3>signals</h3>
      <table className="sig-table">
        <tbody>
          {Object.entries(sig).map(([k, v]) => (
            <tr key={k}><td>{k}</td><td><code>{String(v)}</code></td></tr>
          ))}
        </tbody>
      </table>
      <h3>evidence trail</h3>
      <div className="feed">
        {d.evidence.map((e) => (
          <div key={e.id} className="ev">
            <span className="ev-ts">#{e.id}</span>
            <span className="ev-actor">{e.method}</span>
            <span className="ev-msg">{e.interpretation || e.value}</span>
          </div>
        ))}
      </div>
      {interp && (
        <>
          <h3>interpretation {interp.fallback ? '(heuristic fallback)' : '(LLM-grounded)'}</h3>
          <p>{interp.summary}</p>
          <p className="muted">{interp.context_hypothesis}</p>
          {(interp.alternative_explanations || []).map((a, i) => <p key={i}>◦ {a}</p>)}
        </>
      )}
    </div>
  );
}

function Fig({ side, f, p, ev }) {
  if (!f) return null;
  const src = mediaSrc(f.image_path);
  const prov = (ev || []).filter((e) => e.kind === 'provenance' || e.kind === 'forensics');
  return (
    <div className="figbox">
      <div className="figbox-title">figure {f.id} ({side}) — paper {f.paper_id}, p.{f.page} {f.width}×{f.height}</div>
      {prov.map((e) => <ProvChip key={e.id} e={e} />)}
      {src
        ? <a href={src} target="_blank" rel="noreferrer"><img src={src} alt={`figure ${f.id}`} loading="lazy" /></a>
        : <div className="muted">image unavailable</div>}
      {f.caption && <div className="caption">{f.caption}</div>}
      {p && <div className="muted">{p.title}</div>}
    </div>
  );
}

function ProvChip({ e }) {
  let v = {};
  try { v = JSON.parse(e.value || '{}'); } catch { /* raw string */ }
  const hot = v.suspicion === 'high' || v.verdict === 'ai_tool_marked' || v.flag === true;
  const label = v.suspicion ? `AI-provenance: ${v.suspicion}`
    : v.flag === true ? `tamper: ${e.method}`
    : `metadata: ${v.verdict || e.method}`;
  const cls = hot ? 'v-high' : v.suspicion === 'medium' ? 'v-mod' : 'v-fp';
  return <div className="prov" title={e.interpretation}><span className={`badge ${cls}`}>{label}</span></div>;
}

function mediaSrc(absPath) {
  if (!absPath) return null;
  const i = absPath.indexOf('/data/');
  return i >= 0 ? '/media/' + absPath.slice(i + 6) : null;
}
