import { useEffect, useRef, useState, useCallback } from 'react';

async function j(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

export const api = {
  state: () => j('/api/state'),
  investigation: (id) => j(`/api/investigations/${id}`),
  papers: (id) => j(`/api/investigations/${id}/papers`),
  figures: (id) => j(`/api/investigations/${id}/figures`),
  candidates: (id, params = {}) =>
    j(`/api/investigations/${id}/candidates?${new URLSearchParams(params)}`),
  candidate: (cid) => j(`/api/candidates/${cid}`),
  events: (id, after = 0, limit = 200) =>
    j(`/api/investigations/${id}/events?after=${after}&limit=${limit}`),
  graph: (id) => j(`/api/investigations/${id}/graph`),
  reports: (id) => j(`/api/investigations/${id}/reports`),
  report: (rid) => j(`/api/reports/${rid}`),
};

export function usePoll(fn, ms = 2500) {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const [error, setError] = useState(null);
  const tick = useCallback(() => {
    fnRef.current().catch((e) => setError(String(e)));
  }, []);
  useEffect(() => {
    tick();
    const t = setInterval(tick, ms);
    return () => clearInterval(t);
  }, [tick, ms]);
  return error;
}

export function useEvents(invId, onEvent) {
  const ref = useRef(onEvent);
  ref.current = onEvent;
  useEffect(() => {
    if (!invId) return undefined;
    let es;
    try {
      es = new EventSource(`/api/stream/${invId}?after=0`);
      es.onmessage = (e) => {
        try { ref.current(JSON.parse(e.data)); } catch { /* ignore */ }
      };
    } catch { /* SSE optional */ }
    return () => es && es.close();
  }, [invId]);
}

export const verdictClass = (v) => ({
  exact_duplicate: 'v-exact',
  high: 'v-high',
  moderate: 'v-mod',
  weak: 'v-weak',
  likely_false_positive: 'v-fp',
}[v] || 'v-un');
