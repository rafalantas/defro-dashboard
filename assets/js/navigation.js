/* View navigation only; live values are rendered from API events by overview.js. */
(function(){
  const tabs=[...document.querySelectorAll('[data-view-target]')];
  const panels=[...document.querySelectorAll('[data-view]')];
  function activate(view){
    for(const panel of panels)panel.classList.toggle('view-hidden',panel.dataset.view!==view);
    for(const tab of tabs){const active=tab.dataset.viewTarget===view;tab.classList.toggle('active',active);tab.setAttribute('aria-selected',String(active));}
  }
  tabs.forEach(tab=>tab.addEventListener('click',()=>activate(tab.dataset.viewTarget)));
  activate('overview');
})();
