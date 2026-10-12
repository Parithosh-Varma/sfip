import React, { useState, useEffect, useRef } from 'react';
import { api, useEvents } from '../api.js';
import { fmtTs } from './Overview.jsx';

export default function Activity({ invId }) {
  const [events, setEvents] = useState([]);
  const [filter, setFilter] = useState('');
  const bottomRef = useRef(null);

  useEffect(() => {
    api.events(invId, 0, 500).then(setEvents).catch(() => {});
  }, [invId]);

  useEvents(invId, (e) => setEvents((prev) => [...prev.slice(-900), e]));

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [events.length]);

  const rows = events.filter(
    (e) => !filter || e.actor.includes(filter) || e.level === filter || e.message.includes(filter),
  );
  return (
    <div className="card">
      <div className="filters">
        <input placeholder="filter by actor / level / text" value={filter}
               onChange={(e) => setFilter(e.target.value)} />
        <span className="muted">{rows.length} events · live</span>
      </div>
      <div className="feed tall">
        {rows.map((e) => (
          <div key={e.id} className={`ev ev-${e.level}`}>
            <span className="ev-ts">{fmtTs(e.ts)}</span>
            <span className="ev-actor">{e.actor}</span>
            <span className="ev-msg">{e.message}</span>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
