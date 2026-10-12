import React, { useState, useMemo } from 'react';
import { api, usePoll, verdictClass } from '../api.js';

/* Lightweight SVG evidence graph: papers → figures → candidates, verdict-colored. */
export default function Graph({ invId }) {
  const [g, setG] = useState({ nodes: [], edges: [] });
  usePoll(async () => setG(await api.graph(invId)), 5000);

  const layout = useMemo(() => layoutGraph(g), [g]);
  if (!g.nodes.length) return <div className="card muted">graph builds as papers arrive…</div>;

  const W = 1100, H = Math.max(420, layout.rows * 92 + 60);
  return (
    <div className="card">
      <h2>evidence graph — {g.nodes.length} nodes · {g.edges.length} edges</h2>
      <svg viewBox={`0 0 ${W} ${H}`} className="graph">
        {layout.edges.map((e, i) => (
          <line key={i} x1={e.x1} y1={e.y1} x2={e.x2} y2={e.y2}
                className={`edge ${e.type} ${e.verdict ? verdictClass(e.verdict) : ''}`} />
        ))}
        {layout.nodes.map((n) => (
          <g key={n.id} transform={`translate(${n.x},${n.y})`}>
            {n.type === 'paper' && <rect x="-46" y="-16" width="92" height="32" rx="6" className="node-paper" />}
            {n.type === 'figure' && (
              n.thumb
                ? <image href={n.thumb} x="-26" y="-26" width="52" height="52" className="node-thumb" />
                : <circle r="14" className="node-fig" />
            )}
            {n.type === 'candidate' && (
              <rect x="-22" y="-22" width="44" height="44" rx="8"
                    className={`node-cand ${verdictClass(n.verdict)}`} transform="rotate(45)" />
            )}
            <text y={n.type === 'figure' && n.thumb ? 40 : n.y2 || 34} className="node-label">{n.label}</text>
            {n.score != null && <text y="46" className="node-score">{Number(n.score).toFixed(2)}</text>}
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
  );
}

function layoutGraph(g) {
  const papers = g.nodes.filter((n) => n.type === 'paper');
  const figs = g.nodes.filter((n) => n.type === 'figure');
  const cands = g.nodes.filter((n) => n.type === 'candidate');
  const W = 1100;
  const pos = {};
  const rows = Math.max(papers.length, 1);
  papers.forEach((p, i) => { pos[p.id] = { x: 130, y: 60 + (i * (rows > 1 ? 560 / (rows - 1 || 1) : 0)) }; });
  const byPaper = {};
  figs.forEach((f) => { (byPaper[f.paper] = byPaper[f.paper] || []).push(f); });
  Object.entries(byPaper).forEach(([pid, list]) => {
    const base = pos[pid] || { x: 130, y: 60 };
    list.forEach((f, i) => { pos[f.id] = { x: 480, y: base.y + (i - (list.length - 1) / 2) * 84 }; });
  });
  cands.forEach((c, i) => {
    const linked = g.edges.filter((e) => e.source === c.id).map((e) => pos[e.target]).filter(Boolean);
    const y = linked.length ? linked.reduce((s, p) => s + p.y, 0) / linked.length : 80 + i * 70;
    pos[c.id] = { x: 860, y: Math.min(Math.max(y, 50), 60 + rows * 92) };
  });
  const nodes = g.nodes.map((n) => ({ ...n, ...(pos[n.id] || { x: 600, y: 80 }) }));
  const edges = g.edges
    .filter((e) => pos[e.source] && pos[e.target])
    .map((e) => ({ ...e, x1: pos[e.source].x + 40, y1: pos[e.source].y, x2: pos[e.target].x - 40, y2: pos[e.target].y }));
  return { nodes, edges, rows };
}
