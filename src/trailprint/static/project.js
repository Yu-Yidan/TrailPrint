const shape = document.querySelector('#shape');
const extra = document.querySelector('#shape-extra');
const resolution = document.querySelector('#resolution');
const resolutionValue = document.querySelector('#resolution-value');
const resolutionNote = document.querySelector('#resolution-note');
const plateTextSettings = document.querySelector('#plate-text-settings');
const titleInput = document.querySelector('#plate-title');
const titleWarning = document.querySelector('#title-fit-warning');
const buildButton = document.querySelector('.build-button');
const buildInitiallyAllowed = !buildButton.disabled;
const titleMeasure = document.createElement('canvas').getContext('2d');
const dotMagnetSetting = document.querySelector('#dot-magnet-setting');

function measureTitleWidthMm(value) {
  titleMeasure.font = window.TRAILPRINT_MODEL_FONT_CSS;
  const reference = titleMeasure.measureText('Name');
  const referenceHeight = reference.actualBoundingBoxAscent + reference.actualBoundingBoxDescent || 720;
  return titleMeasure.measureText(value).width / referenceHeight * 5 * window.TRAILPRINT_TITLE_WIDTH_SCALE;
}

function validateTitleFit() {
  const limit = window.TRAILPRINT_TITLE_LIMITS[shape.value]?.[extra.value];
  const width = measureTitleWidthMm(titleInput.value);
  const overflowing = limit != null && width > limit;
  titleWarning.hidden = !overflowing;
  titleWarning.textContent = overflowing
    ? `Title is approximately ${width.toFixed(1)} mm wide; this layout allows ${Number(limit).toFixed(1)} mm. Shorten the title to build.`
    : '';
  titleInput.classList.toggle('invalid', overflowing);
  titleInput.setAttribute('aria-invalid', overflowing ? 'true' : 'false');
  titleInput.setCustomValidity(overflowing ? 'Title is too long for this layout.' : '');
  buildButton.disabled = !buildInitiallyAllowed || overflowing;
}

function refreshTextFields() {
  const hasText = extra.value.includes('TEXT');
  plateTextSettings.hidden = !hasText;
  validateTitleFit();
  if (!hasText) return;
  const usesFive = shape.value === 'HEXAGON' && extra.value === 'OUTER TEXT';
  const hasPlate = extra.value === 'OUTER TEXT' || (shape.value === 'HEXAGON' && extra.value === 'FRONT TEXT');
  dotMagnetSetting.hidden = !hasPlate;
  plateTextSettings.querySelectorAll('[data-text-field="4"], [data-text-field="5"]')
    .forEach(field => { field.hidden = !usesFive; });
  plateTextSettings.querySelector('[data-upper-text-row]').hidden = !usesFive;
}

function refreshExtras(initialValue = null) {
  const extras = window.TRAILPRINT_EXTRAS[shape.value];
  extra.replaceChildren(...extras.map(value => {
    const option = document.createElement('option'); option.value = value;
    option.textContent = window.TRAILPRINT_EXTRA_LABELS[value]; return option;
  }));
  extra.value = extras.includes(initialValue) ? initialValue : (extras.includes('OUTER TEXT') ? 'OUTER TEXT' : 'NONE');
  refreshTextFields();
}
shape.addEventListener('change', () => refreshExtras());
extra.addEventListener('change', refreshTextFields);
refreshExtras(extra.dataset.initialValue);
titleInput.addEventListener('input', validateTitleFit);
if (document.fonts) document.fonts.ready.then(validateTitleFit);
function refreshResolution() {
  resolutionValue.textContent = resolution.value;
  resolutionNote.classList.toggle('warning', Number(resolution.value) > 8);
}
resolution.addEventListener('input', refreshResolution);
refreshResolution();

const statusBox = document.querySelector('#build-status');
if (statusBox && ['queued', 'running', 'cancelling'].includes(statusBox.classList[1])) {
  const cancelBuild = document.querySelector('#cancel-build');
  if (cancelBuild) cancelBuild.addEventListener('click', async () => {
    cancelBuild.disabled = true;
    cancelBuild.textContent = 'Cancelling…';
    const response = await fetch(cancelBuild.dataset.cancelUrl, { method: 'POST' });
    if (!response.ok) cancelBuild.textContent = 'Could not cancel';
  });
  const poll = async () => {
    const response = await fetch(`/builds/${statusBox.dataset.jobId}`);
    if (!response.ok) return;
    const job = await response.json();
    statusBox.className = `build-status ${job.state}`;
    document.querySelector('#job-state').textContent = job.state[0].toUpperCase() + job.state.slice(1);
    document.querySelector('#job-message').textContent = job.phase || job.message;
    document.querySelector('#job-elapsed').textContent = `${job.elapsed_seconds}s elapsed`;
    if (job.percent != null) document.querySelector('#job-progress').style.width = `${job.percent}%`;
    if (cancelBuild) cancelBuild.hidden = !job.can_cancel;
    if (['queued', 'running', 'cancelling'].includes(job.state)) setTimeout(poll, 600);
    else if (job.state === 'success' && job.preview_url) window.location.assign(job.preview_url);
    else setTimeout(() => location.reload(), 1000);
  };
  poll();
}
