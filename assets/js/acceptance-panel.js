/* Read-only production acceptance dashboard card. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const card = $('acceptanceCard'), status = $('acceptanceStatus');
  const summary = $('acceptanceSummary'), gates = $('acceptanceGates');
  const twin = $('acceptanceTwin');
  const refresh = $('acceptanceRefresh');
  if (!card || !status || !summary || !gates || !twin || !refresh || !window.ThermoFlowApi) return;

  const names = {
    telemetry: 'Telemetria obserwacyjna', digital_twin: 'Digital Twin', 'Burner ML': 'Burner ML',
    'Fuel ML': 'Fuel ML', 'Data quality': 'Jakość danych',
    'Model monitoring': 'Monitoring modeli', api_health: 'API health',
    intelligence_endpoint: 'Endpoint inteligencji', http_5xx: 'Błędy HTTP 5xx',
    frontend_smoke: 'Test interfejsu', desktop_mobile_review: 'Przegląd desktop / mobile',
    container_rollout: 'Wdrożenie kontenerów'
  };
  const stateNames = {
    ready: 'Zaliczone', shadow_ready: 'Gotowe do obserwacji', collecting: 'Zbieranie danych',
    blocked: 'Blokada', not_assessed: 'Nie oceniono'
  };
  const reportNames = {
    ready_for_manual_signoff: 'Gotowe do ręcznego odbioru',
    blocked: 'Wykryto blokady', incomplete: 'Odbiór niekompletny'
  };
  let pending = false;

  function node(tag, text, className) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (className) el.className = className;
    return el;
  }
  function renderTwin(report) {
    twin.replaceChildren(node('h3', 'Digital Twin · trend obserwacyjny'));
    if (!report || report.status !== 'observing' || !report.state) {
      const label = report?.status === 'stale' ? 'Telemetria nieaktualna' : 'Zbieranie zsynchronizowanych pomiarów';
      twin.append(node('p', label + '. Projekcja nie jest dostępna.', 'acceptance-empty'));
      return;
    }
    const grid = node('div', undefined, 'acceptance-twin-grid');
    for (const [key, label] of [['boiler_c','Kocioł'],['return_c','Powrót'],['flue_c','Spaliny']]) {
      if (!Number.isFinite(report.state[key])) continue;
      const metric = node('div', undefined, 'acceptance-metric');
      metric.append(node('span', label), node('strong', report.state[key].toFixed(1) + '°C'));
      grid.append(metric);
    }
    twin.append(grid);
    const forecasts = report.projections || {};
    const list = node('div', undefined, 'acceptance-twin-projections');
    for (const key of ['boiler_c','return_c','flue_c']) {
      const values = forecasts[key] || {};
      for (const horizon of ['5','15','30']) {
        const point = values[horizon];
        if (!point) continue;
        list.append(node('p', key.replace('_c','') + ' · ' + horizon + ' min: ' + point.value_c.toFixed(1) + '°C (' + point.range_low_c.toFixed(1) + '–' + point.range_high_c.toFixed(1) + '°C)'));
      }
    }
    twin.append(list);
    twin.append(node('p', 'Ekstrapolacja trendu pomiarów, nie symulacja fizyczna ani przedział prawdopodobieństwa.', 'acceptance-empty'));
  }
  function render(data) {
    renderTwin(data.digital_twin);

    const counts = data.summary || {};
    const state = data.status;
    status.replaceChildren(node('strong', reportNames[state] || 'Status nieznany',
      'acceptance-overall acceptance-' + (state === 'ready_for_manual_signoff' ? 'ready' : state === 'blocked' ? 'blocked' : 'pending')));
    summary.replaceChildren();
    for (const [label, key] of [['Zaliczone', 'passed'], ['Blokady', 'blockers'], ['Do uzupełnienia', 'outstanding']]) {
      const metric = node('div', undefined, 'acceptance-metric');
      metric.append(node('span', label), node('strong', Number.isInteger(counts[key]) ? String(counts[key]) : '—'));
      summary.append(metric);
    }
    gates.replaceChildren();
    if (!Array.isArray(data.gates) || !data.gates.length) {
      gates.append(node('p', 'API nie zwróciło listy bramek. Nie można ocenić gotowości.', 'acceptance-empty'));
      return;
    }
    for (const gate of data.gates) {
      const row = node('article', undefined, 'acceptance-gate');
      const heading = node('div', undefined, 'acceptance-gate-heading');
      heading.append(node('strong', names[gate.name] || gate.name || 'Nieznana bramka'));
      heading.append(node('span', stateNames[gate.status] || 'Nieznany status',
        'acceptance-state acceptance-state-' + (gate.status || 'unknown')));
      row.append(heading);
      if (gate.detail) row.append(node('p', gate.detail));
      gates.append(row);
    }
  }
  async function load() {
    if (pending) return;
    pending = true;
    refresh.disabled = true;
    status.textContent = 'Pobieram stan odbioru…';
    summary.replaceChildren();
    gates.replaceChildren();
    try {
      const response = await window.ThermoFlowApi.get('/api/thermoflow/v1/acceptance');
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const data = await response.json();
      if (data.schema !== 'thermoflow-production-acceptance/2' ||
          !['ready_for_manual_signoff', 'blocked', 'incomplete'].includes(data.status)) {
        throw new Error('Nieznany format raportu');
      }
      render(data);
    } catch (_) {
      status.replaceChildren(node('strong', 'Odbiór niedostępny', 'acceptance-overall acceptance-error'));
      summary.replaceChildren();
      gates.replaceChildren(node('p', 'Nie można pobrać raportu odbioru. Brak danych nie oznacza zaliczenia bramek. Spróbuj ponownie.', 'acceptance-empty'));
      twin.replaceChildren(node('h3', 'Digital Twin'), node('p', 'Raport niedostępny.', 'acceptance-empty'));
    } finally {
      pending = false;
      refresh.disabled = false;
    }
  }
  refresh.addEventListener('click', load);
  window.addEventListener('thermoflow:view', event => {
    if (event.detail?.view === 'diagnostics') load();
  });
  if (document.querySelector('[data-view-target="diagnostics"][aria-selected="true"]')) load();
})();
