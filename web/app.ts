interface ModelContextTool {
  name: string; title: string; description: string;
  inputSchema: { type: string; properties: Record<string, never>; additionalProperties: boolean };
  annotations: { readOnlyHint: boolean; untrustedContentHint: boolean };
  execute(input: unknown): { command: string; platforms: string[]; next: string };
}
interface Document { modelContext?: { registerTool(tool: ModelContextTool, options: { signal: AbortSignal }): unknown } }
document.querySelectorAll<HTMLButtonElement>('[data-copy]').forEach(button => {
  button.addEventListener('click', async () => {
    const target = document.getElementById(button.dataset.copy || '');
    const status = document.getElementById('copy-status');
    const icon = button.querySelector('svg');
    if (button.disabled || !target || !status || !icon) return;
    button.disabled = true;
    const text = (target.textContent || '').trim();
    try {
      await navigator.clipboard.writeText(text);
      button.classList.add('copied');
      button.setAttribute('aria-label', 'Install command copied');
      const previous = icon.innerHTML;
      icon.innerHTML = '<path d="m4 12 5 5L20 6"/>';
      status.textContent = 'Install command copied. Paste it in your terminal.';
      setTimeout(() => { button.disabled = false; button.classList.remove('copied'); button.setAttribute('aria-label', 'Copy install command'); icon.innerHTML = previous; }, 2000);
    } catch {
      button.disabled = false;
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(target);
      selection?.removeAllRanges(); selection?.addRange(range);
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
      execute(input: unknown) {
        if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).length) throw new Error('No arguments expected.');
        return { command: (document.getElementById('install-code')?.textContent || '').trim(), platforms: ['macOS ARM64', 'Linux x64', 'Linux ARM64'], next: 'Run bailout to bootstrap this machine or repair your usual tools. When finished, bailout uninstall removes the binary.' };
      },
    }, { signal: lifecycle.signal })).catch(() => {});
  } catch { /* The visible interface works without WebMCP. */ }
}
