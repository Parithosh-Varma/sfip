/* Node harness: render the real Graph component with jsdom against the live
 * API. Catches render-time throws (TDZ, TypeError) with readable stacks.
 * Usage: SFIP_BASE=http://127.0.0.1:8000 npm run smoke (server must run). */
import { JSDOM } from 'jsdom';

const BASE = process.env.SFIP_BASE || 'http://127.0.0.1:8000';
const realFetch = globalThis.fetch;

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: BASE + '/' });
global.window = dom.window;
global.document = dom.window.document;
global.HTMLElement = dom.window.HTMLElement;
global.SVGElement = dom.window.SVGElement;
global.Node = dom.window.Node;
global.Event = dom.window.Event;
global.CustomEvent = dom.window.CustomEvent;
global.EventSource = class { constructor() {} close() {} addEventListener() {} };
global.IS_REACT_ACT_ENVIRONMENT = true;

// relative API paths resolve against the live server
global.fetch = async (url, opts) => realFetch(new URL(String(url), BASE), opts);

const errors = [];
process.on('uncaughtException', (e) => { errors.push('uncaught: ' + e.stack); });
// never exit silently: a drained loop with pending top-level work must be loud
process.on('unhandledRejection', (e) => {
  console.error('UNHANDLED_REJECTION:', e);
  process.exit(2);
});

const React = (await import('react')).default;
const { createRoot } = await import('react-dom/client');
const { act } = await import('react-dom/test-utils');
const { GraphHarness } = await import('./harness-bundle.mjs');

try {
  await act(async () => {
    createRoot(document.getElementById('root')).render(React.createElement(GraphHarness, {}));
    await new Promise((r) => setTimeout(r, 1500));
  });
} catch (e) {
  errors.push('render: ' + e.stack);
}
await new Promise((r) => setTimeout(r, 2500));
const html = document.getElementById('root').innerHTML;
console.log('ERRORS:', errors.length ? '\n' + errors.join('\n---\n') : 'none');
console.log('HTML_LEN:', html.length);
console.log('HAS_SVG:', html.includes('<svg'));
console.log('NODES_RENDERED:', (html.match(/class="gnode/g) || []).length);

// exercise the click-to-inspect path (Inspector + candidate-detail fetch)
try {
  await act(async () => {
    const node = document.querySelector('.gnode');
    if (node) node.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 1500));
  });
} catch (e) {
  errors.push('click: ' + e.stack);
}
await new Promise((r) => setTimeout(r, 1000));
const html2 = document.getElementById('root').innerHTML;
console.log('ERRORS_AFTER_CLICK:', errors.length ? '\n' + errors.join('\n---\n') : 'none');
console.log('INSPECTOR_OPEN:', html2.includes('graphside') && (html2.includes('candidate') || html2.includes('paper')));
process.exit(errors.length ? 1 : 0);
