/* ThermoFlow Sprint 3 — read-only boiler-cycle diagnostics from existing history API. */
(function(){
  const $=id=>document.getElementById(id);
  const fmt=(value,digits=1)=>value==null||!Number.isFinite(Number(value))?'Brak danych':Number(value).toLocaleString('pl-PL',{maximumFractionDigits:digits}) ;
  const date=value=>{if(!value)return '—';const parsed=new Date(value.replace(' ','T'));return Number.isNaN(parsed.getTime())?String(value):parsed.toLocaleString('pl-PL',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});};
  function kpi(label,value){const item=document.createElement('div');item.className='diagnostics-kpi';const name=document.createElement('span'),reading=document.createElement('strong');name.textContent=label;reading.textContent=value;item.append(name,reading);return item;}
  function event(title,detail,when){const item=document.createElement('article');item.className='diagnostics-event';const body=document.createElement('div'),heading=document.createElement('strong'),description=document.createElement('small'),time=document.createElement('time');heading.textContent=title;description.textContent=detail;time.textContent=when||'Czas niedostępny';body.append(heading,description);item.append(body,time);return item;}
  function render(payload){
    const state=$('diagnosticsHistoryState'),summary=$('diagnosticsSummary'),events=$('diagnosticsEvents'),analysis=payload?.analysis;
    summary.replaceChildren();events.replaceChildren();
    if(!payload?.configured){state.textContent='BRAK KONFIGURACJI';state.className='badge warn';events.append(event('Historia niedostępna',payload?.error||'Skonfiguruj kanały ThingSpeak.',''));return;}
    if(!payload?.connected||!analysis||analysis.error){state.textContent='BRAK DANYCH';state.className='badge warn';events.append(event('Nie udało się przeanalizować historii',analysis?.error||payload?.error||'Brak połączenia z ThingSpeak.',''));return;}
    const s=analysis.summary||{},runs=analysis.burner_runs||[],cycles=analysis.protection_cycles||[];
    state.textContent='HISTORIA DOSTĘPNA';state.className='badge good';
    summary.append(kpi('Uruchomienia palnika',fmt(analysis.burner_starts,0)),kpi('Czas pracy palnika',s.burner_minutes_total==null?'Brak danych':fmt(s.burner_minutes_total)+' min'),kpi('Cykle ochrony 4D',fmt(s.cycles,0)),kpi('Min. powrót w cyklach',s.min_return==null?'Brak danych':fmt(s.min_return)+' °C'));
    const records=[];
    runs.slice().reverse().forEach(run=>records.push({when:run.start,title:run.running?'Palnik pracuje — cykl otwarty':'Cykl pracy palnika',detail:`${fmt(run.minutes)} min · szczyt ${fmt(run.peak_kw)} kW`}));
    cycles.slice().reverse().forEach(cycle=>records.push({when:cycle.protection_start,title:'Cykl ochrony powrotu / zaworu 4D',detail:`Min. otwarcie ${fmt(cycle.min_opening,0)}% · przerwa do zadanej ${cycle.cold_gap_minutes==null?'niezamknięta':fmt(cycle.cold_gap_minutes)+' min'} · min. powrót ${cycle.min_return==null?'brak danych':fmt(cycle.min_return)+' °C'}`}));
    records.sort((a,b)=>String(b.when).localeCompare(String(a.when)));
    if(!records.length)events.append(event('Brak wykrytych cykli w tym zakresie','To nie oznacza awarii — analiza opiera się na zapisanych próbkach z ostatnich 24 godzin.',''));
    else records.slice(0,12).forEach(item=>events.append(event(item.title,item.detail,date(item.when))));
    $('diagnosticsHistoryMeta').textContent=`Źródło: ThingSpeak · zakres ${fmt(payload.range_hours)} h · pobrano ${date(payload.fetched_at)} · analiza wyłącznie informacyjna.`;
  }
  async function load(){try{const response=await ThermoFlowApi.get('/api/history?hours=24');const payload=await response.json();render(payload);}catch(_){render({configured:true,connected:false,error:'Nie udało się pobrać historii ThingSpeak.'});}}
  load();setInterval(load,60000);
})();
