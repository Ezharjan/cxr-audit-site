const API_BASE_URL = window.PAGE_CONFIG.apiBaseUrl;
let currentTaskId = null;
let statusCheckInterval = null;
let csrfToken = window.PAGE_CONFIG.csrfToken;
let currentResults = null;
let previewId = null;
let previousPreviewId = null;
let uploadCheck = null;
let mappingManifest = null;
const escapeHtml = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// ══════════ Initialization ══════════
document.addEventListener('DOMContentLoaded', async () => {
    await checkApiConnection();
    checkLlmConnection();
    await checkForActiveTask();
});

// ══════════ Resume active task on page load ══════════
async function checkForActiveTask() {
    try {
        const r = await fetch(`${API_BASE_URL}/active-task`);
        const d = await r.json();
        if (d.active && d.task_id) {
            currentTaskId = d.task_id;
            lockFormForActiveTask();
            // Show status panel with current state
            const sc = document.getElementById('statusContainer');
            sc.className = 'status-container status-processing';
            sc.style.display = 'block';
            document.getElementById('statusMessage').textContent = `Resuming task\u2026 ${d.progress_message || ''}`;
            const pct = typeof d.progress_percent === 'number' ? d.progress_percent : 10;
            updateProgress(pct, d.progress_message || 'Resuming\u2026');

            if (d.status === 'completed') {
                // Background worker finished — fetch saved results from DB
                await fetchSavedResults(d.task_id);
            } else {
                // Still running — resume polling
                startMonitoring();
            }
        }
    } catch(e) { console.error('Active-task check error:', e); }
}

function lockFormForActiveTask() {
    const btn = document.getElementById('submitBtn');
    btn.disabled = true;
    btn.textContent = 'Task in progress\u2026';
    // Disable file inputs
    document.getElementById('sourceFiles').disabled = true;
    document.getElementById('supplementalSteps').disabled = true;
}

function unlockForm() {
    const btn = document.getElementById('submitBtn');
    btn.disabled = false;
    btn.textContent = 'Upload & check';
    document.getElementById('sourceFiles').disabled = false;
    document.getElementById('supplementalSteps').disabled = false;
}

