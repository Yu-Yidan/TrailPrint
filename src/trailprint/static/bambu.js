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


const editor = document.querySelector('#band-editor');
function initializeBandEditor(editor) {
  const config = JSON.parse(editor.dataset.settings);
  const e = config.elevation;
  const advanced = document.querySelector('#advanced-terrain');
  const controls = document.querySelector('#band-controls');
  const terrain = document.querySelector('#terrain-color');
  const hidden = document.querySelector('#terrain-bands');
  const scale = document.querySelector('#band-scale');
  const rows = document.querySelector('#band-rows');
  const error = document.querySelector('#band-error');
  const form = document.querySelector('#bambu-form');
  const validElevation = e && Number.isFinite(e.source_min_m) && e.source_max_m > e.source_min_m;
  const unit = e.unit_system === 'METRIC' ? 'm' : 'ft';
  const factor = unit === 'm' ? 1 : 1 / .3048;
  const low = e.source_min_m || 0, high = e.source_max_m || 1;
  const relief = e.model_surface_max_z_mm - e.model_surface_min_z_mm;
  const span = high-low;
  const minWidth = span * config.layer_height / relief;
  let bands = config.saved.bands?.length ? config.saved.bands.map(x => ({...x})) : [{upper_m:high,color:terrain.value}];
  if (validElevation) bands[bands.length-1].upper_m=high;
  const previous = i => i ? bands[i-1].upper_m : low;
  const number = x => (x*factor).toFixed(1);
  function store() { hidden.value = JSON.stringify(bands); }
  function check() {
    const invalid = advanced.checked && (!validElevation || bands.some((b,i) => !config.colors[b.color] || !Number.isFinite(b.upper_m) || b.upper_m-previous(i)<minWidth-1e-6) || Math.abs(bands.at(-1).upper_m-high)>1e-6);
    error.textContent = invalid ? 'Each band must cover at least one nominal layer within the current elevation range.' : '';
    document.querySelector('#add-band').disabled = bands.length>=5 || !validElevation;
    return !invalid;
  }
  function boundary(index,value) {
    const minimum = previous(index)+minWidth;
    const maximum = bands[index+1].upper_m-minWidth;
    if (minimum>maximum) return;
    bands[index].upper_m = Math.max(minimum, Math.min(maximum,value));
    render();
  }
  function input(type,value,callback) {
    const element = document.createElement('input');element.type=type;element.value=value;element.className='text-input';
    if(type==='number') element.step='any';
    element.addEventListener('change',()=>callback(element.value));return element;
  }
  function render() {
    controls.hidden=!advanced.checked;
    terrain.parentElement.hidden=advanced.checked;
    scale.replaceChildren();rows.replaceChildren();
    bands.map((b,i)=>({b,i})).reverse().forEach(({b,i}) => {
      const segment = document.createElement('div');segment.className='band-segment';
      segment.style.background=config.colors[b.color]?.[1] || '#777';
      segment.title=`${number(previous(i))}–${number(b.upper_m)} ${unit}`;scale.append(segment);
      if(i>0) {
        const index=i-1;
        const divider=document.createElement('button');divider.type='button';divider.className='band-divider';
        divider.dataset.boundary=index;
        divider.setAttribute('aria-label',`Elevation divider ${index+1}`);divider.setAttribute('role','slider');divider.setAttribute('aria-orientation','vertical');
        divider.setAttribute('aria-valuemin',number(previous(index)+minWidth));divider.setAttribute('aria-valuemax',number(bands[i].upper_m-minWidth));divider.setAttribute('aria-valuenow',number(bands[index].upper_m));divider.textContent='↕';
        divider.addEventListener('pointerdown',event=>{
          event.preventDefault();
          const startY=event.clientY,startValue=bands[index].upper_m,height=scale.getBoundingClientRect().height;
          const move=event=>boundary(index,startValue+(startY-event.clientY)/height*span);
          const stop=()=>{window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',stop);window.removeEventListener('pointercancel',stop);};
          window.addEventListener('pointermove',move);window.addEventListener('pointerup',stop);window.addEventListener('pointercancel',stop);
        });
        divider.addEventListener('keydown',event=>{
          if(['ArrowDown','ArrowUp'].includes(event.key)) {event.preventDefault();boundary(index,bands[index].upper_m+(event.key==='ArrowUp'?minWidth:-minWidth));scale.querySelector(`[data-boundary="${index}"]`)?.focus();}
        });segment.append(divider);
      }
      const row=document.createElement('div');row.className='terrain-band-row';row.dataset.bandIndex=i;
      const label=document.createElement('strong');label.className='band-range';label.textContent=`${number(previous(i))}–${number(b.upper_m)} ${unit}`;row.append(label);
      const line=document.createElement('div');line.className='band-control-line';row.append(line);
      const color=document.createElement('select');color.className='text-input';color.setAttribute('aria-label',`Color for ${label.textContent}`);
      Object.entries(config.colors).forEach(([key,value])=>{const option=new Option(value[0],key);option.selected=b.color===key;color.add(option);});
      color.addEventListener('change',()=>{b.color=color.value;render();});line.append(color);
      const end=document.createElement('label');end.textContent=`Lower elevation (${unit})`;
      const value=input('number',number(previous(i)),value=>boundary(i-1,Number(value)/factor));
      value.readOnly=i===0;end.append(value);line.append(end);
      const remove=document.createElement('button');remove.type='button';remove.className='button secondary';remove.textContent='Remove divider';remove.disabled=i===0;
      remove.addEventListener('click',()=>{bands[i-1].upper_m=b.upper_m;bands.splice(i,1);render();});line.append(remove);
      const thickness=(b.upper_m-previous(i))/span*relief;
      const detail=document.createElement('output');detail.className='band-thickness';detail.textContent=`${thickness.toFixed(2)} mm · approximately ${(thickness/config.layer_height).toFixed(2)} layers`;row.append(detail);
      rows.append(row);
    });store();check();
  }
  advanced.disabled=!validElevation;
  if(!validElevation) advanced.checked=false;
  advanced.addEventListener('change',()=>{if(advanced.checked && bands.length===1) bands[0].color=terrain.value;render();});
  terrain.addEventListener('change',()=>{if(bands.length===1) bands[0].color=terrain.value;render();});
  document.querySelector('#add-band').addEventListener('click',()=>{
    if(bands.length>=5)return;
    const widths=bands.map((b,i)=>b.upper_m-previous(i));const i=bands.length-1;
    if(widths[i]<2*minWidth) {error.textContent='The highest band is too thin to split.';return;}
    const end=bands[i].upper_m;bands[i].upper_m=previous(i)+widths[i]/2;bands.splice(i+1,0,{upper_m:end,color:bands[i].color});render();
  });
  document.querySelector('#reset-bands').addEventListener('click',()=>{bands=[{upper_m:high,color:terrain.value}];render();});
  const osm=document.querySelector('#osm-bands');osm.hidden=!config.suggestions.length;
  osm.addEventListener('click',()=>{const colors={black:'charcoal',green:'green',grey:'nardo',white:'ivory'};bands=config.suggestions.map(b=>({upper_m:b.max_elevation_m,color:colors[b.role]||'green'}));render();});
  form.addEventListener('submit',event=>{store();if(!check())event.preventDefault();});render();
}

const refreshButton = document.querySelector('#refresh-preview');
function initializePreviewRefresh(refreshButton) {
  const form = document.querySelector('#bambu-form');
  const status = document.querySelector('#preview-refresh-status');
  let edits = 0;
  const edited = () => { edits++; status.textContent = 'Preview outdated — refresh to see your edits.'; };
  form.addEventListener('input', edited);
  form.addEventListener('change', edited);
  form.addEventListener('click', event => {
    if (event.target.closest('#add-band,#reset-bands,#osm-bands,.terrain-band-row button')) edited();
  });
  const bandsInput = document.querySelector('#terrain-bands');
  let previousBands = bandsInput.value;
  const observer = new MutationObserver(() => {
    if (previousBands !== bandsInput.value) { previousBands = bandsInput.value; edited(); }
  });
  observer.observe(document.querySelector('#band-rows'), {childList:true});
  refreshButton.addEventListener('click', async () => {
    if (!form.reportValidity()) return;
    const version = edits;
    refreshButton.disabled = true;
    const exportButton = form.querySelector('button[type="submit"]');
    const wasDisabled = exportButton.disabled;
    exportButton.disabled = true;
    status.textContent = 'Rendering previews…';
    try {
      const response = await fetch(refreshButton.dataset.url, {method:'POST',body:new FormData(form)});
      const started = await response.json();
      if (!response.ok) throw Error(started.error || 'Preview refresh failed');
      let job;
      do {
        await new Promise(resolve => setTimeout(resolve, 800));
        const result = await fetch(refreshButton.dataset.statusUrl.replace('PREVIEW_JOB_ID', encodeURIComponent(started.id)));
        if (!result.ok) throw Error('Could not check preview status');
        job = await result.json();
        status.textContent = job.message;
      } while (['queued','running'].includes(job.state));
      if (job.state !== 'success') throw Error(job.message);
      if (job.geometry_changed) { window.location.reload(); return; }
      document.querySelectorAll('.preview-images img').forEach(img => {
        const url = new URL(img.src);url.searchParams.set('v',job.revision);img.src=url.href;
      });
      status.textContent = edits === version ? 'Preview refreshed. Generate a 3MF to export these colors.' : 'Preview outdated — settings changed during rendering. Refresh again.';
    } catch (error) {status.textContent = error.message;}
    finally {refreshButton.disabled=false;exportButton.disabled=wasDisabled;}
  });
}

if (editor) initializeBandEditor(editor);
if (refreshButton) initializePreviewRefresh(refreshButton);
