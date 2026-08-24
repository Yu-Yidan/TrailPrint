const bambuStatus = document.querySelector('#bambu-status');
if (bambuStatus && ['queued', 'running'].includes(bambuStatus.classList[1])) {
  const pollBambu = async () => {
    const response = await fetch(`/bambu-builds/${bambuStatus.dataset.jobId}`);
    if (!response.ok) return;
    const job = await response.json();
    bambuStatus.className = `build-status ${job.state}`;
    document.querySelector('#bambu-state').textContent = job.state[0].toUpperCase() + job.state.slice(1);
    document.querySelector('#bambu-message').textContent = job.message;
    document.querySelector('#bambu-elapsed').textContent = `${job.elapsed_seconds}s elapsed`;
    if (['queued', 'running'].includes(job.state)) setTimeout(pollBambu, 1200);
    else if (job.state === 'success') {
      try {
        await fetch(bambuStatus.dataset.openFolderUrl, {
          method: 'POST',
          credentials: 'same-origin',
        });
      } finally {
        setTimeout(() => { location.href = bambuStatus.dataset.homeUrl; }, 500);
      }
    }
  };
  pollBambu();
}

document.querySelectorAll('.filament-grid select').forEach(select => {
  select.addEventListener('change', () => {
    const option = select.options[select.selectedIndex];
    select.parentElement.querySelector('.filament-dot')
      .style.setProperty('--filament-color', option.dataset.color);
  });
});
