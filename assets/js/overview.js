/* ThermoFlow 2.0 — operational Overview, read-only projections of existing APIs. */
(function(){
  const $ = id => document.getElementById(id);
  const labels = {floor:'Pompa podłogowa',valve4d:'Pompa zaworu 4D',dhw:'Pompa CWU',circulation:'Pompa cyrkulacyjna'};
  const fmtTemp = value => value == null || !Number.isFinite(Number(value)) ? 'Brak odczytu' : Number(value).toLocaleString('pl-PL',{maximumFractionDigits:1})+' °C';
  const fmtNum = (value,digits=1) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('pl-PL',{minimumFractionDigits:digits,maximumFractionDigits:digits});
  const state = value => value === true ? 'Pracuje' : value === false ? 'Wyłączona' : 'Brak odczytu';
  function node(tag,className,text){const el=document.createElement(tag);if(className)el.className=className;if(text!=null)el.textContent=String(text);return el;}
  function row(parent,label,value,kind){const el=node('div','overview-row');el.append(node('span','overview-row-label',label),node('strong','overview-row-value '+(kind||''),value));parent.append(el);}
  function renderAlerts(payload,summary,ageSeconds){
    const root=$('overviewAlerts');root.replaceChildren();
    const alerts=[];
    if(!payload?.configured)alerts.push(['warn','Brak konfiguracji połączenia z eModul API.']);
    else if(!payload.connected)alerts.push(['danger',payload.error||'Brak połączenia ze sterownikiem eModul.']);
    else if(ageSeconds>90)alerts.push(['warn','Odczyt sterownika jest starszy niż 90 sekund.']);
    const ret=payload?.data?.valve?.return;
    if(payload?.connected&&ret!=null&&Number.isFinite(Number(ret))&&Number(ret)<54)alerts.push(['danger',`Temperatura powrotu ${fmtTemp(ret)} — poniżej dolnej granicy 54 °C zakresu ochrony 55 °C ±1 °C.`]);
    if(payload?.connected&&ret==null)alerts.push(['warn','Brak odczytu temperatury powrotu — nie można ocenić warunków ochrony.']);
    if(summary?.hopper?.below_reserve===true)alerts.push(['danger','Szacowany zapas pelletu osiągnął zalecaną rezerwę zasobnika.']);
    if(!alerts.length)alerts.push(['good','Brak wykrytych ostrzeżeń na podstawie dostępnych odczytów.']);
    for(const [kind,message] of alerts)root.append(node('div','overview-alert '+kind,message));
  }
  function renderStatus(payload){
    const data=payload?.data||{},t=data.temperatures||{},v=data.valve||{},fuel=data.fuel||{},p=data.pumps||{};
    const fetched=payload?.fetched_at?new Date(payload.fetched_at):null;
    const age=fetched&&!Number.isNaN(fetched.getTime())?Math.max(0,(Date.now()-fetched.getTime())/1000):Infinity;
    const connected=payload?.configured===true&&payload?.connected===true;
    const stale=connected&&age>90;
    const badge=$('overviewConnection');
    badge.textContent=!payload?.configured?'API NIE SKONFIGUROWANE':!payload?.connected?'API NIEDOSTĘPNE':stale?'ODCZYT NIEAKTUALNY':'DANE ZE STEROWNIKA';
    badge.className='badge '+(connected&&!stale?'good':'warn');
    $('overviewCo').textContent=fmtTemp(t.co);
    $('overviewReturn').textContent=fmtTemp(v.return);
    $('overviewOutdoor').textContent=fmtTemp(t.outdoor);
    $('overviewFlue').textContent=fmtTemp(t.flue);
    $('overviewFuel').textContent=fuel.percent==null?'Brak odczytu':fmtNum(fuel.percent,0)+' %';
    $('overviewBoilerState').textContent=data.controller_status||'Brak odczytu';
    $('overviewUpdated').textContent=fetched&&!Number.isNaN(fetched.getTime())?'Ostatni odczyt: '+fetched.toLocaleString('pl-PL'):'Brak znacznika czasu odczytu';
    const pumpRoot=$('overviewPumps');pumpRoot.replaceChildren();
    for(const [key,label] of Object.entries(labels))row(pumpRoot,label,state(p[key]),p[key]==null?'unknown':p[key]?'active':'inactive');
    row(pumpRoot,'Otwarcie zaworu 4D',v.opening==null?'Brak odczytu':fmtNum(v.opening,0)+' %',v.opening==null?'unknown':'');
    row(pumpRoot,'Temperatura bieżąca zaworu 4D',fmtTemp(v.current),v.current==null?'unknown':'');
    row(pumpRoot,'Temperatura zadana zaworu 4D',fmtTemp(v.set),v.set==null?'unknown':'');
    row(pumpRoot,'Płomień',data.fire==null?'Brak odczytu':data.fire?'Wykryty':'Niewykryty',data.fire==null?'unknown':data.fire?'active':'inactive');
    renderAlerts(payload,window.ThermoFlowFuelSummary,age);
  }
  function renderFuel(summary){
    const root=$('overviewFuelModels');root.replaceChildren();
    if(!summary){root.append(node('p','overview-muted','Oczekiwanie na podsumowanie paliwa.'));return;}
    if(summary.configured===false){root.append(node('p','overview-muted','Kanał paliwa nie jest skonfigurowany.'));return;}
    if(summary.warming){root.append(node('p','overview-muted','Trwa przygotowywanie podsumowania paliwa…'));return;}
    const refill=node('section','overview-model');
    refill.append(node('h4','','Model domu z dosypek'));
    if(summary.model){
      const m=summary.model;
      row(refill,'Dopasowanie',`${m.periods_used??'—'} okresów`);
      row(refill,'Zużycie zależne od HDD',m.kg_per_hdd==null?'—':fmtNum(m.kg_per_hdd,2)+' kg/HDD');
      row(refill,'Stała część',m.base_kg_per_day==null?'—':fmtNum(m.base_kg_per_day,1)+' kg/dobę');
      row(refill,'Temperatura bazowa',m.balance_temp==null?'—':fmtNum(m.balance_temp,1)+' °C');
    }else refill.append(node('p','overview-muted','Model niedopasowany — potrzeba co najmniej 4 prawidłowych okresów między dosypkami.'));
    const daily=node('section','overview-model');
    daily.append(node('h4','','Model dobowy'));
    const dm=summary.daily_model;
    if(dm?.status==='aktywny'){
      row(daily,'Stan','Aktywny','active');
      row(daily,'Dni uczące',dm.days??'—');
      row(daily,'Czynniki',Array.isArray(dm.terms)?dm.terms.join(' + '):'—');
      row(daily,'Błąd LOO',dm.rmse_loo_kg_per_day==null?'—':fmtNum(dm.rmse_loo_kg_per_day,1)+' kg/dobę');
    }else if(dm?.status==='zbiera dane'){
      row(daily,'Stan','Zbiera dane','unknown');
      row(daily,'Zebrane pełne doby',dm.days??0);
      row(daily,'Wymagane doby',dm.days_needed??'—');
    }else daily.append(node('p','overview-muted','Status modelu dobowego niedostępny.'));
    const hopper=node('section','overview-model');
    hopper.append(node('h4','','Zapas w zasobniku — model'));
    if(summary.hopper){
      const h=summary.hopper;
      row(hopper,'Szacowany zapas',h.remaining_kg==null?'—':fmtNum(h.remaining_kg,1)+' kg');
      row(hopper,'Poziom',h.remaining_percent==null?'—':fmtNum(h.remaining_percent,0)+' %');
      row(hopper,'Czas do rezerwy',h.days_to_reserve==null?'—':fmtNum(h.days_to_reserve,1)+' dni');
      row(hopper,'Metoda',h.method||'Brak informacji');
    }else hopper.append(node('p','overview-muted','Brak danych do oszacowania zapasu z dosypek i licznika.'));
    if(summary.last7)row(hopper,'Zużycie z ostatnich pełnych dób',fmtNum(summary.last7.kg_per_day,1)+' kg/dobę');
    root.append(refill,daily,hopper);
    renderAlerts(window.ThermoFlowStatus,summary,window.ThermoFlowStatus?.fetched_at?Math.max(0,(Date.now()-new Date(window.ThermoFlowStatus.fetched_at).getTime())/1000):Infinity);
  }
  window.addEventListener('thermoflow:status',event=>{window.ThermoFlowStatus=event.detail;renderStatus(event.detail);});
  window.addEventListener('thermoflow:fuel',event=>{window.ThermoFlowFuelSummary=event.detail;renderFuel(event.detail);});
  if(window.ThermoFlowStatus)renderStatus(window.ThermoFlowStatus);
  if(window.ThermoFlowFuelSummary)renderFuel(window.ThermoFlowFuelSummary);
  if(!window.ThermoFlowStatus)$('overviewUpdated').textContent='Oczekiwanie na pierwszy odczyt sterownika…';
  if(!window.ThermoFlowFuelSummary)renderFuel(null);
})();
