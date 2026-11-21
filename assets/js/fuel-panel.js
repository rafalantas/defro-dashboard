(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const n = (v, d=1) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('pl-PL',{maximumFractionDigits:d,minimumFractionDigits:d});
  const date = (v) => { const d=new Date(v); return Number.isNaN(d.getTime())?'—':d.toLocaleString('pl-PL',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}); };
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let dailyData=[], refillData=[], summaryData=null, telemetryAudit=null;
  const get = async (path) => { const r=await ThermoFlowApi.get(path); if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); };
  function renderChart() {
    const canvas=$('fuelDailyChart'); if(!canvas) return;
    const rect=canvas.getBoundingClientRect(); if(!rect.width) return;
    const ratio=window.devicePixelRatio||1, w=Math.max(320,rect.width), h=260;
    canvas.width=Math.round(w*ratio); canvas.height=Math.round(h*ratio);
    const c=canvas.getContext('2d'); c.setTransform(ratio,0,0,ratio,0,0); c.clearRect(0,0,w,h);
    const rows=dailyData.filter(x=>x && x.day).slice(-30);
    if(!rows.length){ c.fillStyle='rgba(190,205,212,.7)'; c.font='13px Manrope, sans-serif'; c.fillText('Brak kompletnych danych dobowych',18,32); return; }
    const series=[
      {key:'kg_counter',label:'Licznik',color:'#49d7bd'},
      {key:'est_kg',label:'Moc palnika',color:'#f0ad67'},
      {key:'model_kg',label:'Model',color:'#8aa8ff'}
    ].filter(s=>rows.some(r=>r[s.key]!=null));
    const vals=rows.flatMap(r=>series.map(s=>r[s.key]).filter(v=>v!==null&&v!==undefined&&v!=='').map(Number).filter(Number.isFinite));
    const max=Math.max(5,...vals)*1.15, left=42,right=12,top=18,bottom=42,plotW=w-left-right,plotH=h-top-bottom;
    c.font='10px DM Mono, monospace'; c.textAlign='right'; c.fillStyle='rgba(174,190,198,.72)';
    for(let i=0;i<=4;i++){const y=top+plotH*i/4;c.strokeStyle='rgba(150,170,180,.13)';c.beginPath();c.moveTo(left,y);c.lineTo(w-right,y);c.stroke();c.fillText(n(max*(1-i/4),0),left-8,y+3);}
    const group=plotW/rows.length, bw=Math.max(2,Math.min(12,group/(series.length+1)));
    rows.forEach((r,i)=>{series.forEach((s,j)=>{const raw=r[s.key];if(raw===null||raw===undefined||raw==='')return;const v=Number(raw);if(!Number.isFinite(v)||v<=0)return;const bh=Math.max(1,v/max*plotH),x=left+i*group+(group-series.length*bw)/2+j*bw,y=top+plotH-bh;c.fillStyle=s.color;c.globalAlpha=s.key==='model_kg'?.62:0.9;c.fillRect(x,y,bw-1,bh);c.globalAlpha=1;});if(i%Math.max(1,Math.ceil(rows.length/6))===0||i===rows.length-1){c.fillStyle='rgba(174,190,198,.72)';c.textAlign='center';c.fillText(String(r.day).slice(5),left+i*group+group/2,h-20);}});
    const legend=$('fuelChartLegend'); if(legend) legend.innerHTML=series.map(s=>'<span><i style="background:'+s.color+'"></i>'+s.label+'</span>').join('');
  }
  function renderRefills(){
    const el=$('fuelRefillHistory'); if(!el)return;
    const rows=refillData.slice().reverse().slice(0,12);
    el.innerHTML=rows.length?rows.map(r=>'<div class="fuel-refill-row"><strong>'+esc(date(r.ts))+'</strong><b>'+n(r.kg,1)+' kg</b></div>').join(''):'<div class="fuel-empty">Brak zapisanych dosypek.</div>';
    const count=$('fuelRefillCount');if(count)count.textContent=refillData.length+' wpisów';
  }
  function renderTelemetryQuality(){
    const el=$('fuelTelemetryQuality');if(!el)return;
    const audit=telemetryAudit;
    if(!audit){el.innerHTML='<div class="fuel-empty">Audyt jakości telemetrii niedostępny lub nieobsługiwany przez API.</div>';return;}
    const channels=audit.channels||[];
    if(!channels.length){el.innerHTML='<div class="fuel-empty">Brak danych z kanałów ThingSpeak.</div>';return;}
    const total=audit.total_raw_feeds??channels.reduce((sum,c)=>sum+(c.raw_feeds||0),0);
    const invalid=audit.total_timestamp_invalid_feeds??channels.reduce((sum,c)=>sum+(c.timestamp_invalid_feeds||0),0);
    const duplicates=audit.total_duplicate_timestamp_feeds??channels.reduce((sum,c)=>sum+(c.duplicate_timestamp_feeds||0),0);
    const signalRows=channels.flatMap(channel=>Object.entries(channel.fields||{}).map(([key,v])=>({channel:channel.name,key,...v})));
    const loss=signalRows.filter(v=>v.raw_samples>0).sort((a,b)=>(b.loss_percent||0)-(a.loss_percent||0));
    const summary='<div class="fuel-quality-summary"><div><span>Surowe próbki</span><b>'+n(total,0)+'</b></div><div><span>Błędne czasy</span><b>'+n(invalid,0)+'</b></div><div><span>Duplikaty</span><b>'+n(duplicates,0)+'</b></div></div>';
    const channelHtml=channels.map(c=>{
      const fields=Object.entries(c.fields||{});
      const accepted=fields.reduce((sum,[,v])=>sum+(v.accepted_samples||0),0);
      const lost=fields.reduce((sum,[,v])=>sum+(v.lost_samples||0),0);
      return '<details class="fuel-quality-channel"><summary><span>'+esc(c.name)+'</span><small>'+n(c.raw_feeds,0)+' próbek · '+n(c.timestamp_invalid_feeds,0)+' błędnych timestampów · '+n(c.duplicate_timestamp_feeds,0)+' nadmiarowych duplikatów</small></summary><div class="fuel-quality-fields">'+fields.map(([key,v])=>'<div class="fuel-quality-field"><span>'+esc(key.replaceAll('_',' '))+'</span><b>'+n(v.loss_percent,1)+'% strat</b><small>'+n(v.accepted_samples,0)+' poprawnych / '+n(v.raw_samples,0)+' surowych · braki '+n(v.missing_samples,0)+' · błędne '+n(v.invalid_samples,0)+' · poza zakresem '+n(v.out_of_range_samples,0)+' · bez czasu '+n(v.timestamp_unusable_samples,0)+'</small></div>').join('')+'</div></details>';
    }).join('');
    const worst=loss.slice(0,3).map(v=>'<span>'+esc(v.key.replaceAll('_',' '))+': '+n(v.loss_percent,1)+'%</span>').join('');
    el.innerHTML=summary+'<div class="fuel-quality-worst">'+(worst||'Brak próbek do oceny strat')+'</div>'+channelHtml+'<p class="fuel-detail-footnote">Audyt read-only. Zakresy sanity są orientacyjne; nie filtrują ani nie zmieniają danych używanych przez model.</p>';
  }
  function renderModel(){
    const s=summaryData||{}, m=s.model||{}, dm=s.daily_model||{}, cc=s.counter_calibration||{}, cal=s.calibration||{}, off=s.om_offset||{};
    const el=$('fuelModelDetails');if(!el)return;
    const ai=s.ai_shadow||{}, base=ai.baseline||{}, candidate=ai.candidate||{};
    const aiLabel=ai.status==='shadow_ready'?'test gotowy · '+(ai.selected==='weather_aware'?'pogoda + temperatura':'sama temperatura'):(ai.message||'zbieranie danych');
    const rows=[
      ['Fuel Intelligence',aiLabel],
      ['AI MAE / bazowy',ai.status==='shadow_ready'?n(candidate.mae_kg_day,2)+' / '+n(base.mae_kg_day,2)+' kg/d':'—'],
      ['AI test chronologiczny',ai.status==='shadow_ready'?(ai.holdout_from+' – '+ai.holdout_to+' · '+ai.holdout_days+' okresów'):(ai.available_periods!=null?'zbiera dane · '+ai.available_periods+'/'+(ai.required_periods||((ai.train_periods_needed||0)+(ai.test_periods_needed||0)))+' okresów':'wymaga więcej danych')],
      ['AI fixed holdout',ai.status==='shadow_ready'?n(ai.fixed_holdout_mae_gain_percent,1)+'% poprawy MAE · wymagane ≥ 3%':'—'],
      ['AI telemetria',ai.telemetry?.status==='shadow_ready'?(ai.telemetry.accepted?'rozszerzony model zaakceptowany':'model bazowy zachowany'):(ai.telemetry?.complete_periods!=null?'zbiera dane · '+ai.telemetry.complete_periods+'/'+(ai.telemetry.periods_needed||25)+' kompletnych okresów'+(ai.telemetry.incomplete_periods?' · '+ai.telemetry.incomplete_periods+' niekompletnych':''):(ai.telemetry?.message||'zbieranie danych'))],
      ['AI telemetria · pokrycie pól (★ cechy modelu)',ai.telemetry?.feature_coverage_periods?[
        'telemetry_co_c','telemetry_dhw_c','telemetry_floor_c','telemetry_return_c',
        'telemetry_boiler_power_kw','telemetry_valve_opening_pct','telemetry_pump_floor_h',
        'telemetry_pump_dhw_h','telemetry_pump_circulation_h'
      ].map(k=>((ai.telemetry.selected_features||[]).includes(k)?'★ ':'')+k.replace('telemetry_','').replaceAll('_',' ')+': '+(ai.telemetry.feature_coverage_periods[k]??0)).join(' · '):'brak rozbicia pokrycia'],
      ['AI telemetria · MAE',ai.telemetry?.status==='shadow_ready'?n(ai.telemetry.candidate?.mae_kg_day,2)+' / '+n(ai.telemetry.baseline?.mae_kg_day,2)+' kg/d':'—'],
      ['AI telemetria · fixed holdout',ai.telemetry?.status==='shadow_ready'?n(ai.telemetry.fixed_holdout_mae_gain_percent,1)+'% poprawy MAE · wymagane ≥ 3%':'—'],
      ['Model domu',m.periods_used!=null?m.periods_used+' okresów · R² '+n(m.r2,2):'za mało okresów'],
      ['Model dobowy',dm.status==='aktywny'?'aktywny · '+(dm.days||0)+' dób':(dm.days!=null?'zbiera dane · '+dm.days+'/'+(dm.days_needed||30):'brak danych')],
      ['Licznik podajnika',n(cc.kg_per_percent,2)+' kg/% · '+(cc.confidence?.label||cc.message||'ocena pewności niedostępna')+(cc.confidence?.periods_used!=null?' · '+cc.confidence.periods_used+' okresów':'')],
      ['Moc palnika',cal.calibrated?'skalibrowana ×'+n(cal.factor,2):(cal.provisional?'wstępny współczynnik ×'+n(cal.factor,2):'brak kalibracji')],
      ['Baza stopniodni',s.hdd_base!=null?n(s.hdd_base,1)+' °C':'—'],
      ['Korekta Open-Meteo',off.applied?n(off.offset,1)+' °C · '+(off.days||0)+' dób':'nieaktywna']
    ];
    el.innerHTML=rows.map(([k,v])=>'<div class="fuel-model-row"><span>'+esc(k)+'</span><b>'+esc(v)+'</b></div>').join('');
  }
  function renderPlanning(){
    const el=$('fuelPlanning');if(!el)return;
    const p=summaryData?.planning||{};
    if(p.status!=='ready'){el.innerHTML='<div class="fuel-empty">'+esc(p.message||'Oczekiwanie na dane pogodowe i model domu')+'</div>';return;}
    const periods=[['Dziś',p.today],['7 dni',p.week],['30 dni',p.month],['Sezon grzewczy',p.season]];
    el.innerHTML='<div class="fuel-planning-grid">'+periods.map(([label,v])=>'<div class="fuel-planning-card"><span>'+label+'</span><strong>'+n(v.kg,0)+' kg</strong><small>≈ '+v.bags_15kg+' worków po 15 kg</small><small>'+n(v.average_kg_day,1)+' kg/dobę</small></div>').join('')+'</div><p class="fuel-detail-footnote">'+esc(p.location)+' · '+esc(p.weather)+' · '+esc(p.note)+'</p>';
  }
  function renderForecast(){
    const el=$('fuelForecast');if(!el)return;
    const days=(summaryData?.forecast||[]).slice(0,7);
    el.innerHTML=days.length?days.map(d=>'<div class="fuel-forecast-day"><span>'+esc(new Date(d.day+'T12:00:00').toLocaleDateString('pl-PL',{weekday:'short',day:'2-digit',month:'2-digit'}))+'</span><b>'+n(d.kg,1)+' kg</b><small>'+n(d.t_mean,1)+' °C</small></div>').join(''):'<div class="fuel-empty">Prognoza niedostępna.</div>';
  }
  let detailsLoadPromise=null, burnerEstimates=new Map();
  function detailError(label,error){
    const el=$('fuelDetailStatus');
    if(el)el.textContent=label+': '+(error?.message||'błąd pobierania');
  }
  function applyBurnerEstimates(){
    dailyData=dailyData.map(row=>({...row,est_kg:burnerEstimates.get(row.day)??null}));
    renderChart();
  }
  function loadDetails(){
    if(detailsLoadPromise)return detailsLoadPromise;
    detailsLoadPromise=(async()=>{
      // Each section paints as soon as its own endpoint responds. The expensive
      // telemetry audit must never hold the daily chart or refill history.
      const dailyTask=get('/api/fuel/daily?days=30').then(daily=>{
        dailyData=daily.days||[];
        applyBurnerEstimates();
      }).catch(error=>detailError('Historia dobowa',error));
      const statsTask=get('/api/fuel/stats?days=30').then(stats=>{
        burnerEstimates=new Map((stats.daily||[]).map(row=>[row.day,row.est_kg]));
        applyBurnerEstimates();
      }).catch(error=>detailError('Estymacja mocy palnika',error));
      const refillsTask=get('/api/fuel/refills').then(refills=>{
        refillData=refills.refills||[];
        renderRefills();
      }).catch(error=>detailError('Historia dosypek',error));
      const auditTask=get('/api/fuel/telemetry-audit?days=30').then(audit=>{
        telemetryAudit=audit?.raw_ingestion_quality||null;
        renderTelemetryQuality();
      }).catch(error=>{
        telemetryAudit=null;
        renderTelemetryQuality();
        detailError('Audyt telemetrii',error);
      });
      await Promise.allSettled([dailyTask,statsTask,refillsTask,auditTask]);
    })().finally(()=>{detailsLoadPromise=null;});
    return detailsLoadPromise;
  }
  function updateSummary(s){summaryData=s;renderModel();renderForecast();renderPlanning();const p=$('fuelStockMethod');if(p&&s.hopper)p.textContent=s.hopper.method||'metoda szacowania niedostępna';setTimeout(renderChart,120);}
  window.addEventListener('thermoflow:fuel',e=>updateSummary(e.detail||{}));
  window.addEventListener('resize',renderChart);
  if(window.ThermoFlowFuelSummary)updateSummary(window.ThermoFlowFuelSummary);
  const fuelPanel=document.querySelector('[data-view="fuel"]');
  window.addEventListener('thermoflow:view',event=>{
    if(event.detail?.view==='fuel')loadDetails();
  });
  // Saved deep links may open the fuel panel before navigation listeners run.
  if(fuelPanel&&!fuelPanel.hidden)loadDetails();
  setInterval(()=>{if(fuelPanel&&!fuelPanel.hidden)loadDetails();},300000);
})();