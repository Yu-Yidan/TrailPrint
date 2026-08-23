const dialog = document.querySelector('#delete-dialog');
if (dialog) {
  const form = document.querySelector('#delete-form');
  const input = document.querySelector('#delete-confirmation');
  const confirm = document.querySelector('#delete-confirm');
  const title = document.querySelector('#delete-title');
  const slugLabel = document.querySelector('#delete-slug-label');
  let expected = '';

  document.querySelectorAll('[data-delete-slug]').forEach(button => button.addEventListener('click', () => {
    expected = button.dataset.deleteSlug;
    title.textContent = button.dataset.deleteTitle;
    slugLabel.textContent = expected;
    form.action = `/projects/${encodeURIComponent(expected)}/delete`;
    input.value = '';
    confirm.disabled = true;
    dialog.showModal();
    input.focus();
  }));
  input.addEventListener('input', () => { confirm.disabled = input.value !== expected; });
  const close = () => dialog.close();
  document.querySelector('#delete-cancel').addEventListener('click', close);
  document.querySelector('.dialog-close').addEventListener('click', close);
  dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
}
