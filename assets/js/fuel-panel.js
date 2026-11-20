(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const n = (v, d=1) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('pl-PL',{maximumFractionDigits:d,minimumFractionDigits:d});
  const date = (v) => { const d=new Date(v); return Number.isNaN(d.getTime())?'—':d.toLocaleString('pl-PL',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}); };
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let dailyData=[], refillData=[], summaryData=null;
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
    const vals=rows.flatMap(r=>series.map(s=>Number(r[s.key])).filter(Number.isFinite));
    const max=Math.max(5,...vals)*1.15, left=42,right=12,top=18,bottom=42,plotW=w-left-right,plotH=h-top-bottom;
    c.font='10px DM Mono, monospace'; c.textAlign='right'; c.fillStyle='rgba(174,190,198,.72)';
    for(let i=0;i<=4;i++){const y=top+plotH*i/4;c.strokeStyle='rgba(150,170,180,.13)';c.beginPath();c.moveTo(left,y);c.lineTo(w-right,y);c.stroke();c.fillText(n(max*(1-i/4),0),left-8,y+3);}
    const group=plotW/rows.length, bw=Math.max(2,Math.min(12,group/(series.length+1)));
    rows.forEach((r,i)=>{series.forEach((s,j)=>{const v=Number(r[s.key]);if(!Number.isFinite(v))return;const bh=Math.max(1,v/max*plotH),x=left+i*group+(group-series.length*bw)/2+j*bw,y=top+plotH-bh;c.fillStyle=s.color;c.globalAlpha=s.key==='model_kg'?.62:0.9;c.fillRect(x,y,bw-1,bh);c.globalAlpha=1;});if(i%Math.max(1,Math.ceil(rows.length/6))===0||i===rows.length-1){c.fillStyle='rgba(174,190,198,.72)';c.textAlign='center';c.fillText(String(r.day).slice(5),left+i*group+group/2,h-20);}});
    const legend=$('fuelChartLegend'); if(legend) legend.innerHTML=series.map(s=>'<span><i style="background:'+s.color+'"></i>'+s.label+'</span>').join('');
  }
  function renderRefills(){
    const el=$('fuelRefillHistory'); if(!el)return;
    const rows=refillData.slice().reverse().slice(0,12);
    el.innerHTML=rows.length?rows.map(r=>'<div class="fuel-refill-row"><strong>'+esc(date(r.ts))+'</strong><b>'+n(r.kg,1)+' kg</b></div>').join(''):'<div class="fuel-empty">Brak zapisanych dosypek.</div>';
    const count=$('fuelRefillCount');if(count)count.textContent=refillData.length+' wpisów';
  }
  function renderModel(){
    const s=summaryData||{}, m=s.model||{}, dm=s.daily_model||{}, cc=s.counter_calibration||{}, cal=s.calibration||{}, off=s.om_offset||{};
    const el=$('fuelModelDetails');if(!el)return;
    const ai=s.ai_shadow||{}, base=ai.baseline||{}, candidate=ai.candidate||{};
    const aiLabel=ai.status==='shadow_ready'?'test gotowy · '+(ai.selected==='weather_aware'?'pogoda + temperatura':'sama temperatura'):(ai.message||'zbieranie danych');
    const rows=[
      ['Fuel Intelligence',aiLabel],
      ['AI MAE / bazowy',ai.status==='shadow_ready'?n(candidate.mae_kg_day,2)+' / '+n(base.mae_kg_day,2)+' kg/d':'—'],
      ['AI test chronologiczny',ai.status==='shadow_ready'?(ai.holdout_from+' – '+ai.holdout_to+' · '+ai.holdout_days+' dni'):'wymaga więcej danych'],
      ['AI fixed holdout',ai.status==='shadow_ready'?n(ai.fixed_holdout_mae_gain_percent,1)+'% poprawy MAE · wymagane ≥ 3%':'—'],
      ['AI odrzucone doby',ai.rejected_days!=null?String(ai.rejected_days):'—'],
      ['AI powody odrzucenia',ai.rejected_by_reason?Object.entries(ai.rejected_by_reason).map(([k,v])=>k+': '+v).join(' · '):'—'],
      ['AI luki w historii',Array.isArray(ai.missing_day_gaps)?(ai.missing_day_gaps.length?ai.missing_day_gaps.length+' luk · '+ai.missing_day_gaps.reduce((sum,g)=>sum+(g.missing_days||0),0)+' brakujących dób':'brak luk'):'—'],
      ['AI niekompletna telemetria',ai.telemetry_incomplete_days!=null?String(ai.telemetry_incomplete_days)+' dób':'—'],
      ['AI telemetria',ai.telemetry?.status==='shadow_ready'?(ai.telemetry.accepted?'rozszerzony model zaakceptowany':'model bazowy zachowany'):(ai.telemetry?.message||'zbieranie danych')],
      ['AI telemetria · historia',ai.telemetry?.complete_days!=null?ai.telemetry.complete_days+' kompletnych dób · trening '+(ai.telemetry.training_days||0)+' / test '+(ai.telemetry.holdout_days||0):'—'],
      ['AI telemetria · braki',ai.telemetry?.incomplete_days!=null?String(ai.telemetry.incomplete_days)+' niekompletnych dób':'—'],
      ['AI telemetria · MAE',ai.telemetry?.status==='shadow_ready'?n(ai.telemetry.candidate?.mae_kg_day,2)+' / '+n(ai.telemetry.baseline?.mae_kg_day,2)+' kg/d':'—'],
      ['AI telemetria · fixed holdout',ai.telemetry?.status==='shadow_ready'?n(ai.telemetry.fixed_holdout_mae_gain_percent,1)+'% poprawy MAE · wymagane ≥ 3%':'—'],
      ['Model domu',m.periods_used!=null?m.periods_used+' okresów · R² '+n(m.r2,2):'za mało okresów'],
      ['Model dobowy',dm.status==='aktywny'?'aktywny · '+(dm.days||0)+' dób':(dm.days!=null?'zbiera dane · '+dm.days+'/'+(dm.days_needed||30):'brak danych')],
      ['Licznik podajnika',cc.calibrated?n(cc.kg_per_percent,2)+' kg/%':(cc.message||'w trakcie kalibracji')],
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
  async function loadDetails(){
    try { const [daily,stats,refills]=await Promise.all([get('/api/fuel/daily?days=30'),get('/api/fuel/stats?days=30'),get('/api/fuel/refills')]); const burnerByDay=new Map((stats.daily||[]).map(r=>[r.day,r.est_kg])); dailyData=(daily.days||[]).map(r=>({...r,est_kg:burnerByDay.get(r.day)??null}));refillData=refills.refills||[];renderChart();renderRefills(); }
    catch(e){ const el=$('fuelDetailStatus');if(el)el.textContent='Nie udało się pobrać historii: '+e.message; }
  }
  function updateSummary(s){summaryData=s;renderModel();renderForecast();renderPlanning();const p=$('fuelStockMethod');if(p&&s.hopper)p.textContent=s.hopper.method||'metoda szacowania niedostępna';setTimeout(renderChart,120);}
  window.addEventListener('thermoflow:fuel',e=>updateSummary(e.detail||{}));
  window.addEventListener('resize',renderChart);
  if(window.ThermoFlowFuelSummary)updateSummary(window.ThermoFlowFuelSummary);
  loadDetails();setInterval(loadDetails,300000);
})();