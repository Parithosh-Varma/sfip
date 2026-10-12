import React, { useState } from 'react';
import { api, usePoll } from '../api.js';

export default function Papers({ invId }) {
  const [papers, setPapers] = useState([]);
  usePoll(async () => setPapers(await api.papers(invId)), 4000);
  return (
    <div className="card">
      <h2>corpus — {papers.length} papers</h2>
      <table className="sig-table">
        <thead><tr><th>id</th><th>year</th><th>title</th><th>status</th><th>figs</th><th>source</th><th>links</th></tr></thead>
        <tbody>
          {papers.map((p) => (
            <tr key={p.id}>
              <td>{p.id}</td>
              <td>{p.year || ''}</td>
              <td className="ptitle" title={p.authors}>{p.title}</td>
              <td>{p.status}</td>
              <td>{p.n_figures}</td>
              <td>{p.source}</td>
              <td>
                {p.doi && <a href={`https://doi.org/${p.doi}`} target="_blank" rel="noreferrer">doi</a>}
                {p.pdf_url && <> · <a href={p.pdf_url} target="_blank" rel="noreferrer">pdf</a></>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
