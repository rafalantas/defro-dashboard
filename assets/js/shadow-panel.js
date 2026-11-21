/* Observe only the small prepared report; existing history is independent. */
(() => {
  'use strict';
  const status = document.getElementById('shadowStatus');
  const components = document.getElementById('shadowComponents');
  const refresh = document.getElementById('shadowRefresh');
  if (!status || !components || !refresh) return;
  const labels = {flue: 'Spaliny', return: 'Powrót'};
  const reasons = {within_baseline: 'w zakresie odniesienia', review_deviation: 'odchylenie do sprawdzenia',
    ambiguous_latest: 'brak wiarygodnego odczytu', insufficient_contiguous_history: 'za mało ciągłych próbek'};
  let pending = false;

  async function load() {
    if (pending) return;
    pending = true;
    refresh.disabled = true;
    status.textContent = 'Pobieram ocenę…';
    components.replaceChildren();
    components.hidden = true;
    try {
      const res = await window.ThermoFlowApi.get('/api/thermoflow/v1/shadow');
      if (!res.ok) throw new Error('shadow unavailable');
      const data = await res.json();
      if (data.schema !== 'thermoflow-shadow/1') throw new Error('unknown schema');
      const time = Number.isFinite(data.evaluated_at)
        ? ` Ocena z ${new Date(data.evaluated_at * 1000).toLocaleTimeString('pl-PL')}.` : '';
      const health = data.health;
      const messages = {collecting: 'Zbieranie próbek w stabilnej fazie pracy.',
        stale: 'Ostatnia ocena jest nieaktualna.', telemetry_unavailable: 'Brak wiarygodnej telemetrii.'};
      const summary = data.status === 'screened' && Number.isFinite(health?.score)
        ? `W zakresie odniesienia: ${health.coverage.usable ? health.score : 0}% składowych.` :
          (messages[data.status] || 'Ocena niedostępna.');
      const review = data.diagnostics?.status === 'review'
        ? ' Odchylenie może wynikać z warunków pracy lub pomiaru. To nie diagnoza.' : '';
      status.textContent = summary + time + review;
      for (const [key, label] of Object.entries(labels)) {
        const row = health?.components?.[key];
        if (!row) continue;
        const item = document.createElement('li');
        item.textContent = `${label}: ${reasons[row.reason] || 'bez oceny'}`;
        components.append(item);
      }
      components.hidden = !components.childElementCount;
    } catch (_) {
      status.textContent = 'Nie można pobrać oceny. Spróbuj ponownie.';
    } finally {
      refresh.disabled = false;
      pending = false;
    }
  }
  refresh.addEventListener('click', load);
  window.addEventListener('thermoflow:view', event => {
    if (event.detail.view === 'diagnostics') load();
  });
  if (document.querySelector('[data-view-target="diagnostics"][aria-selected="true"]')) load();
})();
