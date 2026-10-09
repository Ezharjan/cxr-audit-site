const API_BASE_URL = window.PAGE_CONFIG.apiBaseUrl;
let currentTaskId = null;
let statusCheckInterval = null;
let csrfToken = window.PAGE_CONFIG.csrfToken;
let currentResults = null;

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
    document.getElementById('lunitFiles').disabled = true;
    document.getElementById('gtFiles').disabled = true;
    document.getElementById('supplementalSteps').disabled = true;
}

function unlockForm() {
    const btn = document.getElementById('submitBtn');
    btn.disabled = false;
    btn.textContent = 'Upload & check';
    document.getElementById('lunitFiles').disabled = false;
    document.getElementById('gtFiles').disabled = false;
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
document.getElementById('lunitFiles').addEventListener('change', e => handleFiles(e.target, 'lunitFilesSelected'));
document.getElementById('gtFiles').addEventListener('change', e => handleFiles(e.target, 'gtFilesSelected'));

function handleFiles(input, displayId) {
    const files = input.files;
    const el = document.getElementById(displayId);
    if (!files.length) { el.style.display = 'none'; return; }

    let total = 0, items = [];
    for (const f of files) { total += f.size; items.push(`<div class="file-item"><span>${f.name}</span><span>${fmtSize(f.size)}</span></div>`); }

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
    document.getElementById(inputId).value = '';
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

// Wire up both drop zones and click-to-upload
const dropZones = document.querySelectorAll('.file-drop');
dropZones[0].addEventListener('click', () => document.getElementById('lunitFiles').click());
dropZones[1].addEventListener('click', () => document.getElementById('gtFiles').click());
setupDragAndDrop('lunitFiles', dropZones[0], 'lunitFilesSelected');
setupDragAndDrop('gtFiles',    dropZones[1], 'gtFilesSelected');

// ══════════ Form submit (Upload & Check) ══════════
document.getElementById('uploadForm').addEventListener('submit', async e => {
    e.preventDefault();
    const lunit = document.getElementById('lunitFiles').files;
    const gt    = document.getElementById('gtFiles').files;
    if (!lunit.length || !gt.length) { alert('Please select files for both CARPL and RIS.'); return; }

    const btn = document.getElementById('submitBtn');
    btn.disabled = true; btn.textContent = 'Checking database\u2026';

    // Hide any previous panels
    document.getElementById('precheckPanel').style.display = 'none';
    document.getElementById('statusContainer').style.display = 'none';
    document.getElementById('resultsContainer').style.display = 'none';

    try {
        const checkFd = new FormData();
        for (const f of lunit) checkFd.append('files', f);
        for (const f of gt)    checkFd.append('files', f);

        const r = await fetch(`${API_BASE_URL}/precheck`, {
            method: 'POST', body: checkFd,
            headers: { 'X-CSRFToken': csrfToken }
        });
        const check = await r.json();

        if (check.error) {
            showError(`Pre-check error: ${check.error}`);
            btn.disabled = false; btn.textContent = 'Upload & check';
            return;
        }

        showPrecheckPanel(check);
    } catch(e) {
        showError(`Pre-check failed: ${e.message}`);
    }
    btn.disabled = false; btn.textContent = 'Upload & check';
});

function showPrecheckPanel(check) {
    const panel = document.getElementById('precheckPanel');

    // File list
    let filesHtml = '';
    if (check.files && check.files.length) {
        check.files.forEach(f => {
            const badge = f.type === 'carpl'
                ? `<span class="file-badge carpl">CARPL \u00B7 ${f.accessions} accessions</span>`
                : `<span class="file-badge ris">RIS/GE</span>`;
            filesHtml += `<div class="file-row"><span>${f.name} <small style="color:var(--c-text-muted)">(${f.rows} rows)</small></span>${badge}</div>`;
        });
    }
    document.getElementById('precheckFiles').innerHTML = filesHtml;

    // Stats
    const statsEl = document.getElementById('precheckStats');
    statsEl.innerHTML = `
        <h4 style="margin:0 0 8px;">Database check</h4>
        <div class="metric"><span class="metric-label">Unique accession numbers</span><span class="metric-value">${check.total}</span></div>
        <div class="metric"><span class="metric-label">New (will be added)</span><span class="metric-value" style="color:var(--c-success)">${check.new}</span></div>
        <div class="metric"><span class="metric-label">Already in database</span><span class="metric-value" style="color:var(--c-warning)">${check.existing}</span></div>`;

    // Errors
    const errEl = document.getElementById('precheckErrors');
    if (check.errors && check.errors.length) {
        errEl.innerHTML = `<div style="background:#fee2e2;border:1px solid #fecaca;border-radius:var(--radius);padding:10px 14px;color:#991b1b;font-size:.88em;">
            <strong>\u26A0\uFE0F Warnings:</strong><ul style="margin:6px 0 0;padding-left:18px;">${check.errors.map(e => `<li>${e}</li>`).join('')}</ul></div>`;
        errEl.style.display = 'block';
    } else {
        errEl.style.display = 'none';
    }

    // Start Analysis button state
    const startBtn = document.getElementById('startAnalysisBtn');
    if (check.new === 0 && check.total > 0) {
        startBtn.disabled = true;
        startBtn.textContent = '\u26D4 All records already exist';
        startBtn.style.background = '#cbd5e1';
    } else {
        startBtn.disabled = false;
        startBtn.textContent = `Start analysis (${check.new} new records)`;
        startBtn.style.background = '';
    }

    panel.style.display = 'block';
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function cancelPrecheck() {
    document.getElementById('precheckPanel').style.display = 'none';
}

function proceedWithAnalysis() {
    const lunit = document.getElementById('lunitFiles').files;
    const gt    = document.getElementById('gtFiles').files;
    document.getElementById('precheckPanel').style.display = 'none';
    uploadFiles(lunit, gt, document.getElementById('supplementalSteps').checked);
}

// Wire up precheck buttons
document.getElementById('startAnalysisBtn').addEventListener('click', proceedWithAnalysis);
document.getElementById('cancelPrecheckBtn').addEventListener('click', cancelPrecheck);

async function uploadFiles(lunit, gt, supp) {
    const fd = new FormData();
    for (const f of lunit) fd.append('lunit_files', f);
    for (const f of gt)    fd.append('ground_truth_files', f);
    fd.append('supplemental_steps', supp);

    const btn = document.getElementById('submitBtn');
    btn.disabled = true; btn.textContent = 'Uploading\u2026';

    const sc = document.getElementById('statusContainer');
    sc.className = 'status-container status-uploading'; sc.style.display = 'block';
    document.getElementById('statusMessage').textContent = `Uploading ${lunit.length + gt.length} files\u2026${supp ? ' (detailed findings enabled)' : ''}`;
    updateProgress(2, 'Uploading\u2026');

    try {
        const r = await fetch(`${API_BASE_URL}/analyze-multiple`, { method:'POST', body:fd, headers:{'X-CSRFToken':csrfToken} });
        if (r.status === 409) {
            const d = await r.json();
            showError(d.error || 'A task is already running.');
            return;
        }
        if (!r.ok) throw new Error(`HTTP ${r.status} \u2014 ${await r.text()}`);
        const d = await r.json();
        currentTaskId = d.task_id;
        lockFormForActiveTask();
        startMonitoring();
    } catch(e) { showError(`Upload failed: ${e.message}`); }
}

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
            <strong>\u26A0\uFE0F Errors:</strong><pre style="margin:6px 0 0;white-space:pre-wrap;font-size:.88em;">${res.error}</pre></div>`;
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
    document.getElementById('statusMessage').innerHTML = `\u274C ${msg}`;
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
