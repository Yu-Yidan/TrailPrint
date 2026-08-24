const deletedCheckboxes = [...document.querySelectorAll('.deleted-checkbox')];
const selectAllDeleted = document.querySelector('#select-all-deleted');
const selectedCount = document.querySelector('#selected-count');
const openPermanentDelete = document.querySelector('#open-permanent-delete');
const permanentDialog = document.querySelector('#permanent-delete-dialog');

if (deletedCheckboxes.length && selectAllDeleted && permanentDialog) {
  const confirmation = document.querySelector('#permanent-confirmation');
  const confirmButton = document.querySelector('#confirm-permanent-delete');
  const dialogCount = document.querySelector('#permanent-delete-count');

  const updateSelection = () => {
    const count = deletedCheckboxes.filter(checkbox => checkbox.checked).length;
    selectedCount.textContent = `${count} selected`;
    openPermanentDelete.disabled = count === 0;
    selectAllDeleted.checked = count === deletedCheckboxes.length;
    selectAllDeleted.indeterminate = count > 0 && count < deletedCheckboxes.length;
  };

  selectAllDeleted.addEventListener('change', () => {
    deletedCheckboxes.forEach(checkbox => { checkbox.checked = selectAllDeleted.checked; });
    updateSelection();
  });
  deletedCheckboxes.forEach(checkbox => checkbox.addEventListener('change', updateSelection));
  openPermanentDelete.addEventListener('click', () => {
    const count = deletedCheckboxes.filter(checkbox => checkbox.checked).length;
    if (!count) return;
    dialogCount.textContent = `${count} project${count === 1 ? '' : 's'}`;
    confirmation.value = '';
    confirmButton.disabled = true;
    permanentDialog.showModal();
    confirmation.focus();
  });
  confirmation.addEventListener('input', () => {
    confirmButton.disabled = confirmation.value !== 'DELETE';
  });
  const close = () => permanentDialog.close();
  document.querySelector('#cancel-permanent-delete').addEventListener('click', close);
  permanentDialog.querySelector('.dialog-close').addEventListener('click', close);
  permanentDialog.addEventListener('click', event => {
    if (event.target === permanentDialog) close();
  });
  updateSelection();
}
