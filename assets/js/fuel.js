function fuelNum(value,digits=0){return value==null||Number.isNaN(Number(value))?'—':Number(value).toLocaleString('pl-PL',{minimumFractionDigits:digits,maximumFractionDigits:digits});}
function fuelDate(value){const date=new Date(value);return Number.isNaN(date.getTime())?'—':date.toLocaleString('pl-PL',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});}
function renderFuel(summary){const week=summary.last7,refills=summary.refills_based,last=summary.last_refill,hopper=summary.hopper,calib=summary.calibration||{};
  $('fuelWeek').textContent=week?`${fuelNum(week.kg)} kg`:'—';
  const cc=summary.counter_calibration,counterNote=cc?.collecting?cc.message:null;
  $('fuelWeekMeta').textContent=week?`${fuelNum(week.kg_per_day,1)} kg/dobę · śr. ${fuelNum(week.t_mean,1)} °C · ${week.days} z 7 dób z danymi${week.source?` · ${week.source}`:''}${counterNote?` · ${counterNote}`:''}`:'brak danych mocy palnika';
  const model=summary.model;const hddValue=model?(refills?.heating_kg_per_hdd??week?.heating_kg_per_hdd??model.kg_per_hdd):(refills?.kg_per_hdd??week?.kg_per_hdd);$('fuelHdd').textContent=hddValue==null?'—':`${fuelNum(hddValue,2)} kg`;
  $('fuelHddLabel').textContent=model?'kg na stopniodzień · ogrzewanie':'kg na stopniodzień';
  $('fuelHddMeta').textContent=model?`stałe ${fuelNum(model.base_kg_per_day,1)} kg/dobę (CWU i straty) · grzanie poniżej ${fuelNum(model.balance_temp,1)} °C`:refills?`z dosypek ${fuelDate(refills.from).slice(0,5)}–${fuelDate(refills.to).slice(0,5)}${week?.kg_per_hdd!=null?` · 7 dni: ${fuelNum(week.kg_per_hdd,2)} kg`:''}`:week?'szacunek z ostatnich 7 dni':'za mało dosypek';
  if(last){const hours=Math.max(0,last.hours_ago),days=Math.floor(hours/24),rest=Math.round(hours%24);$('fuelSince').textContent=days?`${days} d ${rest} h`:`${rest} h`;$('fuelSinceMeta').textContent=`${fuelDate(last.ts)} · dosypano ${fuelNum(last.kg)} kg`;}
  else{$('fuelSince').textContent='—';$('fuelSinceMeta').textContent='brak zapisanych dosypek';}
  const tile=$('fuelHopperTile');tile.classList.remove('ok','alert');
  if(hopper){const left=hopper.days_to_reserve,hoursLeft=left!=null?Math.round(left*24):null;$('fuelHopper').textContent=`≈ ${fuelNum(hopper.remaining_kg)} kg`;$('fuelHopperMeta').textContent=hopper.below_reserve?`poniżej zalecanego minimum ${fuelNum(hopper.reserve_kg)} kg, dosyp teraz`:`${hopper.remaining_percent??'—'}%${left!=null?` · do rezerwy ${left<1?`ok. ${hoursLeft} h`:`ok. ${fuelNum(left,1)} dnia`}${hopper.days_source?.startsWith('prognoza')?' wg prognozy':''}`:''} · z ${fuelNum(hopper.capacity_kg)} kg${counterNote?` · ${counterNote}`:''}`;tile.classList.add(hopper.below_reserve||(left!=null&&left<1)?'alert':'ok');}
  else{$('fuelHopper').textContent='—';$('fuelHopperMeta').textContent='brak danych do szacunku';tile.classList.add('ok');}
  const forecast=(summary.forecast||[]).slice(0,3).map(day=>`${fuelNum(day.kg)} kg (${fuelNum(day.t_mean,1)} °C)`).join(' / ');const offset=summary.om_offset;const dm=summary.daily_model;const dmText=!dm?null:dm.status!=='aktywny'?`model dobowy (bezwładność, słońce, wiatr): zbiera dane (${dm.days}/${dm.days_needed} dób)`:dm.terms.join()!=='hdd'?`model dobowy: ${[dm.lag_weights?`bezwładność (temperatura ${fuelNum(dm.lag_weights[0],1)}×dziś + ${fuelNum(dm.lag_weights[1],1)}×wczoraj)`:null,dm.kg_per_solar_mj!=null?`słońce ${fuelNum(dm.kg_per_solar_mj,2)} kg na MJ/m²`:null,dm.kg_per_wind_hdd!=null?`wiatr +${fuelNum(dm.kg_per_wind_hdd,2)} kg na 100 km/h×stopniodzień`:null].filter(Boolean).join(', ')} (błąd prognozy ${fuelNum(dm.rmse_loo_kg_per_day,1)} kg/dobę zamiast ${fuelNum(dm.rmse_loo_temperature_only,1)})`:`model dobowy: bezwładność, słońce i wiatr bez istotnego wpływu (${dm.days} dób)`;
  $('fuelFooter').textContent=[model?`Model domu z ${model.periods_used} okresów między dosypkami (R² ${fuelNum(model.r2,2)})`:'Za mało dosypek na model domu',
    counterNote||(cc?.calibrated?`licznik podajnika ${fuelNum(cc.kg_per_percent,2)} kg/%`:null),
    calib.calibrated?`moc palnika ×${fuelNum(calib.factor,2)}`:calib.provisional?`moc palnika ×${fuelNum(calib.factor,2)} (szacunek do kalibracji po zmianie podajnika)`:null,
    `stopniodni przy bazie ${fuelNum(summary.hdd_base??model?.balance_temp,1)} °C`,
    model?.base_fixed&&model.best_fit_balance_temp!=null&&model.best_fit_balance_temp!==model.balance_temp?`dopasowanie bazy (informacyjnie): ${fuelNum(model.best_fit_balance_temp,1)} °C`:null,
    offset?.applied?`Open-Meteo skorygowane o ${fuelNum(offset.offset,1)} K do czujnika sterownika`:null,
    forecast?`prognoza zużycia na 3 dni: ${forecast}`:null,
    dmText,
    'stan zasobnika zakłada dosypywanie do pełna, rezerwa 15% to zalecane minimum z instrukcji kotła',
    hopper?.method||null,fuelDate(summary.fetched_at)].filter(Boolean).join(' · ');
  $('fuelCard').hidden=false;}
let fuelRetry=null;
async function loadFuel(){try{const response=await ThermoFlowApi.get('/api/fuel/summary');if(!response.ok)return;const summary=await response.json();if(summary.warming){clearTimeout(fuelRetry);fuelRetry=setTimeout(loadFuel,15000);return;}if(summary.configured)renderFuel(summary);}catch(_){}}
function tick(){const d=new Date();$('clock').textContent=d.toLocaleTimeString('pl-PL',{hour:'2-digit',minute:'2-digit',second:'2-digit'});}tick();setInterval(tick,1000);loadStatus();setInterval(loadStatus,30000);loadFuel();setInterval(loadFuel,300000);
$('pauseBtn').onclick=()=>{paused=!paused;document.body.classList.toggle('flowpaused',paused);const svg=document.querySelector('.schematic');if(paused)svg.pauseAnimations();else svg.unpauseAnimations();$('pauseBtn').innerHTML=paused?'▶ &nbsp; Wznów':'Ⅱ &nbsp; Wstrzymaj';};