async function fetchSavedResults(taskId) {
    try {
        const r = await fetch(`${API_BASE_URL}/task-results/${taskId}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        currentResults = await r.json();
        showResults(currentResults);
    } catch(e) {
        // Fall back to proxied results endpoint
        await fetchResults();
    }
}

// ══════════ API connection ══════════
async function checkApiConnection() {
    const el = document.getElementById('apiStatus');
    el.className = 'api-pill checking'; el.textContent = 'Checking API\u2026';
    try {
        const r = await fetch(`${API_BASE_URL}/check-connection`, { headers: { 'X-CSRFToken': csrfToken } });
        const d = await r.json();
        if (d.connected) { el.className = 'api-pill connected'; el.textContent = '\u2705 API Connected'; }
        else              { el.className = 'api-pill disconnected'; el.textContent = '\u274C Disconnected'; }
    } catch { el.className = 'api-pill disconnected'; el.textContent = '\u274C Error'; }
}

// \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550 LLM connection \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
async function checkLlmConnection() {
    const el = document.getElementById('llmStatus');
    el.className = 'api-pill checking'; el.textContent = 'Checking LLM\u2026';
    try {
        const r = await fetch(`${API_BASE_URL}/check-llm`, { headers: { 'X-CSRFToken': csrfToken } });
        const d = await r.json();
        if (d.connected) { el.className = 'api-pill connected'; el.textContent = '\u2705 LLM Connected'; }
        else              { el.className = 'api-pill disconnected'; el.textContent = '\u274C LLM Offline'; }
    } catch { el.className = 'api-pill disconnected'; el.textContent = '\u274C LLM Error'; }
}

// ══════════ File inputs ══════════
document.getElementById('sourceFiles').addEventListener('change', e => handleFiles(e.target, 'sourceFilesSelected'));

function handleFiles(input, displayId) {
    previewId = null; mappingManifest = null; uploadCheck = null;
    document.getElementById('precheckPanel').style.display = 'none';
    const files = input.files;
    const el = document.getElementById(displayId);
    if (!files.length) { el.style.display = 'none'; return; }

    let total = 0, items = [];
    for (const f of files) { total += f.size; items.push(`<div class="file-item"><span>${escapeHtml(f.name)}</span><span>${fmtSize(f.size)}</span></div>`); }

    let listHtml;
    if (files.length <= 5) { listHtml = `<div class="file-list">${items.join('')}</div>`; }
    else { listHtml = `<div class="file-list">${items.slice(0,3).join('')}<div class="file-item" style="font-style:italic;color:#94a3b8;"><span>\u2026 and ${files.length - 3} more</span></div></div>`; }

    el.innerHTML = `<span class="file-info">\u2705 ${files.length} file${files.length>1?'s':''} (${fmtSize(total)})</span>
        <button type="button" class="remove-file-btn" data-input="${input.id}" data-display="${displayId}">\u2715 Remove</button>${listHtml}`;
    el.style.display = 'flex';

    // Attach remove handler
    el.querySelector('.remove-file-btn').addEventListener('click', function() {
        clearFiles(this.dataset.input, this.dataset.display);
    });
}

function fmtSize(b) {
    if (!b) return '0 B';
    const k = 1024, s = ['B','KB','MB','GB'], i = Math.floor(Math.log(b)/Math.log(k));
    return (b/Math.pow(k,i)).toFixed(1)+' '+s[i];
}

function clearFiles(inputId, displayId) {
    previewId = null; mappingManifest = null;
    document.getElementById(inputId).value = '';
    document.getElementById('precheckPanel').style.display = 'none';
    const el = document.getElementById(displayId);
    el.style.display = 'none'; el.innerHTML = '';
}

// ══════════ Drag and drop ══════════
function setupDragAndDrop(inputId, dropZone, displayId) {
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(evt => {
        dropZone.addEventListener(evt, e => { e.preventDefault(); e.stopPropagation(); }, false);
    });
    ['dragenter', 'dragover'].forEach(evt => {
        dropZone.addEventListener(evt, () => dropZone.classList.add('dragover'), false);
    });
    ['dragleave', 'drop'].forEach(evt => {
        dropZone.addEventListener(evt, () => dropZone.classList.remove('dragover'), false);
    });
    dropZone.addEventListener('drop', e => {
        const files = e.dataTransfer.files;
        if (!files.length) return;
        const input = document.getElementById(inputId);
        input.files = files;
        handleFiles(input, displayId);
    });
}

// Wire up the shared drop zone and click-to-upload
const dropZones = document.querySelectorAll('.file-drop');
dropZones[0].addEventListener('click', () => document.getElementById('sourceFiles').click());
setupDragAndDrop('sourceFiles', dropZones[0], 'sourceFilesSelected');

// Inspect, map, preview, then confirm the exact private preview.
document.getElementById('uploadForm').addEventListener('submit', async e => {
    e.preventDefault();
    await checkUpload();
});

async function checkUpload() {
    const files = [...document.getElementById('sourceFiles').files];
    if (!files.length) { alert('Select at least one source file.'); return; }
    const btn = document.getElementById('submitBtn');
    btn.disabled = true; btn.textContent = 'Checking files…';
    const fd = new FormData();
    files.forEach(f => fd.append('files', f));
    fd.append('supplemental_steps', document.getElementById('supplementalSteps').checked ? 'true' : 'false');
    if (mappingManifest) fd.append('upload_manifest', JSON.stringify(mappingManifest));
    if (previousPreviewId) fd.append('previous_preview', previousPreviewId);
    previewId = null;
    try {
        const r = await fetch(`${API_BASE_URL}/precheck`, {method:'POST',body:fd,headers:{'X-CSRFToken':csrfToken}});
        const result = await r.json();
        if (!r.ok || result.error) throw new Error(result.error || 'File check failed.');
        uploadCheck = result; previewId = result.preview_id || null; previousPreviewId = previewId;
        showPrecheckPanel(result);
    } catch (error) { showError(error.message); }
    finally { btn.disabled=false; btn.textContent='Upload & check'; }
}

function mappingChanged() {
    previewId = null;
    const btn = document.getElementById('startAnalysisBtn');
    btn.disabled = true; btn.textContent = 'Recheck mappings before confirming';
    document.getElementById('recheckMappingsBtn').hidden = false;
}

function showPrecheckPanel(check) {
    const panel = document.getElementById('precheckPanel');
    const container = document.getElementById('precheckFiles');
    container.replaceChildren();
    mappingManifest = check.files.map(f => f.format_id ? {format_id:f.format_id} : {column_mapping:{...f.mapping}});
    check.files.forEach((file, index) => {
        const section = document.createElement('section');
        const heading = document.createElement('h4'); heading.textContent=`${file.name} (${file.rows} rows)`;
        section.append(heading);
        const label = document.createElement('label'); label.textContent='File format'; label.htmlFor=`format-${index}`;
        const select = document.createElement('select'); select.id=label.htmlFor;
        select.add(new Option('Custom mapping', ''));
        check.formats.forEach(f => select.add(new Option(f.name, f.format_id)));
        select.value=file.format_id || '';
        let selectedFormat = select.value;
        section.append(label,select);
        const table = document.createElement('table');
        const columns = [];
        Object.entries(file.mapping).forEach(([header,destination], column) => {
            const row=document.createElement('tr'); const source=document.createElement('th'); source.scope='row'; source.textContent=header;
            const cell=document.createElement('td'); const allocation=document.createElement('select'); allocation.id=`mapping-${index}-${column}`;
            allocation.setAttribute('aria-label',`${header}: destination field`);
            allocation.add(new Option('Excluded', ''));
            Object.entries(check.fields).forEach(([id,field]) => allocation.add(new Option(field.label, id)));
            allocation.value=destination;
            allocation.addEventListener('change', () => {
                mappingManifest[index]={column_mapping:Object.fromEntries(columns.map(([h,a]) => [h,a.value]))};
                select.value=''; selectedFormat=''; mappingChanged();
            });
            columns.push([header,allocation]); cell.append(allocation); row.append(source,cell); table.append(row);
        });
        select.addEventListener('change', () => {
            if (!confirm('Changing format replaces all current column allocations. Continue?')) {select.value=selectedFormat; return;}
            selectedFormat=select.value;
            const format=check.formats.find(f => f.format_id===select.value);
            columns.forEach(([header,allocation]) => {allocation.value=format?.mapping[header] || '';});
            mappingManifest[index]=format ? {format_id:format.format_id} : {column_mapping:Object.fromEntries(columns.map(([h,a]) => [h,a.value]))};
            mappingChanged();
        });
        section.append(table);
        const exclusions=document.createElement('p'); exclusions.textContent='Excluded columns: '+(file.excluded.join(', ') || 'none'); section.append(exclusions);
        container.append(section);
    });
    document.getElementById('precheckStats').textContent = `${check.total} distinct studies: ${check.new_complete} new complete; ${check.new_incomplete} incomplete (will be discarded); ${check.existing_enrichable} existing studies to enrich; ${check.existing_unchanged} unchanged. ${check.invalid_raw_rows} rows with invalid or missing anon_id.`;
    const errors=document.getElementById('precheckErrors'); errors.replaceChildren();
    check.errors.forEach(error => {const p=document.createElement('p');p.textContent=`File ${error.file+1}, row ${error.row}, ${error.field}: ${error.code}`;errors.append(p);});
    (check.excluded_reasons || []).forEach(reason => {const p=document.createElement('p');p.textContent='Incomplete study missing: '+reason.join(', ');errors.append(p);});
    if (check.disagreements.length) {const p=document.createElement('p');p.textContent=`${check.disagreements.length} stored-value disagreements; stored values will be preserved.`;errors.append(p);}
    if (check.protected_enrichment.length) {const p=document.createElement('p');p.textContent='Existing processing inputs cannot be enriched because they affect derived results.';errors.append(p);}
    errors.style.display=errors.childElementCount?'block':'none';
    const start=document.getElementById('startAnalysisBtn'); start.disabled=check.blocked;
    start.textContent=check.blocked?'Correct invalid input before confirming':`Confirm (${check.new_incomplete} incomplete discarded)`;
    document.getElementById('recheckMappingsBtn').hidden=true;
    panel.style.display='block'; panel.scrollIntoView({behavior:'smooth',block:'nearest'});
}

function cancelPrecheck() { previewId=null; document.getElementById('precheckPanel').style.display='none'; }
async function proceedWithAnalysis() {
    if (!previewId) return;
    const start=document.getElementById('startAnalysisBtn'); start.disabled=true;
    const fd=new FormData();fd.append('preview_id',previewId);
    try {
        const r=await fetch(`${API_BASE_URL}/analyze-auto-sort`,{method:'POST',body:fd,headers:{'X-CSRFToken':csrfToken}});
        const result=await r.json();
        if (!r.ok || result.error) throw new Error(result.error || 'Submission failed.');
        document.getElementById('precheckPanel').style.display='none';
        if (!result.task_id) {
            const status=document.getElementById('statusContainer');status.style.display='block';status.className='status-container status-completed';
            document.getElementById('statusMessage').textContent=`Complete. ${result.counts.existing_enrichable} existing studies enriched; ${result.counts.new_incomplete} incomplete discarded. No analysis required.`;
            previewId=null; return;
        }
        currentTaskId=result.task_id;lockFormForActiveTask();startMonitoring();
    } catch(error) {showError(error.message);start.disabled=false;}
}
document.getElementById('startAnalysisBtn').addEventListener('click',proceedWithAnalysis);
document.getElementById('cancelPrecheckBtn').addEventListener('click',cancelPrecheck);
document.getElementById('recheckMappingsBtn').addEventListener('click',checkUpload);
document.getElementById('supplementalSteps').addEventListener('change',mappingChanged);

// ══════════ Status monitoring ══════════
function stopTask() {
    if (!currentTaskId) return;
    if (!confirm('Stop the current task? Results processed so far will not be saved.')) return;
    const btn = document.getElementById('stopTaskBtn');
    btn.disabled = true;
    btn.textContent = 'Stopping\u2026';
    fetch(`${API_BASE_URL}/tasks/${currentTaskId}/cancel`, {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken }
    }).then(r => r.json().then(d => ({ ok: r.ok, d }))).then(({ ok, d }) => {
        if (ok) {
            clearInterval(statusCheckInterval);
            btn.style.display = 'none';
            showError('Task stopped by user.');
        } else {
            btn.disabled = false;
            btn.textContent = '\u25a0 Stop Task';
            alert(d.error || 'Failed to stop task.');
        }
    }).catch(e => {
        btn.disabled = false;
        btn.textContent = '\u25a0 Stop Task';
        alert(`Error: ${e.message}`);
    });
}

function startMonitoring() {
    updateProgress(6, 'Upload complete, starting analysis\u2026');
    const sc = document.getElementById('statusContainer');
    sc.className = 'status-container status-processing';
    document.getElementById('statusMessage').textContent = 'Processing\u2026';
    const stopBtn = document.getElementById('stopTaskBtn');
    stopBtn.style.display = 'block';
    stopBtn.disabled = false;
    stopBtn.textContent = '\u25a0 Stop Task';
    stopBtn.onclick = stopTask;
    checkStatus();
    statusCheckInterval = setInterval(checkStatus, 1000);
}

async function checkStatus() {
    if (!currentTaskId) return;
    try {
        const r = await fetch(`${API_BASE_URL}/status/${currentTaskId}`);
        const s = await r.json();
        let msg = s.progress || 'Processing\u2026';

        if (s.progress_details) {
            const d = s.progress_details;
            if (d.step === 'llm' && d.total > 0) msg = `LLM grading: ${d.current}/${d.total}`;
            else if (d.step === 'lunit' && d.total > 0) msg = `Detailed findings: ${d.current}/${d.total}`;
        }
        document.getElementById('statusMessage').textContent = msg;

        let pct = typeof s.progress_percent === 'number' ? s.progress_percent : guessPct(s.status, s.progress);
        let txt = msg;
        if (s.total_reports) txt += ` (${s.total_reports} total reports)`;
        updateProgress(pct, txt);

        if (s.status === 'completed') { clearInterval(statusCheckInterval); await fetchResults(); }
        else if (s.status === 'failed') { clearInterval(statusCheckInterval); showError(`Processing failed: ${s.error || 'Unknown'}`); }
    } catch(e) { console.error('Status check error:', e); }
}

function guessPct(status, txt) {
    if (status === 'completed') return 100;
    if (status === 'failed') return 0;
    if (status === 'saving') return 95;
    if (status !== 'processing') return 5;
    if (!txt) return 15;
    if (txt.includes('Initializing')) return 8;
    if (txt.includes('Loaded') && txt.includes('reports')) return 12;
    if (txt.includes('LLM grading')) return 30;
    if (txt.includes('Detailed findings')) return 60;
    if (txt.includes('accuracy')) return 85;
    if (txt.includes('time statistics')) return 90;
    if (txt.includes('false negative')) return 95;
    if (txt.includes('Analysis complete')) return 100;
    return 15;
}

async function fetchResults() {
    try {
        const r = await fetch(`${API_BASE_URL}/results/${currentTaskId}`);
        currentResults = await r.json();
        showResults(currentResults);
    } catch(e) { showError(`Fetch results failed: ${e.message}`); }
}

// ══════════ Show results ══════════
function showResults(res) {
    const sc = document.getElementById('statusContainer');
    sc.className = 'status-container status-completed';
    document.getElementById('statusMessage').textContent = '\u2705 Analysis completed successfully!';
    updateProgress(100, 'Complete');

    const newRecs = res.new_records_added || 0;
    const skipped = res.existing_records_skipped || 0;
    const total   = res.total_records_processed || 0;

    let errHtml = '';
    if (res.error) {
        errHtml = `<div style="background:#fee2e2;border:1px solid #fecaca;border-radius:var(--radius);padding:12px 16px;margin-bottom:16px;color:#991b1b;font-size:.9em;">
            <strong>\u26A0\uFE0F Errors:</strong><pre style="margin:6px 0 0;white-space:pre-wrap;font-size:.88em;">${escapeHtml(res.error)}</pre></div>`;
    }

    const rc = document.getElementById('resultsContainer');
    document.getElementById('resultsContent').innerHTML = `
        ${errHtml}
        <div class="db-stats-box">
            <h4 style="margin:0 0 8px;">Database import summary</h4>
            <div class="metric"><span class="metric-label">Total processed</span><span class="metric-value">${total}</span></div>
            <div class="metric"><span class="metric-label">New records added</span><span class="metric-value" style="color:var(--c-success)">${newRecs}</span></div>
            <div class="metric"><span class="metric-label">Skipped (already existed)</span><span class="metric-value" style="color:var(--c-warning)">${skipped}</span></div>
        </div>
        <div class="download-row" style="margin-top:20px;">
            <a href="${window.PAGE_CONFIG.reportUrl}" class="btn btn-primary" style="text-decoration:none;">View reports</a>
        </div>`;
    rc.style.display = 'block';

    // Attach download CSV handler if present
    const dlBtn = document.getElementById('downloadCsvBtn');
    if (dlBtn) dlBtn.addEventListener('click', downloadCSV);

    document.getElementById('stopTaskBtn').style.display = 'none';
    unlockForm();
}

// ══════════ Helpers ══════════
function showError(msg) {
    const sc = document.getElementById('statusContainer');
    sc.className = 'status-container status-error';
    sc.style.display = 'block';
    document.getElementById('statusMessage').textContent = `Error: ${msg}`;
    updateProgress(0, 'Error');
    document.getElementById('stopTaskBtn').style.display = 'none';
    unlockForm();
    if (statusCheckInterval) clearInterval(statusCheckInterval);
}

function updateProgress(pct, txt) {
    const fill = document.getElementById('progressFill');
    fill.style.width = pct + '%';
    fill.style.background = pct < 20 ? '#d97706' : pct < 85 ? 'var(--c-primary)' : 'var(--c-success)';
    document.getElementById('progressText').textContent = `${Math.round(pct)}% \u2014 ${txt}`;
}

function downloadCSV() {
    if (!currentResults?.csv_data) return;
    dl('data:text/csv;charset=utf-8,' + encodeURIComponent(currentResults.csv_data),
       (currentResults.filename || `cxr_${new Date().toISOString().slice(0,19).replace(/:/g,'-')}`) + '.csv');
}
function dl(uri, name) { const a = document.createElement('a'); a.href = uri; a.download = name; a.click(); }
