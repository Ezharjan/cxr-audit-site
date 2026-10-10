const fields = JSON.parse(document.getElementById('fieldCatalogue').textContent);
const formats = JSON.parse(document.getElementById('formatCatalogue').textContent);
const fieldSelect = document.getElementById('fieldIdentifier');
Object.entries(fields).forEach(([id, field]) => fieldSelect.add(new Option(field.label + ' (' + id + ')', id)));
function fieldChanged() {
    const field = {...fields[fieldSelect.value]};
    document.getElementById('fieldStorage').textContent = `Storage: ${fieldSelect.value} / ${field.storage_type}. Upload allowed: ${field.upload_allowed}.`;
    document.getElementById('fieldRevision').value = field.revision;
    delete field.storage_type; delete field.upload_allowed; delete field.revision;
    document.getElementById('fieldSettings').value = JSON.stringify(field, null, 2);
}
fieldSelect.addEventListener('change', fieldChanged); fieldChanged();
const formatSelect = document.getElementById('formatChoice');
formats.forEach(f => formatSelect.add(new Option(f.name, f.format_id)));
function formatChanged() {
    const saved = formats.find(f => f.format_id === formatSelect.value);
    document.getElementById('formatIdentifier').value = saved?.format_id || '';
    document.getElementById('formatRevision').value = saved?.revision || 0;
    const settings = saved ? Object.fromEntries(['name','headers','mapping','rules','role','position','enabled'].map(k => [k,saved[k]])) : {name:'',headers:['anon_id'],mapping:{anon_id:'accession_no'},rules:{},role:'source',position:0,enabled:true};
    document.getElementById('formatSettings').value = JSON.stringify(settings,null,2);
}
formatSelect.addEventListener('change', formatChanged); formatChanged();
async function save(form) {
    const response = await fetch(location.href, {method:'POST',body:new FormData(form)});
    const result = await response.json();
    document.getElementById('configurationMessage').textContent = result.error || 'Settings saved.';
    if (response.ok) location.reload();
}
['seedForm','fieldForm','formatForm'].forEach(id => document.getElementById(id).addEventListener('submit', e => {e.preventDefault(); save(e.target).catch(() => {document.getElementById('configurationMessage').textContent='Save failed.';});}));
document.getElementById('profileField').addEventListener('click',async () => {
    const body = new FormData(document.getElementById('fieldForm')); body.set('action','profile');
    try {const r=await fetch(location.href,{method:'POST',body}); const d=await r.json(); document.getElementById('profileResult').textContent=JSON.stringify(d,null,2);} catch {document.getElementById('profileResult').textContent='Profile unavailable.';}
});
