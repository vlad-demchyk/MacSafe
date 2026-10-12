// The run bar: added to the report page after loading (through pywebview.evaluate_js).
// Runs nothing itself. It only calls Api.run_check and polls Api.status.
(() => {
  if (document.getElementById('audit-bar')) return;

  const css = `
    #audit-bar { position:fixed; top:0; left:0; right:0; z-index:9999; height:56px; box-sizing:border-box; display:flex; align-items:center; gap:10px;
      padding:0 18px; background:color-mix(in srgb, var(--card, #fff) 82%, transparent); backdrop-filter:blur(18px) saturate(1.4);
      -webkit-backdrop-filter:blur(18px) saturate(1.4); border-bottom:1px solid var(--line, #d6d6dc); color:var(--ink, #242426);
      font:14px/1.3 -apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,Arial,sans-serif; }
    #audit-bar button { display:inline-flex; align-items:center; gap:8px; font:inherit; font-weight:600; border-radius:10px; padding:8px 14px;
      cursor:pointer; border:1px solid transparent; transition:filter .15s, transform .12s; }
    #audit-bar button svg { width:16px; height:16px; fill:none; stroke:currentColor; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
    #audit-bar button:active:not(:disabled) { transform:scale(.97); }
    #audit-bar button:disabled { opacity:.45; cursor:not-allowed; }
    #audit-run { background:var(--accent, #2563eb); color:var(--on-accent, #fff); }
    #audit-run:hover:not(:disabled) { filter:brightness(1.1); }
    #audit-purge { background:var(--card2, #ededf0); color:var(--ink, #242426); border-color:var(--line, #d6d6dc); }
    #audit-purge:hover:not(:disabled) { filter:brightness(.97); }
    .ab-spacer { flex:1; }
    #audit-pill { display:inline-flex; align-items:center; gap:10px; padding:6px 14px; border-radius:999px; border:1px solid var(--line, #d6d6dc);
      background:var(--card, #fff); min-width:0; max-width:60%; }
    #audit-pill .ab-dot { width:9px; height:9px; border-radius:50%; background:var(--muted, #8a8a90); flex:none; }
    #audit-state { font-weight:650; white-space:nowrap; }
    #audit-msg { color:var(--muted, #64646a); font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; font-variant-numeric:tabular-nums; }
    #audit-pill.running { border-color:var(--blue, #2563eb); } #audit-pill.running .ab-dot { background:var(--blue, #2563eb); animation:audit-pulse 1s ease-in-out infinite; }
    #audit-pill.done .ab-dot { background:var(--good, #16a34a); } #audit-pill.done #audit-state { color:var(--good, #16a34a); }
    #audit-pill.error { border-color:var(--critical, #dc2626); } #audit-pill.error .ab-dot { background:var(--critical, #dc2626); } #audit-pill.error #audit-state { color:var(--critical, #dc2626); }
    #audit-steps { display:inline-flex; gap:4px; align-items:center; }
    #audit-steps i { display:block; width:20px; height:5px; border-radius:3px; background:var(--line, #d6d6dc); }
    #audit-steps i.done { background:var(--good, #16a34a); } #audit-steps i.now { background:var(--blue, #2563eb); animation:audit-pulse 1s ease-in-out infinite; }
    @keyframes audit-pulse { 50% { opacity:.4; } }
    @media (prefers-reduced-motion:reduce) { #audit-steps i.now, #audit-pill.running .ab-dot { animation:none; } #audit-bar button { transition:none; } }
    #audit-setup { position:fixed; right:16px; bottom:16px; z-index:9999; width:340px; padding:14px 16px; border-radius:16px;
      background:var(--card, #fff); color:var(--ink, #242426); border:1px solid var(--line, #d6d6dc); box-shadow:0 18px 52px rgba(0,0,0,.25);
      font:13px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,Arial,sans-serif; }
    #audit-setup p { margin:6px 0 10px; color:var(--muted, #64646a); }
    #audit-setup .row { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:8px 0; border-top:1px solid var(--line, #d6d6dc); }
    #audit-setup small { display:block; color:var(--muted, #64646a); font-size:12px; }
    #audit-setup button { font:inherit; color:var(--on-accent, #fff); background:var(--accent, #2563eb); border:0; border-radius:8px; padding:6px 12px; cursor:pointer; }
    body { padding-top:56px !important; }
    .side { top:56px !important; height:calc(100vh - 56px) !important; }
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  const ICON_PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4l13 8-13 8z"/></svg>';
  const ICON_MEM = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="8" width="18" height="8" rx="1"/><path d="M7 8v8M11 8v8M15 8v8"/></svg>';
  const bar = document.createElement('div');
  bar.id = 'audit-bar';
  bar.innerHTML = `<button id="audit-run" type="button" disabled>${ICON_PLAY}Запустити перевірку</button>`
    + `<button id="audit-purge" type="button" disabled title="Потрібен пароль адміністратора при кожному виклику">${ICON_MEM}Звільнити памʼять</button>`
    + '<span class="ab-spacer"></span>'
    + '<div id="audit-pill" class="idle"><i class="ab-dot"></i><span id="audit-state">Завантаження…</span><span id="audit-steps" hidden></span><span id="audit-msg"></span></div>';
  document.body.prepend(bar);

  const STATE = { idle: 'Готовий', running: 'Виконується', done: 'Готово', error: 'Помилка' };
  const runBtn = document.getElementById('audit-run');
  const pill = document.getElementById('audit-pill');
  const stateEl = document.getElementById('audit-state');
  const msgEl = document.getElementById('audit-msg');
  let polling = false;

  // The check script reports its stages as "Collecting: ..." lines. We show them as step N of 5 instead of raw text.
  const STAGES = [
    [/hardware/i, 'Система'], [/load, memory/i, 'Памʼять і процеси'], [/security/i, 'Безпека й мережа'],
    [/caches/i, 'Кеші'], [/Building/i, 'Звіт'],
  ];
  const stepsEl = document.getElementById('audit-steps');
  const stageOf = msg => STAGES.findIndex(([re]) => re.test(msg || ''));
  const renderSteps = s => {
    const n = s.status === 'running' ? stageOf(s.message) : -1;
    stepsEl.hidden = n < 0;
    if (n < 0) return null;
    stepsEl.innerHTML = STAGES.map((_, i) => `<i class="${i < n ? 'done' : i === n ? 'now' : ''}"></i>`).join('');
    return `${n + 1}/${STAGES.length} ${STAGES[n][1]}`;
  };

  const setStatus = status => { pill.className = status; };
  const render = s => {
    stateEl.textContent = STATE[s.status] || s.status;
    setStatus(s.status);
    runBtn.disabled = s.status === 'running';
    let extra = '';
    if (s.started) {
      const end = s.finished || s.now;
      const sec = Math.max(0, Math.round(end - s.started));
      extra = `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
    }
    const stage = renderSteps(s);
    const text = stage || (s.status === 'done' ? '' : s.message) || '';
    msgEl.textContent = [text, extra].filter(Boolean).join(' · ');
  };

  const poll = async () => {
    try {
      render(await window.pywebview.api.status());
    } catch (e) {
      stateEl.textContent = 'Немає зв’язку із застосунком';
      setStatus('error');
    }
  };

  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    try {
      const res = await window.pywebview.api.run_check();
      if (!res.ok) { msgEl.textContent = res.error; setStatus('error'); stateEl.textContent = 'Помилка'; }
    } catch (e) {
      msgEl.textContent = 'Не вдалося запустити перевірку';
    }
    poll();
  });

  // Memory cleaning: macOS itself shows the administrator password window
  const purgeBtn = document.getElementById('audit-purge');
  purgeBtn.addEventListener('click', async () => {
    purgeBtn.disabled = true;
    msgEl.textContent = 'Очікую пароль адміністратора…';
    try {
      const r = await window.pywebview.api.purge_memory();
      msgEl.textContent = r.ok
        ? `Вільна памʼять: ${r.freeBefore}% → ${r.freeAfter}%`
          + (r.memory && r.memory.delta ? `, доступно ${r.memory.delta.availableMb >= 0 ? '+' : ''}${r.memory.delta.availableMb} МБ` : '')
        : (r.error || 'Не вдалося очистити памʼять');
    } catch (e) {
      msgEl.textContent = 'Не вдалося очистити памʼять';
    }
    purgeBtn.disabled = false;
  });

  // First-run card: explains what macOS will ask, and disappears after “Done”
  const setupCard = async () => {
    let perm;
    try { perm = await window.pywebview.api.permissions(); } catch (e) { return; }
    if (!perm || perm.firstRunDone) return;
    const card = document.createElement('div');
    card.id = 'audit-setup';
    card.innerHTML = '<b>Перший запуск: доступи</b>'
      + '<p>Кожен пункт потрібен лише для своєї дії. Кнопка «Дозволити» викликає запит macOS.</p>'
      + perm.items.map(it => `<div class="row"><span>${it.title}<small>${it.note}</small></span>`
        + (it.action === 'request_finder' ? '<button type="button" data-req="finder">Дозволити</button>' : '')
        + '</div>').join('')
      + '<div class="row"><span></span><button type="button" id="audit-setup-done">Готово</button></div>';
    document.body.appendChild(card);
    card.querySelectorAll('[data-req="finder"]').forEach(b => b.addEventListener('click', async () => {
      b.disabled = true;
      const r = await window.pywebview.api.request_finder();
      b.textContent = r.ok ? 'Дозволено' : 'Не дозволено';
    }));
    document.getElementById('audit-setup-done').addEventListener('click', async () => {
      await window.pywebview.api.finish_setup();
      card.remove();
    });
  };

  // We start polling when the pywebview bridge is ready
  const bridgeReady = () => window.pywebview && window.pywebview.api
    && typeof window.pywebview.api.status === 'function'
    && typeof window.pywebview.api.run_check === 'function';

  const start = () => {
    if (polling) return;
    if (bridgeReady()) {
      polling = true;
      runBtn.disabled = false;
      purgeBtn.disabled = false;
      poll();
      setInterval(poll, 1000);
      setupCard();
    } else {
      setTimeout(start, 200);
    }
  };
  start();
})();
