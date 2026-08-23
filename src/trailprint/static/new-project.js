const input = document.querySelector('#gpx');
const zone = document.querySelector('#drop-zone');
const nameInput = document.querySelector('#name');
const title = document.querySelector('#file-title');
const help = document.querySelector('#file-help');
const preview = document.querySelector('#slug-preview');

function slugify(value) {
  return value.normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'project-name';
}
function updateSlug() { preview.textContent = slugify(nameInput.value); }
function selectFile(file) {
  if (!file) return;
  title.textContent = file.name;
  help.textContent = `${(file.size / 1024).toFixed(1)} KB · ready to upload`;
  zone.classList.add('selected');
  if (!nameInput.value.trim()) nameInput.value = file.name.replace(/\.gpx$/i, '').replace(/[-_]+/g, ' ');
  updateSlug();
}
input.addEventListener('change', () => selectFile(input.files[0]));
nameInput.addEventListener('input', updateSlug);
['dragenter', 'dragover'].forEach(event => zone.addEventListener(event, e => { e.preventDefault(); zone.classList.add('dragging'); }));
['dragleave', 'drop'].forEach(event => zone.addEventListener(event, e => { e.preventDefault(); zone.classList.remove('dragging'); }));
zone.addEventListener('drop', event => {
  if (!event.dataTransfer.files.length) return;
  const transfer = new DataTransfer(); transfer.items.add(event.dataTransfer.files[0]); input.files = transfer.files;
  selectFile(input.files[0]);
});
updateSlug();
