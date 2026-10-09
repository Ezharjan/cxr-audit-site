const copyKey = document.getElementById('copyKey');
if (copyKey) copyKey.addEventListener('click', async () => {
    const input = document.getElementById('newKey');
    const status = document.getElementById('copyStatus');
    try { await navigator.clipboard.writeText(input.value); status.textContent = 'Copied.'; }
    catch { input.focus(); input.select(); status.textContent = 'Select and copy the key.'; }
});
