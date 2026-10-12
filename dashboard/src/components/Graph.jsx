import React, { useState, useMemo, useRef } from 'react';
import { api, usePoll, verdictClass } from '../api.js';

/* Evidence graph: papers → figures → candidates.
 * Pan (drag) + zoom (wheel), verdict/score filters, click-to-inspect. */
const COL_X = { paper: 150, figure: 540, candidate: 920 };
const W = 1080;

const ALL_VERDICTS = ['exact_duplicate', 'high', 'moderate', 'weak', 'likely_false_positive', 'unverified'];

export default function Graph({ invId }) {
  const [g, setG] = useState({ nodes: [], edges: [] });
  const [papers, setPapers] = useState([]);
  const [figures, setFigures] = useState([]);
  usePoll(async () => setG(await api.graph(invId)), 5000);
  usePoll(async () => setPapers(await api.papers(invId)), 15000);
  usePoll(async () => setFigures(await api.figures(invId)), 15000);

  const [show, setShow] = useState({ paper: true, figure: true, candidate: true });
  const [verdicts, setVerdicts] = useState(new Set(['exact_duplicate', 'high', 'moderate', 'weak']));
  const [minScore, setMinScore] = useState(0);
  const [linkedOnly, setLinkedOnly] = useState(true);
  const [thumbs, setThumbs] = useState(true);
  const [sel, setSel] = useState(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const fitted = useRef(false);

  // fit whole graph on first load; afterwards the user's view is never yanked
  React.useEffect(() => {
    if (!fitted.current && H > 0 && nodes.length > 0) {
      fitted.current = true;
      setView({ x: 0, y: 0, k: Math.min(1, Math.max(0.08, VB_H / H)) });
    }
  }, [H, nodes.length]);

  const paperById = useMemo(() => Object.fromEntries(papers.map((p) => [`p${p.id}`, p])), [papers]);
  const figById = useMemo(() => Object.fromEntries(figures.map((f) => [`f${f.id}`, f])), [figures]);

  const { nodes, edges, H } = useMemo(
    () => layoutGraph(g, { show, verdicts, minScore, linkedOnly }),
    [g, show, verdicts, minScore, linkedOnly],
  );

  const svgRef = useRef(null);
  const drag = useRef(null);
  // svg box has fixed aspect 1080:670 via CSS; viewBox keeps the same ratio,
  // so screen-px → svg-unit scale is uniform on both axes.
  const VB_H = (W / 1) * 0.62;

  function unitScale() {
    const r = svgRef.current.getBoundingClientRect();
    return (W / view.k) / r.width;
  }

  function onWheel(e) {
    const r = svgRef.current.getBoundingClientRect();
    const mx = view.x + ((e.clientX - r.left) / r.width) * (W / view.k);
    const my = view.y + ((e.clientY - r.top) / r.height) * (VB_H / view.k);
    const k2 = Math.min(6, Math.max(0.08, view.k * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
    const s = k2 / view.k;
    setView({ k: k2, x: mx - (mx - view.x) * s, y: my - (my - view.y) * s });
  }
  function onMouseDown(e) {
    drag.current = { sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y, moved: false };
  }
  function onMouseMove(e) {
    if (!drag.current) return;
    const k = unitScale();
    const dx = (e.clientX - drag.current.sx) * k;
    const dy = (e.clientY - drag.current.sy) * k;
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.current.moved = true;
    setView((v) => ({ ...v, x: drag.current.vx - dx, y: drag.current.vy - dy }));
  }
  function onMouseUp() { drag.current = null; }
  function reset() { setView({ x: 0, y: 0, k: Math.min(1, Math.max(0.08, VB_H / H)) }); }

  function toggleVerdict(v) {
    setVerdicts((prev) => {
      const n = new Set(prev);
      if (n.has(v)) n.delete(v);
      else n.add(v);
      return n;
    });
  }

  return (
    <div className="graphwrap">
      <div className="graphbar card">
        <div className="filters">
          {['paper', 'figure', 'candidate'].map((t) => (
            <label key={t}><input type="checkbox" checked={show[t]}
              onChange={() => setShow((s) => ({ ...s, [t]: !s[t] }))} /> {t}s</label>
          ))}
          <label>min score
            <input type="number" min="0" max="1" step="0.05" value={minScore}
              onChange={(e) => setMinScore(Number(e.target.value))} />
          </label>
          <label><input type="checkbox" checked={linkedOnly} onChange={() => setLinkedOnly(!linkedOnly)} />
            only linked figures</label>
          <label><input type="checkbox" checked={thumbs} onChange={() => setThumbs(!thumbs)} />
            thumbnails</label>
          <button className="mini-btn" onClick={reset}>reset view</button>
        </div>
        <div className="filters">
          {ALL_VERDICTS.map((v) => (
            <label key={v}><input type="checkbox" checked={verdicts.has(v)}
              onChange={() => toggleVerdict(v)} />
              <span className={`badge ${verdictClass(v)}`}>{v}</span></label>
          ))}
        </div>
        <div className="muted">{nodes.length} nodes · {edges.length} edges · drag to pan · wheel to zoom · click to inspect</div>
      </div>
      <div className="graphmain">
        <div className="card graphsvg">
          <svg ref={svgRef} viewBox={`${view.x} ${view.y} ${W / view.k} ${VB_H / view.k}`}
               onWheel={onWheel} onMouseDown={onMouseDown} onMouseMove={onMouseMove}
               onMouseUp={onMouseUp} onMouseLeave={onMouseUp} className="graph">
            {edges.map((e, i) => (
              <line key={i} x1={e.x1} y1={e.y1} x2={e.x2} y2={e.y2}
                    className={`edge ${e.type} ${e.verdict ? verdictClass(e.verdict) : ''}`}
                    strokeWidth={e.w || 1.2}>
                <title>{e.type}{e.score != null ? ` — score ${Number(e.score).toFixed(3)}` : ''}</title>
              </line>
            ))}
            {nodes.map((n) => (
              <g key={n.id} transform={`translate(${n.x},${n.y})`}
                 onClick={(ev) => { if (!drag.current?.moved) setSel(n); ev.stopPropagation(); }}
                 className={`gnode ${sel?.id === n.id ? 'sel' : ''}`}>
                {n.type === 'paper' && (
                  <><rect x="-58" y="-17" width="116" height="34" rx="7" className="node-paper" />
                    <text className="node-label">{n.label}</text></>
                )}
                {n.type === 'figure' && (thumbs && n.thumb
                  ? <><image href={n.thumb} x="-24" y="-24" width="48" height="48" className="node-thumb" />
                      <text y="38" className="node-label">{n.label}</text></>
                  : <><circle r="13" className="node-fig" />
                      <text y="28" className="node-label">{n.label}</text></>)}
                {n.type === 'candidate' && (
                  <><rect x="-20" y="-20" width="40" height="40" rx="8"
                          className={`node-cand ${verdictClass(n.verdict)}`} transform="rotate(45)" />
                    <text y="34" className="node-label">{n.label}</text>
                    {n.score != null && <text y="46" className="node-score">{Number(n.score).toFixed(2)}</text>}</>
                )}
                <title>{n.title || n.label}</title>
              </g>
            ))}
          </svg>
          <div className="legend">
            <span><i className="sw sw-paper" /> paper</span>
            <span><i className="sw sw-fig" /> figure</span>
            <span><i className="sw v-exact" /> exact</span>
            <span><i className="sw v-high" /> high</span>
            <span><i className="sw v-mod" /> moderate</span>
            <span><i className="sw v-weak" /> weak</span>
          </div>
        </div>
        <div className="card graphside">
          {!sel && <div className="muted">click any node to inspect it here</div>}
          {sel && <Inspector n={sel} invId={invId} paperById={paperById} figById={figById} />}
        </div>
      </div>
    </div>
  );
}

function layoutGraph(g, { show, verdicts, minScore, linkedOnly }) {
  const vOK = (c) => verdicts.has(c.verdict || 'unverified') && (c.score ?? 0) >= minScore;
  const visCands = show.candidate ? g.nodes.filter((n) => n.type === 'candidate' && vOK(n)) : [];
  const visCandIds = new Set(visCands.map((c) => c.id));
  const linkedFigs = new Set();
  g.edges.forEach((e) => {
    if (e.type === 'similarity' && visCandIds.has(e.source)) linkedFigs.add(e.target);
  });
  const visFigs = show.figure
    ? g.nodes.filter((n) => n.type === 'figure' && (!linkedOnly || linkedFigs.has(n.id)))
    : [];
  const visFigIds = new Set(visFigs.map((f) => f.id));
  const usedPapers = new Set(visFigs.map((f) => f.paper));
  const visPapers = show.paper
    ? g.nodes.filter((n) => n.type === 'paper' && (usedPapers.size === 0 || usedPapers.has(n.id)))
    : [];

  const H = Math.max(560, visFigs.length * 44, visCands.length * 40, visPapers.length * 80) + 40;
  const pos = {};
  const spread = (list, x) => list.forEach((n, i) => {
    pos[n.id] = { x, y: list.length === 1 ? H / 2 : 50 + (i * (H - 100)) / (list.length - 1) };
  });
  spread([...visPapers].sort((a, b) => a.id.localeCompare(b.id)), COL_X.paper);
  // figures grouped by paper, ordered by paper then id
  const figsSorted = [...visFigs].sort((a, b) =>
    a.paper === b.paper ? a.id.localeCompare(b.id) : a.paper.localeCompare(b.paper));
  spread(figsSorted, COL_X.figure);
  spread([...visCands].sort((a, b) => (b.score ?? 0) - (a.score ?? 0)), COL_X.candidate);

  const nodes = [...visPapers, ...figsSorted, ...visCands].map((n) => ({ ...n, ...(pos[n.id] || { x: 540, y: 60 }) }));
  const edges = g.edges
    .filter((e) => pos[e.source] && pos[e.target])
    .filter((e) => e.type !== 'similarity' || visCandIds.has(e.source))
    .map((e) => {
      const a = pos[e.source], b = pos[e.target];
      const w = e.type === 'similarity' ? 1 + 3 * (e.score ?? 0) : 1;
      return { ...e, x1: a.x + 52, y1: a.y, x2: b.x - 52, y2: b.y, w };
    });
  return { nodes, edges, H };
}

function Inspector({ n, invId, paperById, figById }) {
  const [cand, setCand] = useState(null);
  React.useEffect(() => {
    setCand(null);
    if (n.type === 'candidate') api.candidate(n.id.slice(1)).then(setCand).catch(() => {});
  }, [n]);

  if (n.type === 'paper') {
    const p = paperById[n.id];
    return <div>
      <h2>{n.id} — paper</h2>
      {p ? <><p><b>{p.title}</b></p>
        <div className="kv">
          <div><span>authors</span><b>{(p.authors || '').slice(0, 120)}</b></div>
          <div><span>year</span><b>{p.year || ''}</b></div>
          <div><span>doi</span><b>{p.doi || ''}</b></div>
          <div><span>status</span><b>{p.status}</b></div>
          <div><span>figures</span><b>{p.n_figures}</b></div>
        </div>
        {p.doi && <p><a href={`https://doi.org/${p.doi}`} target="_blank" rel="noreferrer">open paper ↗</a></p>}
      </> : <div className="muted">metadata loading…</div>}
    </div>;
  }
  if (n.type === 'figure') {
    const f = figById[n.id];
    return <div>
      <h2>figure {n.id.slice(1)}</h2>
      {n.thumb && <a href={n.thumb} target="_blank" rel="noreferrer">
        <img src={n.thumb} alt={n.id} className="inspect-img" /></a>}
      {f ? <div className="kv">
        <div><span>paper</span><b>{f.paper_id}</b></div>
        <div><span>page</span><b>{f.page}</b></div>
        <div><span>size</span><b>{f.width}×{f.height}</b></div>
      </div> : null}
      {f?.caption && <p className="caption">{f.caption}</p>}
    </div>;
  }
  return <div>
    <h2>candidate {n.id.slice(1)} <span className={`badge ${verdictClass(n.verdict)}`}>{n.verdict}</span></h2>
    <div className="kv">
      <div><span>score</span><b>{n.score != null ? Number(n.score).toFixed(4) : '—'}</b></div>
      {cand && <>
        <div><span>transform</span><b>{cand.candidate.best_transform || '—'}</b></div>
        <div><span>uncertainty</span><b>{cand.candidate.uncertainty ?? '—'}</b></div>
      </>}
    </div>
    {cand?.candidate?.flags_json && <p className="muted">flags: {JSON.parse(cand.candidate.flags_json).join(', ') || 'none'}</p>}
    {cand?.interpretation?.summary && <p>{cand.interpretation.summary}</p>}
    <button className="mini-btn" onClick={() =>
      window.dispatchEvent(new CustomEvent('sfip:open-candidate', { detail: Number(n.id.slice(1)) }))}>
      open in Candidates tab →
    </button>
  </div>;
}
