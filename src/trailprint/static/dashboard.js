const funnelTabs = [...document.querySelectorAll('.funnel-tab')];
const funnelPanels = [...document.querySelectorAll('[data-stage-panel]')];
const tabList = document.querySelector('.funnel-tabs');

if (funnelTabs.length && tabList) {
  const validStages = new Set(funnelTabs.map(tab => tab.dataset.stage));
  const remembered = sessionStorage.getItem('trailprint-dashboard-stage');
  const initial = validStages.has(remembered) ? remembered : tabList.dataset.defaultStage;
  const selectStage = stage => {
    funnelTabs.forEach(tab => {
      const selected = tab.dataset.stage === stage;
      tab.setAttribute('aria-selected', selected ? 'true' : 'false');
      tab.tabIndex = selected ? 0 : -1;
    });
    funnelPanels.forEach(panel => { panel.hidden = panel.dataset.stagePanel !== stage; });
    sessionStorage.setItem('trailprint-dashboard-stage', stage);
  };
  funnelTabs.forEach(tab => tab.addEventListener('click', () => selectStage(tab.dataset.stage)));
  selectStage(initial);
}
