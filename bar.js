// The run bar: added to the report page after loading (through pywebview.evaluate_js).
// Runs nothing itself. It only calls Api.run_check and polls Api.status.
(() => {
  if (document.getElementById('audit-bar')) return;

  const css = `
    #audit-bar { position:fixed; top:0; left:0; right:0; z-index:9999; display:flex; align-items:center; gap:12px;
      padding:10px 16px; background:#1f1f22; border-bottom:1px solid rgba(255,255,255,.08); color:#ededf0;
      font:14px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,Arial,sans-serif; }
    #audit-bar b { font-weight:800; }
    #audit-bar button { font:inherit; color:#eaf0f8; background:#2c2c30; border:1px solid #3a3a3f; border-radius:9px;
      padding:7px 14px; cursor:pointer; }
    #audit-bar button:disabled { opacity:.5; cursor:not-allowed; }
    #audit-state { font-weight:650; }
    #audit-msg { color:#acbbcd; font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; }
    #audit-state.running { color:#6fb5ff; } #audit-state.done { color:#4cd18b; } #audit-state.error { color:#ff6f7f; }
    #audit-steps { display:inline-flex; gap:4px; align-items:center; }
    #audit-steps i { display:block; width:22px; height:5px; border-radius:3px; background:#3a3a3f; }
    #audit-steps i.done { background:#4cd18b; } #audit-steps i.now { background:#6fb5ff; animation:audit-pulse 1s ease-in-out infinite; }
    @keyframes audit-pulse { 50% { opacity:.4; } }
    @media (prefers-reduced-motion:reduce) { #audit-steps i.now { animation:none; } }
    #audit-purge { margin-left:0; }
    #audit-setup { position:fixed; right:16px; bottom:16px; z-index:9999; width:340px; padding:14px 16px; border-radius:14px;
      background:#101d2d; color:#eaf0f8; border:1px solid #243851; box-shadow:0 18px 52px rgba(0,0,0,.35);
      font:13px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,Arial,sans-serif; }
    #audit-setup p { margin:6px 0 10px; color:#acbbcd; }
    #audit-setup .row { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:8px 0; border-top:1px solid #243851; }
    #audit-setup small { display:block; color:#acbbcd; font-size:12px; }
    #audit-setup button { font:inherit; color:#eaf0f8; background:#17304d; border:1px solid #243851; border-radius:8px; padding:6px 12px; cursor:pointer; }
    body { padding-top:56px !important; }
    .side { top:56px !important; height:calc(100vh - 56px) !important; }
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  const bar = document.createElement('div');
  bar.id = 'audit-bar';
  bar.innerHTML = '<button id="audit-run" type="button" disabled>Запустити перевірку</button>'
    + '<button id="audit-purge" type="button" disabled title="Потрібен пароль адміністратора при кожному виклику">Звільнити памʼять</button>'
    + '<span id="audit-state">Завантаження…</span><span id="audit-steps" hidden></span><span id="audit-msg"></span>';
  document.body.prepend(bar);

  const STATE = { idle: 'Готовий', running: 'Виконується', done: 'Завершено', error: 'Помилка' };
  const runBtn = document.getElementById('audit-run');
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

  const render = s => {
    stateEl.textContent = STATE[s.status] || s.status;
    stateEl.className = s.status;
    runBtn.disabled = s.status === 'running';
    let extra = '';
    if (s.started) {
      const end = s.finished || s.now;
      const sec = Math.max(0, Math.round(end - s.started));
      extra = ` · ${Math.floor(sec / 60)} хв ${String(sec % 60).padStart(2, '0')} с`;
    }
    const stage = renderSteps(s);
    msgEl.textContent = (stage || s.message || '') + extra;
  };

  const poll = async () => {
    try {
      render(await window.pywebview.api.status());
    } catch (e) {
      stateEl.textContent = 'Немає зв’язку із застосунком';
      stateEl.className = 'error';
    }
  };

  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    try {
      const res = await window.pywebview.api.run_check();
      if (!res.ok) { msgEl.textContent = res.error; stateEl.className = 'error'; stateEl.textContent = 'Помилка'; }
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
