import React, { useState } from 'react';
import { api, usePoll } from './api.js';
import Overview from './components/Overview.jsx';
import Candidates from './components/Candidates.jsx';
import Papers from './components/Papers.jsx';
import Graph from './components/Graph.jsx';
import Reports from './components/Reports.jsx';
import Activity from './components/Activity.jsx';

const TABS = ['Overview', 'Candidates', 'Papers', 'Graph', 'Reports', 'Activity'];

export default function App() {
  const [state, setState] = useState(null);
  const [health, setHealth] = useState(null);
  const [tab, setTab] = useState('Overview');

  usePoll(async () => setState(await api.state()), 2500);
  usePoll(async () => setHealth(await fetch('/api/health').then((r) => r.json())), 15000);

  React.useEffect(() => {
    const h = () => setTab('Candidates');
    window.addEventListener('sfip:open-candidate', h);
    return () => window.removeEventListener('sfip:open-candidate', h);
  }, []);

  const invs = state?.investigations || [];
  const active = state?.active || null;
  const invId = active?.investigation?.id || invs[0]?.id || null;
  const cur = active?.investigation || invs.find((i) => i.id === invId) || null;
  const running = cur?.status === 'running';

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">◧</span>
          <div>
            <div className="title">SFIP — Scientific Image Forensics</div>
            <div className="subtitle">autonomous investigation · read-only observation</div>
          </div>
        </div>
        <div className="topmeta">
          {cur && (
            <div className="seed" title={cur.seed}>
              <span className={`dot ${running ? 'dot-live' : 'dot-idle'}`} />
              #{cur.id} · {cur.seed_type} · <code>{cur.seed}</code>
            </div>
          )}
          <span className={`pill ${running ? 'pill-live' : 'pill-done'}`}>
            {cur ? cur.status.toUpperCase() : 'IDLE'}
          </span>
          <span className={`pill ${health?.llm_available ? 'pill-live' : 'pill-done'}`}
                title={health?.model}>
            LLM {health?.llm_available ? 'ON' : 'FALLBACK'}
          </span>
        </div>
      </header>

      <nav className="tabs">
        {TABS.map((t) => (
          <button key={t} className={tab === t ? 'tab active' : 'tab'} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
        {invId && (
          <a className="tab export" href={`/api/export/${invId}`}>Export ↧</a>
        )}
      </nav>

      <main className="content">
        {!invId && <div className="empty">No investigations yet. Start one with:<br />
          <code>python backend/app/main.py investigate "&lt;DOI or researcher name&gt;"</code></div>}
        {invId && tab === 'Overview' && <Overview invId={invId} active={active} />}
        {invId && tab === 'Candidates' && <Candidates invId={invId} />}
        {invId && tab === 'Papers' && <Papers invId={invId} />}
        {invId && tab === 'Graph' && <Graph invId={invId} />}
        {invId && tab === 'Reports' && <Reports invId={invId} />}
        {invId && tab === 'Activity' && <Activity invId={invId} />}
      </main>
    </div>
  );
}
