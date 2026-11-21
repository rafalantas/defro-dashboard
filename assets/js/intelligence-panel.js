/* Read-only presentation of the unified intelligence contract. */
(() => {
  'use strict';
  const byId = id => document.getElementById(`intelligence${id}`);
  const live = byId('Live'), benchmarks = byId('Benchmarks'), status = byId('Status');
  const refresh = byId('Refresh'), results = byId('Results');
  if (!live || !benchmarks || !status || !refresh || !results) return;

  const modules = {
    health_score: ['Health Score', 'Ocena spalin i powrotu względem zakresu odniesienia.'],
    diagnostic_ai: ['Diagnostic AI', 'Hipotezy dotyczące odchyleń, nie diagnoza ani ocena bezpieczeństwa.'],
    burner_ml: ['Burner ML', 'Długość cykli palnika potwierdzonych sygnałem FIRE.', 'min',
      {candidate: 'Model', training_median: 'Mediana treningowa', previous_cycle: 'Poprzedni cykl'}],
    fuel_ml: ['Fuel ML', 'Zużycie peletu w zweryfikowanych okresach między dosypkami.', 'kg/d',
      {candidate: 'Model', as_issued_baseline: 'Zapisana prognoza bazowa', previous_period: 'Poprzedni okres'}],
    weather_impact: ['Weather Impact', 'Porównanie modeli na tych samych okresach z archiwalną prognozą pogody.', 'kg/d',
      {without_weather: 'Bez pogody', with_weather: 'Z pogodą'}]
  };
  const components = {flue: 'Spaliny', return: 'Powrót'};
  const observations = {within_baseline: 'w zakresie odniesienia', review_deviation: 'odchylenie do sprawdzenia'};
  const reasons = {
    not_configured: 'Nie skonfigurowano paczki benchmarków.',
    not_loaded: 'Paczka benchmarków jeszcze się nie wczytała.',
    missing_bundle: 'Brak pliku paczki benchmarków.',
    no_benchmark_report: 'Paczka nie zawiera raportu dla tego modułu.',
    insufficient_verified_samples: 'Za mało zweryfikowanych próbek do testu.',
    invalid_bundle: 'Nie udało się odczytać paczki benchmarków.',
    invalid_benchmark_report: 'Raport tego modułu jest nieprawidłowy.',
    no_usable_benchmark: 'Nie uzyskano wyniku, który można pokazać.',
    bundle_outside_freshness_window: 'Paczka wymaga ponownej ewaluacji.',
  };
  const states = {
    screened: 'Ocena dostępna', review: 'Do sprawdzenia',
    no_signal_in_observed_components: 'Bez sygnału odchylenia', evaluated: 'Test zakończony',
    collecting: 'Oczekuje na dane', stale: 'Nieaktualne', telemetry_unavailable: 'Brak telemetrii',
    unavailable: 'Niedostępne', unavailable_module: 'Brak raportu', loading: 'Pobieranie',
  };
  const number = new Intl.NumberFormat('pl-PL', {maximumFractionDigits: 2});
  const date = new Intl.DateTimeFormat('pl-PL', {dateStyle: 'short', timeStyle: 'medium'});
  let pending = false, expiryTimer;

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  const note = (card, text) => card.append(element('p', text, 'intelligence-context'));

  function timestamp(card, entry, offline) {
    if (!Number.isFinite(entry.evaluated_at)) return;
    const instant = new Date(entry.evaluated_at * 1000);
    if (!Number.isFinite(instant.getTime())) return;
    const line = element('p', offline ? 'Ewaluacja: ' : 'Ostatni odczyt: ', 'intelligence-context');
    const time = element('time', date.format(instant));
    time.dateTime = instant.toISOString();
    line.append(time);
    card.append(line);
  }

  function health(card, entry) {
    const report = entry.result;
    if (!report) return;
    if (entry.status === 'screened' && Number.isFinite(report.score)) {
      card.append(element('p', `${number.format(report.score)}%`, 'intelligence-score'));
      note(card, 'Składowe w zakresie odniesienia, nie procent sprawności kotła.');
    }
    const {usable, expected} = report.coverage || {};
    if (Number.isInteger(usable) && Number.isInteger(expected)) note(card, `Pokrycie: ${usable} z ${expected} składowych.`);
    const list = element('ul', undefined, 'intelligence-list');
    for (const [key, label] of Object.entries(components)) {
      const row = report.components?.[key];
      if (row) list.append(element('li', `${label}: ${observations[row.reason] || 'bez oceny'}.`));
    }
    if (list.childElementCount) card.append(list);
  }

  function diagnostic(card, entry, screens) {
    if (entry.status === 'no_signal_in_observed_components') {
      note(card, 'Nie wykryto odchylenia w ocenionych składowych. To nie potwierdza braku usterek.');
    } else if (entry.status === 'review') {
      const list = element('ul', undefined, 'intelligence-list');
      for (const [key, label] of Object.entries(components)) {
        const item = Array.isArray(entry.result?.items) &&
          entry.result.items.find(row => row?.component === key && row.evidence_ref === `/screens/${key}`);
        const evidence = item && screens?.[key]?.evidence;
        if (!evidence || !Number.isFinite(evidence.observed) || !Number.isFinite(evidence.median)) continue;
        list.append(element('li', `${label}: odczyt ${number.format(evidence.observed)} °C, mediana odniesienia ${number.format(evidence.median)} °C.`));
      }
      if (list.childElementCount) {
        card.append(list);
        note(card, 'Możliwe wyjaśnienia: zmiana warunków pracy albo różnica pomiaru. Przyczyna niepotwierdzona.');
      } else note(card, 'Szczegóły obserwacji niedostępne.');
    }
  }

  function metrics(card, entry, unit, variants) {
    const report = entry.result;
    if (Number.isInteger(report?.eligible_samples)) note(card, `Próby zakwalifikowane: ${report.eligible_samples}.`);
    if (entry.status !== 'evaluated' || !report?.holdout) return;
    const rows = Object.entries(variants).map(([key, label]) => [label, report.holdout[key]]);
    if (!rows.every(([, value]) => value && Number.isInteger(value.samples) &&
        Number.isFinite(value.mae) && Number.isFinite(value.rmse))) {
      note(card, 'Brak kompletnych metryk porównania.');
      return;
    }
    note(card, `Próby testowe na wariant: ${rows[0][1].samples}. Jednostka błędu: ${unit}.`);
    const table = element('table', undefined, 'intelligence-table');
    table.setAttribute('aria-label', 'Porównanie błędu modeli na próbach testowych');
    const header = table.createTHead().insertRow();
    for (const name of ['Wariant', 'MAE', 'RMSE']) {
      const th = element('th', name);
      th.scope = 'col';
      header.append(th);
    }
    const body = table.createTBody();
    for (const [name, values] of rows) {
      const row = body.insertRow();
      const th = element('th', name);
      th.scope = 'row';
      row.append(th, element('td', number.format(values.mae)), element('td', number.format(values.rmse)));
    }
    card.append(table);
  }

  function render(data, fallback, referenceNow = Date.now()) {
    clearTimeout(expiryTimer);
    live.replaceChildren();
    benchmarks.replaceChildren();
    let nextExpiry = Infinity;
    for (const [id, [title, description, unit, variants]] of Object.entries(modules)) {
      const offline = unit !== undefined;
      let entry = data?.modules[id] || {status: fallback};
      if (entry.status === 'stale') entry = {...entry, result: null};
      if (Number.isFinite(entry.evaluated_at) && entry.status !== 'stale') {
        const expires = entry.evaluated_at * 1000 + (offline ? 7 * 86400000 : 90000);
        if (referenceNow >= expires) {
          entry = {...entry, status: 'stale', result: null,
            reason: offline ? 'bundle_outside_freshness_window' : null};
        } else {
          nextExpiry = Math.min(nextExpiry, expires);
        }
      }
      const card = element('article', undefined, 'intelligence-module');
      card.dataset.module = id;
      const heading = element('div', undefined, 'intelligence-heading');
      const label = element('span', Object.hasOwn(states, entry.status) ? states[entry.status] : states.unavailable_module, 'intelligence-state');
      if (['screened', 'evaluated'].includes(entry.status)) label.dataset.tone = 'ready';
      heading.append(element('h4', title), label);
      card.append(heading);
      note(card, description);
      if (Object.hasOwn(reasons, entry.reason)) note(card, reasons[entry.reason]);
      else if (!offline && entry.status === 'collecting') note(card, 'Potrzeba więcej świeżych próbek z porównywalnych warunków pracy.');
      if (offline) metrics(card, entry, unit, variants);
      else if (id === 'health_score') health(card, entry);
      else diagnostic(card, entry, data?.screens);
      timestamp(card, entry, offline);
      (offline ? benchmarks : live).append(card);
    }
    if (Number.isFinite(nextExpiry)) {
      const start = performance.now();
      expiryTimer = setTimeout(() => render(data, fallback, referenceNow + performance.now() - start),
        Math.min(2147483647, nextExpiry - referenceNow + 1));
    }
  }

  async function load() {
    if (pending) return;
    pending = true;
    refresh.disabled = true;
    results.setAttribute('aria-busy', 'true');
    status.textContent = 'Pobieranie wyników…';
    render(null, 'loading');
    try {
      const response = await window.ThermoFlowApi.get('/api/thermoflow/v1/intelligence');
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      if (data?.schema !== 'thermoflow-unified-intelligence/1' || !data.modules ||
          typeof data.modules !== 'object' || Array.isArray(data.modules)) throw new Error('unknown schema');
      const serverNow = Date.parse(response.headers.get('Date'));
      render(data, 'unavailable_module', Number.isFinite(serverNow) ? serverNow : Date.now());
      status.textContent = 'Wyniki wyłącznie doradcze. Panel nie steruje kotłem.';
    } catch (_) {
      render(null, 'unavailable');
      status.textContent = 'Nie udało się pobrać wyników. Odśwież lub spróbuj później.';
    } finally {
      pending = false;
      refresh.disabled = false;
      results.removeAttribute('aria-busy');
    }
  }

  render(null, 'unavailable_module');
  refresh.addEventListener('click', load);
  window.addEventListener('thermoflow:view', event => {
    if (event.detail.view === 'diagnostics') load();
  });
})();
