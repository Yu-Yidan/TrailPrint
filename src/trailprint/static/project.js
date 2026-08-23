const shape = document.querySelector('#shape');
const extra = document.querySelector('#shape-extra');
const resolution = document.querySelector('#resolution');
const resolutionValue = document.querySelector('#resolution-value');
const resolutionNote = document.querySelector('#resolution-note');

function refreshExtras() {
  extra.replaceChildren(...window.TRAILPRINT_EXTRAS[shape.value].map(value => {
    const option = document.createElement('option'); option.value = value;
    option.textContent = window.TRAILPRINT_EXTRA_LABELS[value]; return option;
  }));
}
shape.addEventListener('change', refreshExtras); refreshExtras();
resolution.addEventListener('input', () => {
  resolutionValue.textContent = resolution.value;
  resolutionNote.classList.toggle('warning', Number(resolution.value) > 8);
});

const statusBox = document.querySelector('#build-status');
if (statusBox && ['queued', 'running'].includes(statusBox.classList[1])) {
  const poll = async () => {
    const response = await fetch(`/builds/${statusBox.dataset.jobId}`);
    if (!response.ok) return;
    const job = await response.json();
    statusBox.className = `build-status ${job.state}`;
    document.querySelector('#job-state').textContent = job.state[0].toUpperCase() + job.state.slice(1);
    document.querySelector('#job-message').textContent = job.phase || job.message;
    document.querySelector('#job-elapsed').textContent = `${job.elapsed_seconds}s elapsed`;
    if (job.percent != null) document.querySelector('#job-progress').style.width = `${job.percent}%`;
    if (['queued', 'running'].includes(job.state)) setTimeout(poll, 1200); else setTimeout(() => location.reload(), 1000);
  };
  poll();
}
