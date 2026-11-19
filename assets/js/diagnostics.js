/* ThermoFlow Sprint 5 — quality-aware, read-only diagnostics derived from ThingSpeak history. */
(function(){
  const $=id=>document.getElementById(id);
  const fmt=(v,d=1)=>v==null||!Number.isFinite(Number(v))?'Brak danych':Number(v).toLocaleString('pl-PL',{maximumFractionDigits:d});
  const when=t=>{if(!t)return '—';const d=new Date(Number(t)*1000);return Number.isNaN(d.getTime())?'—':d.toLocaleString('pl-PL',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});};
  const ids={co:'thermoflow-co',flue:'thermoflow-flue',power:'thermoflow-boiler-power',ret:'thermoflow-valve4d-return',valve:'thermoflow-valve4d-current',opening:'thermoflow-valve4d-opening',setpoint:'thermoflow-valve4d-effective-setpoint',floor:'thermoflow-pump-floor',dhw:'thermoflow-pump-dhw',rad:'thermoflow-pump-valve4d'};
  function points(payload,key){const item=(payload.series||[]).find(s=>s.id===ids[key]);return (item?.points||[]).filter(p=>Array.isArray(p)&&Number.isFinite(Number(p[0]))&&Number.isFinite(Number(p[1]))).map(p=>[Number(p[0]),Number(p[1])]).sort((a,b)=>a[0]-b[0]);}
  function analysisSettings(payload){return payload?.analysis_config&&typeof payload.analysis_config==='object'?payload.analysis_config:null;}
  function quality(data,maxGapSeconds){let maxGap=0,gaps=0,intervals=[];for(let i=1;i<data.length;i++){const gap=data[i][0]-data[i-1][0];if(gap<=0)continue;intervals.push(gap);if(gap>maxGap)maxGap=gap;if(maxGapSeconds!=null&&gap>maxGapSeconds)gaps++;}intervals.sort((a,b)=>a-b);const middle=Math.floor(intervals.length/2),median=intervals.length?(intervals.length%2?intervals[middle]:(intervals[middle-1]+intervals[middle])/2):null;return {count:data.length,maxGap,gaps,median,from:data[0]?.[0]??null,to:data[data.length-1]?.[0]??null};}
  function spans(data,predicate,maxGapSeconds){let total=0,episodes=[],open=null,previous=null;for(const [t,v] of data){if(previous&&maxGapSeconds!=null&&t-previous[0]<=maxGapSeconds&&predicate(previous[1])){const duration=t-previous[0];total+=duration;if(open){open.end=t;open.seconds+=duration;open.min=Math.min(open.min,previous[1]);}else{open={start:previous[0],end:t,seconds:duration,min:previous[1]};episodes.push(open);}}else if(open){open=null;}previous=[t,v];}return {seconds:total,episodes};}
  function average(values){const valid=values.filter(Number.isFinite);return valid.length?valid.reduce((a,b)=>a+b,0)/valid.length:null;}
  function cycles(data,lowThreshold,targetThreshold,maxGapSeconds){const result=[];let start=null,low=null,previous=null;for(const [t,v] of data){if(previous&&maxGapSeconds!=null&&t-previous[0]>maxGapSeconds&&start!=null){result.push({start,end:null,minutes:null,min:low,interrupted:true});start=null;low=null;}if(start==null&&lowThreshold!=null&&v<lowThreshold){start=t;low=v;}else if(start!=null){low=Math.min(low,v);if(targetThreshold!=null&&v>=targetThreshold){result.push({start,end:t,minutes:(t-start)/60,min:low,interrupted:false});start=null;low=null;}}previous=[t,v];}if(start!=null)result.push({start,end:null,minutes:null,min:low,interrupted:false});return result;}
  function pumpImpact(temp,pump,windowSeconds){const deltas=[];for(let i=1;i<pump.length;i++){if(pump[i-1][1]===0&&pump[i][1]===1){const t=pump[i][0],before=temp.filter(p=>p[0]>=t-windowSeconds&&p[0]<t).slice(-1)[0],after=temp.find(p=>p[0]>=t&&p[0]<=t+windowSeconds);if(before&&after)deltas.push(after[1]-before[1]);}}return {starts:deltas.length,mean:average(deltas)};}
  function kpi(label,value,detail=''){const e=document.createElement('div');e.className='diagnostics-kpi';const n=document.createElement('span'),v=document.createElement('strong'),d=document.createElement('small');n.textContent=label;v.textContent=value;d.textContent=detail;e.append(n,v);if(detail)e.append(d);return e;}
  function row(label,value,detail=''){const e=document.createElement('article');e.className='diagnostics-event';const b=document.createElement('div'),h=document.createElement('strong'),s=document.createElement('small'),t=document.createElement('time');h.textContent=label;s.textContent=detail;t.textContent=value;b.append(h,s);e.append(b,t);return e;}
  function render(payload,config){
    const state=$('diagnosticsHistoryState'),summary=$('diagnosticsSummary'),events=$('diagnosticsEvents');
    summary.replaceChildren();events.replaceChildren();
    if(!payload?.configured||!payload?.connected||!Array.isArray(payload.series)){state.textContent='BRAK HISTORII';state.className='badge warn';events.append(row('Historia niedostępna','—',payload?.error||'ThingSpeak nie zwrócił serii historycznych.'));return;}
    const get=key=>points(payload,key),co=get('co'),flue=get('flue'),power=get('power'),ret=get('ret'),valve=get('valve'),opening=get('opening'),setpoint=get('setpoint'),floor=get('floor'),dhw=get('dhw'),rad=get('rad');
    const settings=analysisSettings(config);
    const coTarget=settings?.co_recovery_target,coLow=settings?.co_low_threshold;
    const returnMinimum=settings?.return_protection_min,returnTolerance=settings?.return_protection_tolerance,minimumOpening=settings?.valve_minimum_opening;
    const maxGap=settings?.max_sample_gap_seconds,pumpWindow=settings?.pump_correlation_window_seconds;
    const powerActiveMin=settings?.boiler_power_active_min_kw,valveChangeMin=settings?.valve_change_min_delta;
    const settingsReady=[coTarget,coLow,returnMinimum,returnTolerance,minimumOpening,maxGap,pumpWindow,powerActiveMin,valveChangeMin].every(v=>v!=null&&Number.isFinite(Number(v)));
    const intro=$('diagnosticsIntro');if(intro)intro.textContent=settingsReady?`Analiza ${fmt(payload.range_hours,0)} h z ThingSpeak. Cykl CO: spadek poniżej ${fmt(coLow,1)}°C → osiągnięcie ${fmt(coTarget,1)}°C. Ochrona powrotu: ${fmt(returnMinimum,1)}°C ±${fmt(returnTolerance,1)}°C. Progi pobrane z API; wyniki opisują zapisane próbki i nie sterują kotłem.`:'Analiza z zapisanych próbek ThingSpeak. Brakuje części konfiguracji z API; wyniki są oznaczone jako częściowe.';
    const coQuality=quality(co,maxGap),returnQuality=quality(ret,maxGap);
    const coCycles=cycles(co,coLow,coTarget,maxGap);
    const cold=returnMinimum==null?{seconds:0,episodes:[]}:spans(ret,v=>v<returnMinimum,maxGap);
    const below64=coLow==null?{seconds:0,episodes:[]}:spans(co,v=>v<coLow,maxGap);
    const below70=coTarget==null?{seconds:0,episodes:[]}:spans(co,v=>v<coTarget,maxGap);
    const burner=powerActiveMin==null?{seconds:0,episodes:[]}:spans(power,v=>v>powerActiveMin,maxGap);
    const lowOpening=minimumOpening==null?{seconds:0,episodes:[]}:spans(opening,v=>v<=minimumOpening,maxGap);
    const changes=opening.slice(1).filter((p,i)=>Math.abs(p[1]-opening[i][1])>=valveChangeMin).length;
    const range=opening.length?Math.max(...opening.map(p=>p[1]))-Math.min(...opening.map(p=>p[1])):null;
    const floorImpact=pumpImpact(ret,floor,pumpWindow),dhwImpact=pumpImpact(ret,dhw,pumpWindow),radImpact=pumpImpact(ret,rad,pumpWindow);
    const complete=coCycles.filter(c=>c.end&&!c.interrupted),interrupted=coCycles.filter(c=>c.interrupted).length,avgCycle=average(complete.map(c=>c.minutes)),slowest=complete.length?Math.max(...complete.map(c=>c.minutes)):null;
    const partial=!settingsReady||coQuality.gaps>0||returnQuality.gaps>0||!co.length||!ret.length;
    state.textContent=partial?'ANALIZA CZĘŚCIOWA':'ANALIZA GOTOWA';state.className='badge '+(partial?'warn':'good');
    [
      kpi(`Cykle CO <${fmt(coLow,1)} → ${fmt(coTarget,1)}°C`,String(complete.length),interrupted+' przerwanych luką danych'),
      kpi(`Średni powrót do ${fmt(coTarget,1)}°C`,avgCycle==null?'Brak danych':fmt(avgCycle)+' min','tylko kompletne cykle bez luk powyżej '+fmt(maxGap/60,1)+' min'),
      kpi(`Czas CO poniżej ${fmt(coLow,1)}°C`,fmt(below64.seconds/60)+' min','z próbek temperatury CO'),
      kpi(`Czas CO poniżej ${fmt(coTarget,1)}°C`,fmt(below70.seconds/60)+' min','łączny czas w zakresie'),
      kpi('Palnik / moc >0',fmt(burner.seconds/60)+' min',burner.episodes.length+' zarejestrowanych okresów pracy'),
      kpi(`Powrót poniżej ${fmt(returnMinimum,1)}°C`,fmt(cold.seconds/60)+' min',cold.episodes.length+' epizodów'),
      kpi('4D przy minimalnym otwarciu',minimumOpening==null?'Brak konfiguracji':fmt(lowOpening.seconds/60)+' min',minimumOpening==null?'Nie znaleziono nastawy w konfiguracji eModul':'próg z konfiguracji eModul: '+fmt(minimumOpening)+'%'),
      kpi('Zmiany otwarcia 4D',String(changes),range==null?'brak zakresu':'rozstęp '+fmt(range)+' p.p.'),
      kpi('Jakość próbek CO',String(coQuality.count),coQuality.gaps+' luk >'+fmt(maxGap,0)+' s · max '+fmt(coQuality.maxGap,0)+' s')
    ].forEach(e=>summary.append(e));
    const notes=[];
    notes.push(complete.length?row('Najdłuższy pełny cykl odbudowy CO',fmt(slowest)+' min','minimum w cyklu '+fmt(Math.min(...complete.map(c=>c.min)))+'°C'):row('Cykle CO','—','Brak pełnego cyklu bez przerwy w danych.'));
    complete.slice(-5).reverse().forEach(c=>notes.push(row('Spadek i odbudowa temperatury CO',when(c.start),fmt(c.minutes)+' min · minimum '+fmt(c.min)+'°C · do '+fmt(coTarget,1)+'°C')));
    burner.episodes.slice(-5).reverse().forEach(e=>notes.push(row('Rozpalanie / moc palnika wykryta',when(e.start),fmt(e.seconds/60)+' min zarejestrowanej pracy · zdarzenie z progu mocy '+fmt(powerActiveMin)+' kW')));
    if(interrupted)notes.push(row('Cykle przerwane luką danych',String(interrupted),'Nie są używane do średniego czasu odbudowy.'));
    if(coQuality.gaps||returnQuality.gaps)notes.push(row('Jakość historii','ANALIZA CZĘŚCIOWA','Wykryto przerwy próbkowania dłuższe niż '+fmt(maxGap/60,1)+' min; czasów w lukach nie doliczamy.'));
    if(cold.episodes.length)cold.episodes.slice(-5).reverse().forEach(e=>notes.push(row(`Powrót poniżej ${fmt(returnMinimum,1)}°C`,when(e.start),fmt(e.seconds/60)+' min · minimum '+fmt(e.min)+'°C')));
    else notes.push(row('Ochrona powrotu',`${fmt(returnMinimum,1)}°C ±${fmt(returnTolerance,1)}°C`,'W zapisanych próbkach nie wykryto zejścia poniżej progu ochrony powrotu.'));
    for(const [label,result] of [['Pompa podłogowa',floorImpact],['Pompa CWU',dhwImpact],['Pompa 4D',radImpact]])notes.push(row(label,result.starts+' startów z próbką',result.mean==null?'Brak par próbek do porównania':'korelacja: średnia zmiana powrotu w '+fmt(pumpWindow/60,0)+' min: '+fmt(result.mean)+'°C · nie oznacza przyczynowości'));
    const flueDelta=flue.length?flue[flue.length-1][1]-flue[0][1]:null;
    notes.push(row('Temperatura spalin',flueDelta==null?'Brak danych':flue.length+' próbek','zmiana w zakresie: '+(flueDelta>0?'+':'')+fmt(flueDelta)+'°C · bez wnioskowania o zapłonie'));
    notes.push(row('Dostępność serii',[co,flue,power,ret,valve,opening,floor,dhw,rad].filter(x=>x.length).length+'/9','brakujące serie nie są estymowane · zadana 4D: '+(setpoint.length?'dostępna':'brak')+' · próbki powrotu: '+returnQuality.count));
    notes.forEach(e=>events.append(e));
    $('diagnosticsHistoryMeta').textContent='Źródło: ThingSpeak · '+fmt(payload.range_hours)+' h · mediana odstępu próbek CO: '+(coQuality.median==null?'brak danych':fmt(coQuality.median,0)+' s')+' · przerwy >'+fmt(maxGap,0)+' s wyłączone z czasu zdarzeń · odświeżanie co 60 s · tylko odczyt.';
  }
  const configPromise=ThermoFlowApi.get('/api/diagnostics').then(response=>response.json()).catch(()=>null);
  let selectedHours=24,requestId=0;
  async function load(){const current=++requestId;try{const config=await configPromise;const minimum=config?.analysis_config?.valve_minimum_opening;const end=Date.now();const start=end-selectedHours*3600000;const windows=[];for(let cursor=start;cursor<end;cursor+=24*3600000)windows.push([cursor,Math.min(cursor+24*3600000,end)]);const responses=Array(windows.length);let next=0;await Promise.all(Array.from({length:Math.min(3,windows.length)},async()=>{while(next<windows.length){const index=next++, [from,to]=windows[index];const params=new URLSearchParams({start:new Date(from).toISOString(),end:new Date(to).toISOString(),format:'full'});if(minimum!=null&&Number.isFinite(Number(minimum)))params.set('minimum_opening',String(minimum));const response=await ThermoFlowApi.get('/api/history?'+params.toString());responses[index]=await response.json();}}));if(current!==requestId)return;const failed=responses.find(payload=>!payload?.configured||!payload?.connected||!Array.isArray(payload.series));if(failed){render(failed,config);return;}const combined=new Map();for(const payload of responses)for(const item of payload.series){if(!combined.has(item.id))combined.set(item.id,{...item,points:[]});const target=combined.get(item.id);target.points.push(...(item.points||[]));}for(const item of combined.values()){const unique=new Map();for(const point of item.points)unique.set(Number(point[0]),[Number(point[0]),Number(point[1])]);item.points=[...unique.values()].sort((a,b)=>a[0]-b[0]);}render({configured:true,connected:true,series:[...combined.values()],range_hours:selectedHours,fetched_at:new Date().toISOString()},config);}catch(_){if(current===requestId)render({configured:true,connected:false,error:'Nie udało się pobrać historii ThingSpeak.'},null);}}
  document.querySelectorAll('[data-diagnostics-range]').forEach(button=>button.addEventListener('click',()=>{selectedHours=Number(button.dataset.diagnosticsRange);document.querySelectorAll('[data-diagnostics-range]').forEach(item=>{const active=item===button;item.classList.toggle('active',active);item.setAttribute('aria-pressed',String(active));});load();}));
  load();setInterval(load,60000);
})();
