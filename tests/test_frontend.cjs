const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, { innerHTML: '', textContent: '', style: {}, classList: { add(){}, toggle(){} }, addEventListener(){}, focus(){} });
  return elements.get(id);
};
const storage = new Map();
const context = vm.createContext({
  console, crypto, AbortController,
  setTimeout(){ return 1; }, clearTimeout(){}, setInterval(){ return 1; }, clearInterval(){},
  navigator: { userAgent: 'Test browser' },
  localStorage: { getItem: k => storage.get(k) || null, setItem: (k,v) => storage.set(k,v) },
  document: { getElementById: element, addEventListener(){}, removeEventListener(){},
    documentElement: { classList: { add(){} } }, querySelectorAll(){ return []; } },
  window: { addEventListener(){}, scrollTo(){} }
});
for (const name of ['rating-config.js', 'rating.js']) vm.runInContext(fs.readFileSync(path.join(root, name), 'utf8'), context, { filename: name });
for (const script of scripts) vm.runInContext(script, context, { filename: 'index.html' });
assert.match(element('app').innerHTML, /data-v="rating"/);
assert.match(element('app').innerHTML, /Начать смену/);
vm.runInContext('view="rating"; render();', context);
assert.match(element('app').innerHTML, /Начисление баллов и ежедневная рассылка пока выключены/);
assert.match(element('app').innerHTML, /ещё не подключён/);
assert.equal((html.match(/Rating\.record\(/g) || []).length, 3);
console.log('Frontend: scripts load; home and rating render; all three game modes connected.');
