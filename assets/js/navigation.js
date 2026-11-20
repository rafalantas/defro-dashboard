/* View navigation only; live values are rendered from API events by overview.js. */
(function(){
  'use strict';

  const tabs = [...document.querySelectorAll('[data-view-target]')];
  const panels = [...document.querySelectorAll('[data-view]')];
  if (!tabs.length || !panels.length) return;

  const storageKey = 'thermoflow.activeView';
  const views = new Set(tabs.map(tab => tab.dataset.viewTarget));
  const readSavedView = () => {
    try {
      const saved = window.localStorage.getItem(storageKey);
      return views.has(saved) ? saved : 'overview';
    } catch (_) {
      return 'overview';
    }
  };

  function activate(view, options = {}) {
    if (!views.has(view)) view = 'overview';

    for (const panel of panels) {
      const visible = panel.dataset.view === view;
      panel.hidden = !visible;
      panel.classList.toggle('view-hidden', !visible);
    }

    let activeIndex = 0;
    tabs.forEach((tab, index) => {
      const active = tab.dataset.viewTarget === view;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      if (active) activeIndex = index;
    });

    if (options.persist !== false) {
      try {
        window.localStorage.setItem(storageKey, view);
      } catch (_) {
        // Navigation remains usable when browser storage is unavailable.
      }
    }
    return activeIndex;
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => activate(tab.dataset.viewTarget));
    tab.addEventListener('keydown', event => {
      let nextIndex;
      if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === 'Home') nextIndex = 0;
      else if (event.key === 'End') nextIndex = tabs.length - 1;
      else return;

      event.preventDefault();
      tabs[nextIndex].focus();
      activate(tabs[nextIndex].dataset.viewTarget);
    });
  });

  activate(readSavedView(), {persist: false});
})();
