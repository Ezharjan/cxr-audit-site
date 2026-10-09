const copyKey = document.getElementById('copyKey');
if (copyKey) copyKey.addEventListener('click', async () => {
    const input = document.getElementById('newKey');
    const status = document.getElementById('copyStatus');
    try { await navigator.clipboard.writeText(input.value); status.textContent = 'Copied.'; }
    catch { input.focus(); input.select(); status.textContent = 'Select and copy the key.'; }
});

const expiryMode = document.getElementById('expiryMode');
const expiryDate = document.getElementById('keyExpiry');
function updateExpiry() {
    const never = expiryMode.value === 'never';
    expiryDate.disabled = never;
    expiryDate.required = !never;
    expiryDate.hidden = never;
    document.getElementById('keyExpiryLabel').hidden = never;
}
expiryMode.addEventListener('change', updateExpiry);
updateExpiry();
