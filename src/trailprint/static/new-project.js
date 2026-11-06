const input = document.querySelector('#gpx');
const zone = document.querySelector('#drop-zone');
const nameInput = document.querySelector('#name');
const title = document.querySelector('#file-title');
const help = document.querySelector('#file-help');
const preview = document.querySelector('#slug-preview');
const selectedFiles = document.querySelector('#selected-files');

function slugify(value) {
  return value.normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '') || 'project_name';
}
function updateSlug() { preview.textContent = slugify(nameInput.value); }
function selectFiles(files) {
  selectedFiles.replaceChildren();
  selectedFiles.hidden = !files.length;
  input.setCustomValidity('');
  if (!files.length) {
    title.textContent = 'Drop your GPX files here';
    help.textContent = 'or click to choose files · maximum 10 MB total';
    zone.classList.remove('selected');
    return;
  }
  const total = Array.from(files).reduce((sum, file) => sum + file.size, 0);
  title.textContent = files.length === 1 ? files[0].name : `${files.length} GPX files selected`;
  help.textContent = `${(total / 1024).toFixed(1)} KB total · maximum 10 MB`;
  if (total > 10 * 1024 * 1024) input.setCustomValidity('Combined GPX uploads must be 10 MB or smaller.');
  for (const file of files) {
    const item = document.createElement('li');
    item.textContent = file.name;
    selectedFiles.append(item);
  }
  zone.classList.add('selected');
  if (!nameInput.value.trim()) nameInput.value = files[0].name.replace(/\.gpx$/i, '').replace(/[-_]+/g, ' ');
  updateSlug();
}
input.addEventListener('change', () => selectFiles(input.files));
nameInput.addEventListener('input', updateSlug);
['dragenter', 'dragover'].forEach(event => zone.addEventListener(event, e => { e.preventDefault(); zone.classList.add('dragging'); }));
['dragleave', 'drop'].forEach(event => zone.addEventListener(event, e => { e.preventDefault(); zone.classList.remove('dragging'); }));
zone.addEventListener('drop', event => {
  if (!event.dataTransfer.files.length) return;
  const transfer = new DataTransfer();
  for (const file of event.dataTransfer.files) transfer.items.add(file);
  input.files = transfer.files;
  selectFiles(input.files);
});
updateSlug();
