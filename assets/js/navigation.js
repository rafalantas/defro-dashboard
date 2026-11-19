/* View navigation and read-only Overview projection. */
(function(){
  const tabs=[...document.querySelectorAll('[data-view-target]')];
  const panels=[...document.querySelectorAll('[data-view]')];
  function value(id){const el=document.getElementById(id);return el?el.textContent.trim():'—';}
  function refreshOverview(){
    const pairs={overviewCo:'tileCo',overviewReturn:'mReturn',overviewOutdoor:'tileOutdoor',overviewFlue:'tileFlue',overviewFuel:'tileFuel',overviewBoilerState:'tileControllerState'};
    for(const [target,source] of Object.entries(pairs)){const el=document.getElementById(target);if(el)el.textContent=value(source);}
    const badge=document.getElementById('overviewConnection'),mode=document.getElementById('modeBadge');
    if(badge&&mode){badge.textContent=mode.textContent;badge.className=mode.className;}
  }
  function activate(view){
    for(const panel of panels)panel.classList.toggle('view-hidden',panel.dataset.view!==view);
    for(const tab of tabs){const active=tab.dataset.viewTarget===view;tab.classList.toggle('active',active);tab.setAttribute('aria-selected',String(active));}
    refreshOverview();
  }
  tabs.forEach(tab=>tab.addEventListener('click',()=>activate(tab.dataset.viewTarget)));
  const reference=window.ThermoFlowReferenceModel;
  const referenceNote=document.getElementById('overviewReferenceModel');
  if(reference&&referenceNote) referenceNote.textContent=`Model referencyjny: ${reference.heatingKgPerHdd.toLocaleString('pl-PL',{minimumFractionDigits:2,maximumFractionDigits:2})} kg/HDD + ${reference.fixedDailyKg} kg/dobę · baza ${reference.hddBaseC.toLocaleString('pl-PL',{minimumFractionDigits:1,maximumFractionDigits:1})}°C. Model eksperymentalny z danych pozostaje osobny.`;
  activate('overview');
  window.setInterval(refreshOverview,3000);
})();
