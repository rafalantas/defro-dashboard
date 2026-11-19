/* ThermoFlow Sprint 4 — read-only operational diagnostics derived from ThingSpeak history. */
(function(){
  const $=id=>document.getElementById(id);
  const fmt=(v,d=1)=>v==null||!Number.isFinite(Number(v))?'Brak danych':Number(v).toLocaleString('pl-PL',{maximumFractionDigits:d});
  const when=t=>{if(!t)return '—';const d=new Date(Number(t)*1000);return Number.isNaN(d.getTime())?'—':d.toLocaleString('pl-PL',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});};
  const ids={co:'thermoflow-co',flue:'thermoflow-flue',power:'thermoflow-boiler-power',ret:'thermoflow-valve4d-return',valve:'thermoflow-valve4d-current',opening:'thermoflow-valve4d-opening',setpoint:'thermoflow-valve4d-effective-setpoint',floor:'thermoflow-pump-floor',dhw:'thermoflow-pump-dhw',rad:'thermoflow-pump-valve4d'};
  function points(payload,key){const item=(payload.series||[]).find(s=>s.id===ids[key]);return (item?.points||[]).filter(p=>Array.isArray(p)&&Number.isFinite(Number(p[0]))&&Number.isFinite(Number(p[1]))).map(p=>[Number(p[0]),Number(p[1])]).sort((a,b)=>a[0]-b[0]);}
  function spans(data,predicate,maxGap=300){let total=0,episodes=[],open=null,previous=null;for(const [t,v] of data){if(previous&&t-previous[0]<=maxGap&&predicate(previous[1])){const duration=t-previous[0];total+=duration;if(open){open.end=t;open.seconds+=duration;open.min=Math.min(open.min,previous[1]);}else{open={start:previous[0],end:t,seconds:duration,min:previous[1]};episodes.push(open);}}else if(open){open=null;}previous=[t,v];}return {seconds:total,episodes};}
  function average(values){const valid=values.filter(Number.isFinite);return valid.length?valid.reduce((a,b)=>a+b,0)/valid.length:null;}
  function cycles(data){const result=[];let start=null,low=null;for(const [t,v] of data){if(start==null&&v<64){start=t;low=v;}else if(start!=null){low=Math.min(low,v);if(v>=70){result.push({start,end:t,minutes:(t-start)/60,min:low});start=null;low=null;}}}if(start!=null)result.push({start,end:null,minutes:null,min:low});return result;}
  function pumpImpact(temp,pump){const deltas=[];for(let i=1;i<pump.length;i++){if(pump[i-1][1]===0&&pump[i][1]===1){const t=pump[i][0],before=temp.filter(p=>p[0]>=t-600&&p[0]<t).slice(-1)[0],after=temp.find(p=>p[0]>=t&&p[0]<=t+600);if(before&&after)deltas.push(after[1]-before[1]);}}return {starts:deltas.length,mean:average(deltas)};}
  function kpi(label,value,detail=''){const e=document.createElement('div');e.className='diagnostics-kpi';const n=document.createElement('span'),v=document.createElement('strong'),d=document.createElement('small');n.textContent=label;v.textContent=value;d.textContent=detail;e.append(n,v);if(detail)e.append(d);return e;}
  function row(label,value,detail=''){const e=document.createElement('article');e.className='diagnostics-event';const b=document.createElement('div'),h=document.createElement('strong'),s=document.createElement('small'),t=document.createElement('time');h.textContent=label;s.textContent=detail;t.textContent=value;b.append(h,s);e.append(b,t);return e;}
  function render(payload){
    const state=$('diagnosticsHistoryState'),summary=$('diagnosticsSummary'),events=$('diagnosticsEvents');
    summary.replaceChildren();events.replaceChildren();
    if(!payload?.configured||!payload?.connected||!Array.isArray(payload.series)){state.textContent='BRAK HISTORII';state.className='badge warn';events.append(row('Historia niedostępna','—',payload?.error||'ThingSpeak nie zwrócił serii historycznych.'));return;}
    const get=key=>points(payload,key),co=get('co'),flue=get('flue'),power=get('power'),ret=get('ret'),valve=get('valve'),opening=get('opening'),setpoint=get('setpoint'),floor=get('floor'),dhw=get('dhw'),rad=get('rad');
    const coCycles=cycles(co),cold=spans(ret,v=>v<55),below64=spans(co,v=>v<64),below70=spans(co,v=>v<70),burner=spans(power,v=>v>0),lowOpening=spans(opening,v=>v<=5);
    const changes=opening.slice(1).filter((p,i)=>Math.abs(p[1]-opening[i][1])>=3).length;
    const range=opening.length?Math.max(...opening.map(p=>p[1]))-Math.min(...opening.map(p=>p[1])):null;
    const floorImpact=pumpImpact(ret,floor),dhwImpact=pumpImpact(ret,dhw),radImpact=pumpImpact(ret,rad);
    const complete=coCycles.filter(c=>c.end),avgCycle=average(complete.map(c=>c.minutes)),slowest=complete.length?Math.max(...complete.map(c=>c.minutes)):null;
    state.textContent='ANALIZA GOTOWA';state.className='badge good';
    [
      kpi('Cykle CO <64 → 70°C',String(coCycles.length),complete.length+' zakończonych'),
      kpi('Średni powrót do 70°C',avgCycle==null?'Brak danych':fmt(avgCycle)+' min','od spadku poniżej 64°C'),
      kpi('Czas CO poniżej 64°C',fmt(below64.seconds/60)+' min','z próbek temperatury CO'),
      kpi('Czas CO poniżej 70°C',fmt(below70.seconds/60)+' min','łączny czas w zakresie'),
      kpi('Palnik / moc >0',fmt(burner.seconds/60)+' min','czas zarejestrowanej mocy'),
      kpi('Powrót poniżej 55°C',fmt(cold.seconds/60)+' min',cold.episodes.length+' epizodów'),
      kpi('4D przy otwarciu ≤5%',fmt(lowOpening.seconds/60)+' min','czas zarejestrowany'),
      kpi('Zmiany otwarcia 4D',String(changes),range==null?'brak zakresu':'rozstęp '+fmt(range)+' p.p.')
    ].forEach(e=>summary.append(e));
    const notes=[];
    notes.push(complete.length?row('Najdłuższy cykl odbudowy CO',fmt(slowest)+' min','minimum w cyklu '+fmt(Math.min(...complete.map(c=>c.min)))+'°C'):row('Cykle CO','—','Brak pełnego cyklu poniżej 64°C zakończonego osiągnięciem 70°C.'));
    if(cold.episodes.length)cold.episodes.slice(-5).reverse().forEach(e=>notes.push(row('Powrót poniżej 55°C',when(e.start),fmt(e.seconds/60)+' min · minimum '+fmt(e.min)+'°C')));
    else notes.push(row('Ochrona powrotu','55°C ±1°C','W zapisanych próbkach nie wykryto zejścia poniżej 55°C.'));
    for(const [label,result] of [['Pompa podłogowa',floorImpact],['Pompa CWU',dhwImpact],['Pompa 4D',radImpact]])notes.push(row(label,result.starts+' startów z próbką',result.mean==null?'Brak par próbek do porównania':'średnia zmiana powrotu w 10 min: '+fmt(result.mean)+'°C'));
    const flueDelta=flue.length?flue[flue.length-1][1]-flue[0][1]:null;
    notes.push(row('Temperatura spalin',flueDelta==null?'Brak danych':flue.length+' próbek','zmiana w zakresie: '+(flueDelta>0?'+':'')+fmt(flueDelta)+'°C · bez wnioskowania o zapłonie'));
    notes.push(row('Dostępność serii',[co,flue,power,ret,valve,opening,floor,dhw,rad].filter(x=>x.length).length+'/9','brakujące serie nie są estymowane · zadana 4D: '+(setpoint.length?'dostępna':'brak')));
    notes.forEach(e=>events.append(e));
    $('diagnosticsHistoryMeta').textContent='Źródło: ThingSpeak · '+fmt(payload.range_hours)+' h · próbki zwykle co ok. 40 s · analiza z dostępnych punktów · odświeżanie co 60 s · tylko odczyt.';
  }
  async function load(){try{const response=await ThermoFlowApi.get('/api/history?hours=24&format=full');render(await response.json());}catch(_){render({configured:true,connected:false,error:'Nie udało się pobrać historii ThingSpeak.'});}}
  load();setInterval(load,60000);
})();