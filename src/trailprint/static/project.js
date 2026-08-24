const shape = document.querySelector('#shape');
const extra = document.querySelector('#shape-extra');
const resolution = document.querySelector('#resolution');
const resolutionValue = document.querySelector('#resolution-value');
const resolutionNote = document.querySelector('#resolution-note');
const orientation = document.querySelector('#text-orientation');
const orientationShape = document.querySelector('#orientation-shape');
const orientationNote = document.querySelector('#orientation-note');
const plateTextSettings = document.querySelector('#plate-text-settings');
const locationStatus = document.querySelector('.location-status');
const titleInput = document.querySelector('#plate-title');
const titleWarning = document.querySelector('#title-fit-warning');
const buildButton = document.querySelector('.build-button');
const buildInitiallyAllowed = !buildButton.disabled;
const titleMeasure = document.createElement('canvas').getContext('2d');
const metricToggle = document.querySelector('#use-metric');
const distanceText = document.querySelector('#plate-text-1');
const plateRecessSetting = document.querySelector('#plate-recess-setting');

const orientationNotes = {
  HEXAGON: 'Text 1 lower-left · Text 2 bottom · Text 3 lower-right',
  OCTAGON: 'Text 1 lower-left · Text 2 bottom · Text 3 lower-right',
  CIRCLE: 'Text 1 left · Text 2 bottom · Text 3 right',
};

function measureTitleWidthMm(value) {
  titleMeasure.font = '1000px "Arial Black", Arial, sans-serif';
  const reference = titleMeasure.measureText('Name');
  const referenceHeight = reference.actualBoundingBoxAscent + reference.actualBoundingBoxDescent || 720;
  return titleMeasure.measureText(value).width / referenceHeight * 5;
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

function refreshOrientation() {
  const hasText = extra.value.includes('TEXT');
  plateTextSettings.hidden = !hasText;
  orientation.hidden = !hasText;
  validateTitleFit();
  if (!hasText) return;
  orientationShape.className = `orientation-shape ${shape.value.toLowerCase()}`;
  const usesFive = shape.value === 'HEXAGON' && extra.value === 'OUTER TEXT';
  const hasPlate = extra.value === 'OUTER TEXT' || (shape.value === 'HEXAGON' && extra.value === 'FRONT TEXT');
  plateRecessSetting.hidden = !hasPlate;
  plateTextSettings.querySelectorAll('[data-text-field="4"], [data-text-field="5"]')
    .forEach(field => { field.hidden = !usesFive; });
  locationStatus.hidden = !usesFive;
  orientationShape.querySelectorAll('[data-slot="4"], [data-slot="5"]').forEach(label => { label.hidden = !usesFive; });
  orientationNote.textContent = `${orientationNotes[shape.value] || ''}${usesFive ? ' · Text 4 upper-right · Text 5 upper-left' : ' · Text 4–5 unused'}`;
}

function refreshExtras() {
  const extras = window.TRAILPRINT_EXTRAS[shape.value];
  extra.replaceChildren(...extras.map(value => {
    const option = document.createElement('option'); option.value = value;
    option.textContent = window.TRAILPRINT_EXTRA_LABELS[value]; return option;
  }));
  extra.value = extras.includes('OUTER TEXT') ? 'OUTER TEXT' : 'NONE';
  refreshOrientation();
}
shape.addEventListener('change', refreshExtras);
extra.addEventListener('change', refreshOrientation);
refreshExtras();
titleInput.addEventListener('input', validateTitleFit);
if (document.fonts) document.fonts.ready.then(validateTitleFit);
resolution.addEventListener('input', () => {
  resolutionValue.textContent = resolution.value;
  resolutionNote.classList.toggle('warning', Number(resolution.value) > 8);
});

if (metricToggle && distanceText) {
  let generatedDistance = window.TRAILPRINT_TRIP_VALUES.IMPERIAL.length;
  distanceText.addEventListener('input', () => { distanceText.dataset.edited = 'true'; });
  metricToggle.addEventListener('change', () => {
    const system = metricToggle.checked ? 'METRIC' : 'IMPERIAL';
    const nextDistance = window.TRAILPRINT_TRIP_VALUES[system].length;
    if (distanceText.dataset.edited !== 'true' || distanceText.value === generatedDistance) {
      distanceText.value = nextDistance;
      distanceText.dataset.edited = 'false';
    }
    generatedDistance = nextDistance;
  });
}

if (plateTextSettings) {
  const countryText = document.querySelector('#plate-text-4');
  const stateText = document.querySelector('#plate-text-5');
  const cityValue = document.querySelector('#location-city');
  const stateValue = document.querySelector('#location-state');
  const countryValue = document.querySelector('#location-country');
  const status = document.querySelector('#location-status');
  const retry = document.querySelector('#retry-location');
  [[countryText, countryValue], [stateText, stateValue]].forEach(([field, hidden]) => {
    field.addEventListener('input', () => {
      field.dataset.edited = 'true';
      hidden.value = field.value;
    });
  });
  const detect = async () => {
    status.textContent = 'Detecting location…'; retry.hidden = true;
    try {
      const response = await fetch(plateTextSettings.dataset.endpoint, { method: 'POST' });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Location lookup failed');
      cityValue.value = result.city || '';
      [[countryText, countryValue, result.country], [stateText, stateValue, result.state]]
        .forEach(([field, hidden, value]) => {
          if (field.dataset.edited !== 'true') field.value = value || '';
          hidden.value = field.value;
        });
      status.textContent = 'Detected from the first GPX point';
    } catch (error) {
      status.textContent = `${error.message}. You can enter the location manually.`;
    } finally { retry.hidden = false; }
  };
  retry.addEventListener('click', detect);
  if (plateTextSettings.dataset.cached !== 'true') detect();
}

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
