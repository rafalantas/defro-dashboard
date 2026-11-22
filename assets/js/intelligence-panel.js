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

  function livePrediction(card, moduleId, prediction) {
    if (!prediction) return;
    const labels = {predicted: 'Prognoza live', collecting: 'Zbieranie danych', stale: 'Dane nieaktualne', unavailable: 'Model niedostępny'};
    const reasons = {
      not_configured: 'Nie skonfigurowano ścieżki artefaktu modelu.',
      not_loaded: 'Model nie został jeszcze wczytany.',
      missing_artifact: 'Brak pliku modelu.',
      invalid_or_stale_artifact: 'Artefakt modelu jest nieprawidłowy lub przeterminowany.',
      waiting_for_previous_complete_cycle_and_model: 'Prognoza pojawi się po zarejestrowaniu pełnego cyklu i rozpoczęciu następnego.',
      waiting_for_next_fire_cycle: 'Oczekiwanie na następny cykl FIRE.',
      fire_telemetry_stale: 'Odczyt FIRE jest zbyt stary.',
      fuel_baseline_or_fresh_summary_unavailable: 'Brak świeżej prognozy bazowej lub zweryfikowanego okresu paliwowego.'
    };
    const box = element('div', undefined, 'intelligence-live-prediction');
    box.append(element('strong', labels[prediction.status] || 'Prognoza niedostępna'));
    const value = prediction.value ?? prediction.prediction;
    if (prediction.status === 'predicted' && Number.isFinite(value)) {
      const unit = prediction.unit || (moduleId === 'burner_ml' ? 'min' : 'kg/d');
      box.append(element('p', number.format(value) + ' ' + unit, 'intelligence-live-value'));
      if (moduleId === 'burner_ml' && Number.isFinite(prediction.elapsed_minutes)) {
        note(box, 'Bieżący cykl trwa ' + number.format(prediction.elapsed_minutes) + ' min. To przewidywany czas całego cyklu, nie pozostały czas.');
      }
      if (moduleId === 'fuel_ml') {
        if (Number.isFinite(prediction.baseline_kg_per_day)) note(box, 'Model bazowy: ' + number.format(prediction.baseline_kg_per_day) + ' kg/d.');
        if (Number.isFinite(prediction.previous_kg_per_day)) note(box, 'Ostatni zweryfikowany okres: ' + number.format(prediction.previous_kg_per_day) + ' kg/d.');
      }
      note(box, 'Wynik doradczy. Nie steruje kotłem ani nie zmienia szacunku zapasu.');
    } else {
      note(box, reasons[prediction.reason] || 'Brak wystarczających świeżych danych do prognozy.');
    }
    card.append(box);
  }
  function weatherForecast(card, forecast) {
    const box = element('section', undefined, 'intelligence-weather-forecast');
    box.append(element('strong', 'Prognoza peletu zależna od pogody'));
    const reasons = {
      forecast_unavailable: 'Prognoza pogodowa lub model paliwowy nie są jeszcze dostępne.',
      invalid_forecast_timestamp: 'Brak wiarygodnego czasu wygenerowania prognozy.',
      fuel_summary_stale: 'Dane prognozy są nieaktualne — odświeżenie silnika paliwowego jest potrzebne.',
      invalid_forecast: 'Dane prognozy mają nieprawidłowy format.',
      no_valid_forecast_days: 'Brak poprawnych dni prognozy.'
    };
    if (!forecast || forecast.status !== 'ready' || !forecast.week) {
      const state = forecast?.status === 'stale' ? 'Prognoza nieaktualna' :
        forecast?.status === 'unavailable' ? 'Prognoza niedostępna' : 'Zbieranie danych';
      box.append(element('p', state, 'intelligence-weather-state'));
      note(box, reasons[forecast?.reason] || reasons.forecast_unavailable);
      card.append(box);
      return;
    }
    const total = forecast.week.kg;
    const average = forecast.week.average_kg_per_day;
    box.append(element('p', number.format(total) + ' kg / ' + forecast.week.days + ' dni',
      'intelligence-weather-total'));
    note(box, 'Średnio ' + number.format(average) + ' kg/d. ' +
      (forecast.source || 'Prognoza Open-Meteo i model paliwowy.'));
    const model = forecast.model || {};
    const modelNames = {
      weather_daily_model: 'model dobowy',
      temperature_hdd_house_model: 'model budynku oparty na stopniodniach',
      unavailable: 'model niedostępny'
    };
    const termNames = {
      hdd: 'temperatura zewnętrzna', hdd_lag: 'bezwładność budynku',
      solar: 'promieniowanie słoneczne', wind_hdd: 'wiatr',
      wind_hdd_lag: 'wiatr przy uwzględnieniu bezwładności'
    };
    const terms = Array.isArray(model.terms) ? model.terms.map(term => termNames[term]).filter(Boolean) : [];
    const detail = model.kind === 'weather_daily_model' && terms.length ? ': ' + terms.join(', ') : '';
    note(box, 'Podstawa: ' + (modelNames[model.kind] || 'model paliwowy') + detail +
      (Number.isInteger(model.training_days) ? ' (' + model.training_days + ' dób treningowych).' : '.'));
    const list = element('ul', undefined, 'intelligence-weather-days');
    for (const day of forecast.days) {
      const details = [number.format(day.t_mean) + ' °C'];
      if (Number.isFinite(day.solar_mj)) details.push('słońce ' + number.format(day.solar_mj) + ' MJ/m²');
      if (Number.isFinite(day.wind_max_kmh)) details.push('wiatr ' + number.format(day.wind_max_kmh) + ' km/h');
      list.append(element('li', day.day + ': ' + number.format(day.kg) + ' kg — ' + details.join(', ')));
    }
    box.append(list);
    if (Number.isFinite(model.loo_rmse_kg_per_day)) {
      note(box, 'Błąd LOO modelu na danych historycznych: ' +
        number.format(model.loo_rmse_kg_per_day) + ' kg/d — to miara błędu, nie gwarantowany przedział prognozy.');
    }
    note(box, 'Prognoza doradcza. Nie zmienia nastaw kotła ani istniejącego rozliczenia paliwa.');
    card.append(box);
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
      if (offline) {
        metrics(card, entry, unit, variants);
        if (id === 'burner_ml' || id === 'fuel_ml') livePrediction(card, id, data?.live_predictions?.[id]);
        if (id === 'weather_impact') weatherForecast(card, data?.weather_forecast);
      } else if (id === 'health_score') health(card, entry);
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
