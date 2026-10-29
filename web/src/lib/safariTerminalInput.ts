import type { Terminal } from '@xterm/xterm';

/** Safari text services can deliver input before a trailing Process key (229),
 * including ordinary Latin typing. Give native text edits one owner: input,
 * never both xterm's keyboard path and its delayed textarea fallback.
 * Control keys still use xterm. No payload/time-window based input deduplication.
 */
export function bindSafariTextInput(term: Terminal, host: HTMLElement) {
  const textarea = term.textarea;
  if (!textarea || !/^((?!chrome|chromium|android).)*safari/i.test(navigator.userAgent)) return () => {};
  // WebKit turns trailing ASCII spaces into NBSP under xterm's nowrap style.
  // pre still prevents wrapping, while retaining the exact native text.
  const whiteSpace = textarea.style.whiteSpace;
  textarea.style.whiteSpace = 'pre';
  let before: string | null = null;
  let last = textarea.value;
  let composition: { prefix: string; suffix: string; selected: string; original: string } | null = null;
  let committed: string | null = null;
  const view = term.element?.querySelector<HTMLElement>('.composition-view');
  const send = (text: string) => { if (text) term.input(text, true); };
  const consume = () => {
    // Never retain an already-sent line for Safari's text services to replay.
    textarea.value = '';
    last = '';
    before = null;
  };
  const preedit = (text: string) => {
    if (!view) return;
    view.textContent = text;
    view.classList.toggle('active', !!text);
    view.style.left = textarea.style.left;
    view.style.top = textarea.style.top;
    view.style.fontFamily = String(term.options.fontFamily);
    view.style.fontSize = `${term.options.fontSize}px`;
  };
  const key = (event: KeyboardEvent) => {
    if (event.target !== textarea) return;
    if (event.type === 'keydown' && !composition && !event.isComposing && event.keyCode !== 229) committed = null;
    if (composition || event.isComposing || event.keyCode === 229 || event.key === 'Dead' || event.key === 'AltGraph'
      || ([...event.key].length === 1 && !event.ctrlKey && !event.metaKey)
      || (event.type === 'keypress' && event.charCode >= 32 && !event.ctrlKey && !event.metaKey)) {
      // Do not preventDefault: the browser must still produce the native edit.
      event.stopImmediatePropagation();
      return;
    }
    if (event.type === 'keydown' && !event.ctrlKey && !event.altKey && !event.metaKey
      && (event.key === 'Backspace' || event.key === 'Delete')) {
      event.preventDefault();
      event.stopImmediatePropagation();
      send(event.key === 'Backspace' ? '\x7f' : '\x1b[3~');
      const start = textarea.selectionStart, end = textarea.selectionEnd;
      const previous = [...textarea.value.slice(0, start)].at(-1)?.length ?? 0;
      const next = [...textarea.value.slice(end)][0]?.length ?? 0;
      textarea.setRangeText('', start === end && event.key === 'Backspace' ? start - previous : start,
        start === end && event.key === 'Delete' ? end + next : end, 'end');
      last = textarea.value;
      before = null;
      committed = null;
    }
  };
  const start = (event: CompositionEvent) => {
    if (event.target !== textarea) return;
    event.stopImmediatePropagation();
    composition = { prefix: textarea.value.slice(0, textarea.selectionStart), suffix: textarea.value.slice(textarea.selectionEnd),
      selected: textarea.value.slice(textarea.selectionStart, textarea.selectionEnd), original: textarea.value };
    committed = null;
    before = null;
  };
  const update = (event: CompositionEvent) => {
    if (event.target !== textarea) return;
    event.stopImmediatePropagation();
    preedit(event.data);
  };
  const end = (event: CompositionEvent) => {
    if (event.target !== textarea) return;
    event.stopImmediatePropagation();
    if (!composition) return;  // An unpaired end is not permission to resend the textarea.
    if (event.data) send('\x7f'.repeat([...composition.selected].length) + event.data);
    committed = event.data ? composition.prefix + event.data + composition.suffix : composition.original;
    composition = null;
    consume();
    preedit('');
  };
  const beforeInput = (event: InputEvent) => {
    if (event.target === textarea) before = textarea.value;
  };
  const input = (event: InputEvent) => {
    if (event.target !== textarea) return;
    event.stopImmediatePropagation();
    if (composition || event.isComposing) { preedit(event.data ?? ''); return; }
    const value = textarea.value;
    // Some WebKit versions follow compositionend with another native input for
    // the same edit. The DOM value identifies that transaction. A repeated key
    // changes the value and is therefore always sent, even with identical data.
    if (committed !== null && value === committed) {
      committed = null;
      consume();
      return;
    }
    committed = null;
    const old = [...(before ?? last)], current = [...value];
    let prefix = 0;
    while (prefix < old.length && prefix < current.length && old[prefix] === current[prefix]) prefix++;
    // Native replacement edits (including Latin text services) replace only
    // their changed tail, not the already delivered prefix. Work in code points.
    send('\x7f'.repeat(old.length - prefix) + current.slice(prefix).join(''));
    consume();
  };
  const paste = (event: ClipboardEvent) => {
    if (event.target !== textarea || !event.clipboardData) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    term.paste(event.clipboardData.getData('text/plain'));
    consume();
    committed = null;
  };
  const focus = (event: FocusEvent) => {
    if (event.target !== textarea) return;
    // xterm clears its textarea on blur; never apply a pre-blur baseline to a
    // later text-service edit that arrives without beforeinput.
    last = textarea.value;
    before = null;
    committed = null;
    composition = null;
    preedit('');
  };
  host.addEventListener('focusin', focus, true);
  host.addEventListener('keydown', key, true);
  host.addEventListener('keypress', key, true);
  host.addEventListener('compositionstart', start, true);
  host.addEventListener('compositionupdate', update, true);
  host.addEventListener('compositionend', end, true);
  host.addEventListener('beforeinput', beforeInput, true);
  host.addEventListener('input', input, true);
  host.addEventListener('paste', paste, true);
  return () => {
    host.removeEventListener('focusin', focus, true);
    host.removeEventListener('keydown', key, true);
    host.removeEventListener('keypress', key, true);
    host.removeEventListener('compositionstart', start, true);
    host.removeEventListener('compositionupdate', update, true);
    host.removeEventListener('compositionend', end, true);
    host.removeEventListener('beforeinput', beforeInput, true);
    host.removeEventListener('input', input, true);
    host.removeEventListener('paste', paste, true);
    preedit('');
    textarea.style.whiteSpace = whiteSpace;
  };
}
