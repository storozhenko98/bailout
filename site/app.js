document.querySelectorAll('[data-copy]').forEach(button => {
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    const text = document.getElementById(button.dataset.copy).textContent.trim();
    const status = document.getElementById('copy-status');
    try {
      await navigator.clipboard.writeText(text);
      button.classList.add('copied');
      button.setAttribute('aria-label', 'Install command copied');
      const icon = button.querySelector('svg');
      const previous = icon.innerHTML;
      icon.innerHTML = '<path d="m4 12 5 5L20 6"/>';
      status.textContent = 'Install command copied. Paste it in your terminal.';
      setTimeout(() => { button.disabled = false; button.classList.remove('copied'); button.setAttribute('aria-label', 'Copy install command'); icon.innerHTML = previous; }, 2000);
    } catch {
      button.disabled = false;
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(document.getElementById(button.dataset.copy));
      selection.removeAllRanges(); selection.addRange(range);
      status.textContent = 'Select and copy the highlighted install command.';
    }
  });
});
// Optional read-only tool, using the same install command as the visible page.
if (document.modelContext?.registerTool) {
  const lifecycle = new AbortController();
  window.addEventListener('pagehide', () => lifecycle.abort(), { once: true });
  try {
    Promise.resolve(document.modelContext.registerTool({
      name: 'get_install_instructions', title: 'Get bailout installation instructions',
      description: 'Read the installation command and supported platforms. Does not run or copy commands.',
      inputSchema: { type: 'object', properties: {}, additionalProperties: false },
      annotations: { readOnlyHint: true, untrustedContentHint: false },
      execute(input) {
        if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).length) throw new Error('No arguments expected.');
        return { command: document.getElementById('install-code').textContent.trim(), platforms: ['macOS ARM64', 'Linux x64', 'Linux ARM64'], next: 'Run bailout to bootstrap this machine or repair your usual tools. When finished, bailout uninstall removes the binary.' };
      },
    }, { signal: lifecycle.signal })).catch(() => {});
  } catch { /* The visible interface works without WebMCP. */ }
}
