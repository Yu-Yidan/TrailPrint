const poll = (root, endpoint, cancelEndpoint) => {
  if (!root) return;
  const started = Date.now();
  const update = async () => {
    try {
      const response = await fetch(endpoint);
      const data = await response.json();
      root.className = `build-status ${data.state} ${root.classList.contains('coaster-bambu-status') ? 'coaster-bambu-status' : 'coaster-job-status'}`;
      root.querySelector('.job-state').textContent = data.state[0].toUpperCase() + data.state.slice(1);
      root.querySelector('.job-message').textContent = data.message;
      root.querySelector('.job-elapsed').textContent = `${Math.round(data.elapsed_seconds)} seconds`;
      if (['success', 'error', 'cancelled'].includes(data.state)) window.location.reload();
      else window.setTimeout(update, 1000);
    } catch (_error) { window.setTimeout(update, 2000); }
  };
  const cancel = root.querySelector('.coaster-cancel');
  if (cancel && cancelEndpoint) cancel.addEventListener('click', async () => {
    cancel.disabled = true;
    await fetch(cancelEndpoint, { method: 'POST' });
    update();
  });
  update();
};

const build = document.querySelector('.coaster-job-status.queued, .coaster-job-status.running, .coaster-job-status.cancelling');
if (build) poll(build, `/coaster-builds/${build.dataset.jobId}`, `/coaster-builds/${build.dataset.jobId}/cancel`);
const bambu = document.querySelector('.coaster-bambu-status.queued, .coaster-bambu-status.running');
if (bambu) poll(bambu, `/coaster-bambu-builds/${bambu.dataset.jobId}`);

document.querySelectorAll('.filament-grid select').forEach(select => {
  select.addEventListener('change', () => {
    const dot = select.parentElement.querySelector('.filament-dot');
    dot.style.setProperty('--filament-color', select.selectedOptions[0].dataset.color);
  });
});
