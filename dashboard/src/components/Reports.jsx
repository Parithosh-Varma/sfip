import React, { useState } from 'react';
import { api, usePoll } from '../api.js';
import { marked } from 'marked';

export default function Reports({ invId }) {
  const [list, setList] = useState([]);
  const [md, setMd] = useState(null);
  const [title, setTitle] = useState('');
  usePoll(async () => setList(await api.reports(invId)), 8000);

  async function open(r) {
    const d = await api.report(r.id);
    setTitle(`report #${r.id} — ${r.created_at}`);
    setMd(d.markdown);
  }

  return (
    <div className="cands">
      <div className="cands-list card">
        <h2>reports</h2>
        {list.length === 0 && <div className="muted">generated automatically when a run finishes</div>}
        {list.map((r) => (
          <button key={r.id} className="cand-row" onClick={() => open(r)}>
            <span className="cand-id">#{r.id}</span>
            <span className="cand-pair">{r.created_at}</span>
          </button>
        ))}
        <p className="muted">{list[0]?.summary}</p>
      </div>
      <div className="cands-detail card report">
        {!md && <div className="muted">select a report</div>}
        {md && <><h2>{title}</h2><div dangerouslySetInnerHTML={{ __html: marked.parse(md) }} /></>}
      </div>
    </div>
  );
}
