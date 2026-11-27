/* Fibaro read-only dashboard: consumes the SQLite archive, never writes to HC3. */
(function(){
  'use strict';
  const root=document.getElementById('fibaroPanel');
  if(!root)return;
  const $=id=>document.getElementById(id);
  let payload=null, selectedZone=null, days=14, loading=false;
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number=value=>value==null||!Number.isFinite(Number(value))?'—':Number(value).toLocaleString('pl-PL',{minimumFractionDigits:1,maximumFractionDigits:1});
  const date=value=>new Date(value*1000).toLocaleString('pl-PL',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  function setStatus(text,tone='warn'){const el=$('fibaroStatus');el.textContent=text;el.className='badge '+tone;}
  function renderSummary(){
    const zones=payload?.zones||[], valid=zones.filter(z=>z.temperature!=null), mean=valid.length?valid.reduce((sum,z)=>sum+z.temperature,0)/valid.length:null;
    const deltas=zones.filter(z=>z.temperature!=null&&(z.setpoint??z.manual_setpoint)!=null);
    const avgDelta=deltas.length?deltas.reduce((sum,z)=>sum+z.temperature-(z.setpoint??z.manual_setpoint),0)/deltas.length:null;
    $('fibaroSummary').innerHTML=[
      ['Strefy',zones.length],['Odczyty temperatury',valid.length+'/'+zones.length],
      ['Średnia temperatura',mean==null?'—':number(mean)+'°C'],
      ['Średnie odchylenie',avgDelta==null?'—':(avgDelta>0?'+':'')+number(avgDelta)+'°C']
    ].map(([label,value])=>'<div class="overview-metric"><span>'+esc(label)+'</span><strong>'+esc(value)+'</strong></div>').join('');
  }
  function renderZones(){
    const zones=payload?.zones||[];
    if(!zones.length){$('fibaroZones').innerHTML='<p class="overview-muted">Brak zapisanych stref w ostatnim snapshotcie. Sprawdź, czy kolektor Fibaro działa i zapisał snapshot.</p>';return;}
    $('fibaroZones').innerHTML=zones.map(zone=>{
      const target=zone.setpoint??zone.manual_setpoint;
      const delta=zone.temperature!=null&&target!=null?zone.temperature-target:null;
      const heads=zone.heads||[], offline=heads.filter(h=>h.dead===true).length, low=heads.filter(h=>h.battery!=null&&h.battery<=20).length;
      const flags=[offline?offline+' offline':null,low?low+' słaba bateria':null].filter(Boolean).join(' · ');
      return '<button type="button" class="fibaro-zone-card'+(selectedZone===zone.id?' selected':'')+'" data-zone-id="'+zone.id+'" aria-pressed="'+(selectedZone===zone.id)+'">'+
        '<div class="fibaro-zone-top"><strong>'+esc(zone.name)+'</strong><span>'+heads.length+' głow.</span></div>'+
        '<div class="fibaro-zone-reading"><strong>'+(zone.temperature==null?'—':number(zone.temperature)+'°')+'</strong><span>'+(target==null?'nastawa —':'cel '+number(target)+'°')+'</span></div>'+
        '<div class="fibaro-zone-track"><i style="width:'+Math.max(0,Math.min(100,zone.temperature==null||target==null?0:(zone.temperature/Math.max(target,1))*100))+'%"></i></div>'+
        '<div class="fibaro-zone-foot"><span class="'+(delta==null?'muted':Math.abs(delta)<=0.5?'good':delta<0?'cool':'warm')+'">'+(delta==null?'brak porównania':(delta>0?'+':'')+number(delta)+'° od celu')+'</span><small>'+esc(flags|| (zone.active===false?'nieaktywna':'stan głowic'))+'</small></div></button>';
    }).join('');
    $('fibaroZones').querySelectorAll('[data-zone-id]').forEach(button=>button.addEventListener('click',()=>{selectedZone=Number(button.dataset.zoneId);renderZones();renderHistory();}));
    if(selectedZone==null||!zones.some(z=>z.id===selectedZone))selectedZone=zones[0]?.id??null;
  }
  function renderHistory(){
    const zone=(payload?.zones||[]).find(z=>z.id===selectedZone), cutoff=payload.generated_at-days*86400, events=(payload?.history||[]).filter(e=>e.zone_id===selectedZone&&e.timestamp>=cutoff);
    $('fibaroHistoryTitle').textContent=zone?'Historia temperatury · '+zone.name:'Historia temperatury stref';
    if(!events.length){$('fibaroChart').innerHTML='<div class="fibaro-chart-empty">Brak zdarzeń zmiany temperatury tej strefy w wybranym okresie. HC3 zapisuje zmiany zdarzeniowo — brak punktów nie oznacza braku próbek ani stałej temperatury.</div>';return;}
    const values=events.map(e=>e.value), target=zone?.setpoint??zone?.manual_setpoint, min=Math.min(...values,...(target==null?[]:[target]))-0.6,max=Math.max(...values,...(target==null?[]:[target]))+0.6,span=Math.max(.2,max-min);
    const x=i=>24+(i/Math.max(1,events.length-1))*672,y=v=>176-((v-min)/span)*142;
    const points=events.map((e,i)=>x(i).toFixed(1)+','+y(e.value).toFixed(1)).join(' ');
    const ticks=[min,(min+max)/2,max], grid=ticks.map(v=>'<line x1="24" x2="696" y1="'+y(v)+'" y2="'+y(v)+'" stroke="var(--line)" stroke-dasharray="3 5"/><text x="20" y="'+(y(v)-5)+'" text-anchor="end" fill="var(--muted)" font-size="10">'+number(v)+'</text>').join('');
    const targetLine=target==null?'':'<line x1="24" x2="696" y1="'+y(target)+'" y2="'+y(target)+'" stroke="#e5b85c" stroke-dasharray="6 5"/><text x="692" y="'+(y(target)-6)+'" text-anchor="end" fill="#e5b85c" font-size="10">nastawa teraz '+number(target)+'°</text>';
    $('fibaroChart').innerHTML='<svg viewBox="0 0 720 210" role="img" aria-label="Zarejestrowane zmiany temperatury strefy">'+grid+targetLine+'<polyline points="'+points+'" fill="none" stroke="var(--teal)" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>'+events.map((e,i)=>'<circle cx="'+x(i)+'" cy="'+y(e.value)+'" r="3.5" fill="var(--teal)"><title>'+esc(date(e.timestamp)+' · '+number(e.value)+'°C')+'</title></circle>').join('')+'</svg><div class="fibaro-chart-meta"><span>'+esc(date(events[0].timestamp))+'</span><span>'+events.length+' zdarzeń · bez interpolacji</span><span>'+esc(date(events[events.length-1].timestamp))+'</span></div>';
  }
  function renderHeads(){
    const zones=payload?.zones||[], heads=zones.flatMap(z=>(z.heads||[]).map(h=>({...h,zone:z.name})));
    const online=heads.filter(h=>h.dead===false).length, offline=heads.filter(h=>h.dead===true).length, unknown=heads.length-online-offline;
    $('fibaroHeadSummary').innerHTML='<div class="overview-row"><span class="overview-row-label">Głowice online</span><b class="overview-row-value active">'+online+'</b></div><div class="overview-row"><span class="overview-row-label">Offline</span><b class="overview-row-value '+(offline?'unknown':'active')+'">'+offline+'</b></div><div class="overview-row"><span class="overview-row-label">Stan nieznany</span><b class="overview-row-value">'+unknown+'</b></div>';
    $('fibaroHeadList').innerHTML=heads.length?heads.map(h=>'<div class="fibaro-head-row"><span><strong>'+esc(h.name||('Głowica '+h.id))+'</strong><small>'+esc(h.zone)+'</small></span><span>'+ (h.battery==null?'bateria —':number(h.battery)+'%')+'</span><span class="'+(h.dead===true?'warm':h.dead===false?'good':'muted')+'">'+(h.dead===true?'offline':h.dead===false?'online':'brak statusu')+'</span></div>').join(''):'<p class="overview-muted">Brak głowic w snapshotcie.</p>';
  }
  function render(){
    if(!payload)return;
    const snap=payload.snapshot||{}, age=snap.age_seconds;
    setStatus(snap.freshness==='fresh'?'SNAPSHOT AKTUALNY':snap.freshness==='stale'?'SNAPSHOT NIEAKTUALNY':'BRAK SNAPSHOTU',snap.freshness==='fresh'?'live':'warn');
    $('fibaroUpdated').textContent=snap.captured_at?'Ostatni snapshot: '+date(snap.captured_at)+' · '+(age<60?Math.round(age)+' s temu':Math.round(age/60)+' min temu'):'Brak zapisanego snapshotu';
    const cutoff=payload.generated_at-days*86400, visibleCount=payload.history.filter(e=>e.timestamp>=cutoff).length;
    $('fibaroWindowMeta').textContent='Zakres '+days+' dni · '+visibleCount+' zdarzeń zmian temperatury · źródło: lokalna baza SQLite';
    renderSummary();renderZones();renderHistory();renderHeads();
  }
  /* Cold zones and why: rule-based, advisory, from /api/fibaro/comfort */
  let comfort=null, comfortHours=24, comfortLoading=false;
  const STATUS_LABEL={cold:'ZIMNO',cooling:'DOGRZEWA SIĘ',ok:'OK',warm:'PRZEGRZANIE',unknown:'BRAK DANYCH'};
  const STATUS_ORDER={cold:0,cooling:1,warm:2,unknown:3,ok:4};
  const signed=value=>value==null?'—':(value>0?'+':'')+number(value);
  function episodeHtml(ep,kind){
    const range=esc(date(ep.start)+' – '+date(ep.end)), tail=ep.ongoing?' · trwa':'';
    if(kind==='warm')return '<div class="comfort-episode"><strong>'+range+'</strong> · '+ep.duration_minutes+' min'+tail+' · maks. +'+number(ep.worst_gap_k)+' K ponad nastawę<ul><li>'+esc(ep.note||'')+'</li></ul></div>';
    const causes=[ep.primary_cause,...(ep.other_causes||[])].map((c,i)=>'<li>'+(i===0?'<b>':'')+esc(c.label)+(i===0?'</b>':'')+' · pewność: '+esc(c.confidence)+(c.evidence&&c.evidence.length?'<br>'+c.evidence.map(esc).join('<br>'):'')+'</li>').join('');
    return '<div class="comfort-episode"><strong>'+range+'</strong> · '+ep.duration_minutes+' min'+tail+' · maks. −'+number(ep.worst_gap_k)+' K od nastawy'+(ep.started_before_window?' · zaczął się przed oknem':'')+'<ul>'+causes+'</ul></div>';
  }
  function renderComfort(){
    const banner=$('comfortBanner'), list=$('comfortList');
    if(!comfort){return;}
    const s=comfort.summary, cov=comfort.coverage, th=comfort.thresholds;
    $('comfortMeta').textContent='Okno '+comfortHours+' h · '+cov.samples+' odczytów co ok. 5 min · zimno = ≥'+number(th.cold_gap_k)+' K poniżej nastawy przez ≥'+th.episode_min_minutes+' min';
    let tone='', text='';
    if(!cov.fresh){tone='';text='Brak świeżych odczytów kolektora Fibaro, więc stan stref jest nieznany. Sprawdź kolektor.';}
    else if(s.cold_now.length){tone='cold';text='<b>Teraz zimno:</b> '+s.cold_now.map(esc).join(', ')+'.';}
    else if(s.cooling_now.length){tone='';text='<b>Dogrzewa się</b> (poniżej nastawy krócej niż '+th.episode_min_minutes+' min): '+s.cooling_now.map(esc).join(', ')+'.';}
    else{tone='ok';text='Żadna strefa nie jest teraz poniżej nastawy.';}
    if(cov.fresh&&s.warm_now.length)text+=' <span class="overview-muted">Przegrzane (≥'+number(th.warm_gap_k)+' K ponad nastawę): '+s.warm_now.map(esc).join(', ')+'.</span>';
    banner.className='comfort-banner'+(tone?' '+tone:'');banner.innerHTML=text;
    const zones=[...comfort.zones].sort((a,b)=>(STATUS_ORDER[a.status]-STATUS_ORDER[b.status])||a.name.localeCompare(b.name,'pl'));
    list.innerHTML=zones.length?zones.map(z=>{
      const cur=z.current||{}, last=z.cold_episodes[z.cold_episodes.length-1];
      const reading=cur.temperature==null?'brak odczytu':number(cur.temperature)+'° · cel '+(cur.setpoint==null?'—':number(cur.setpoint)+'°')+' · '+signed(cur.gap_k==null?null:-cur.gap_k)+' K'+(cur.heat_required===true?' · grzeje':cur.heat_required===false?' · bez zapotrzebowania':'');
      const cause=last?esc(last.primary_cause.label)+'<small>ostatni epizod: '+esc(date(last.start))+' · '+last.duration_minutes+' min · pewność: '+esc(last.primary_cause.confidence)+'</small>':'Brak epizodów zimna w oknie';
      const mins=z.cold_minutes?'zimno '+z.cold_minutes+' min':(z.warm_minutes?'ciepło '+z.warm_minutes+' min':'—');
      const detail=[...z.cold_episodes].reverse().map(ep=>episodeHtml(ep,'cold')).join('')+[...z.warm_episodes].reverse().map(ep=>episodeHtml(ep,'warm')).join('')||'<p class="overview-muted">Brak epizodów w wybranym oknie.</p>';
      const invalid=z.invalid_temperature_samples?'<p class="overview-muted">Pominięto '+z.invalid_temperature_samples+' odczytów temperatury poza zakresem (np. 0 °C z zaniku łączności).</p>':'';
      return '<details class="comfort-row"><summary><span class="comfort-name">'+esc(z.name)+'<small>'+esc(reading)+'</small></span><span class="comfort-chip '+esc(z.status)+'">'+esc(STATUS_LABEL[z.status]||z.status)+'</span><span class="comfort-cause">'+cause+'</span><span class="comfort-mins">'+esc(mins)+'</span></summary><div class="comfort-detail">'+detail+invalid+'</div></details>';
    }).join(''):'<p class="overview-muted">Brak stref w zapisanych odczytach.</p>';
    $('comfortNote').textContent=(comfort.limitations||[]).join(' ');
  }
  async function loadComfort(){
    if(comfortLoading)return;comfortLoading=true;
    try{
      const response=await ThermoFlowApi.get('/api/fibaro/comfort?hours='+comfortHours);
      const data=await response.json();
      if(!response.ok)throw new Error(data.error||'Nie udało się policzyć stanu stref');
      comfort=data;renderComfort();
    }catch(error){
      $('comfortBanner').className='comfort-banner';$('comfortBanner').textContent=error.message||'Nie udało się pobrać analizy zimna.';
      $('comfortList').innerHTML='';
    }finally{comfortLoading=false;}
  }
  document.querySelectorAll('[data-comfort-hours]').forEach(button=>button.addEventListener('click',()=>{
    comfortHours=Number(button.dataset.comfortHours);
    document.querySelectorAll('[data-comfort-hours]').forEach(item=>{const active=item===button;item.classList.toggle('active',active);item.setAttribute('aria-pressed',String(active));});
    loadComfort();
  }));
  async function load(){
    if(loading)return;loading=true;setStatus('ŁADOWANIE…');
    loadComfort();
    try{
      const response=await ThermoFlowApi.get('/api/fibaro/dashboard?days=14');
      const data=await response.json();
      if(!response.ok)throw new Error(data.error||'Nie udało się odczytać historii Fibaro');
      payload=data;days=14;render();
    }catch(error){
      setStatus('BRAK DANYCH','warn');
      $('fibaroZones').innerHTML='<p class="overview-muted">'+esc(error.message||'Nie udało się pobrać danych z lokalnej bazy.')+'</p>';
      $('fibaroUpdated').textContent='Sprawdź bazę SQLite i działanie kolektora Fibaro.';
    }finally{loading=false;}
  }
  document.querySelectorAll('[data-fibaro-range]').forEach(button=>button.addEventListener('click',()=>{
    const range=Number(button.dataset.fibaroRange);days=range;
    document.querySelectorAll('[data-fibaro-range]').forEach(item=>{const active=item===button;item.classList.toggle('active',active);item.setAttribute('aria-pressed',String(active));});
    if(payload)render();
  }));
  $('fibaroRefresh').addEventListener('click',load);
  window.addEventListener('thermoflow:view',event=>{if(event.detail?.view==='fibaro'&&!payload)load();});
  if(document.querySelector('[data-view-target="fibaro"].active'))load();
})();
