import { cleanChatText, safeChatLink } from './chat.ts';
export type Span = { text: string; kind: 'text' | 'strong' | 'em' | 'code' | 'link'; url?: string };
export type Block = { kind: 'text'; spans: Span[] } | { kind: 'code'; text: string };
/** A deliberately small, non-recursive Markdown subset. No HTML/GFM/AST parser.
 * Each successful delimiter scan consumes its entire scanned span; an unmatched
 * delimiter keeps the suffix once or selects whole-message plaintext. No rescanning of
 * overlapping suffixes. Work <= constant * 4096 UTF-16 units; output <=128 spans.
 * Larger, deep or complex inputs are a single escaped plaintext node, not parsed.
 */
export function parseChatMarkdown(input: string): { plain: boolean; text: string; blocks: Block[] } {
  const text = cleanChatText(input.slice(0, 131072));
  const plain = () => ({ plain: true, text, blocks: [] });
  if (text.length > 4096) return plain();
  const lines = text.split('\n');
  if (lines.length > 128) return plain();
  let budget = 128;
  const blocks: Block[] = [];
  for (let line = 0; line < lines.length; line++) {
    let quote = 0;
    while (lines[line].slice(quote * 2, quote * 2 + 2) === '> ') { if (++quote > 8) return plain(); }
    // Only a bare fence closes. Words beside a fence are content, never dropped.
    if (/^`{3,}[\w+#.-]*\s*$/.test(lines[line])) {
      const code: string[] = [];
      while (++line < lines.length && !/^`{3,}\s*$/.test(lines[line])) code.push(lines[line]);
      blocks.push({ kind: 'code', text: code.join('\n') });
      if (--budget < 0) return plain();
      continue;
    }
    const source = lines[line], spans: Span[] = [];
    let i = 0, start = 0;
    const add = (span: Span) => { spans.push(span); budget--; };
    while (i < source.length) {
      if (budget < 0) return plain();
      const image = source[i] === '!' && source[i + 1] === '[';
      if ((image || source[i] === '[') && i > 0 && /[\p{L}\p{N}_]/u.test(source[i - 1])) i += image ? 2 : 1; // arr[0](x), vec![a](b)
      else if (image || source[i] === '[') {
        const offset = image ? 2 : 1, middle = source.indexOf('](', i + offset);
        if (middle < 0) break;
        const end = source.indexOf(')', middle + 2);
        if (end < 0) break;
        const label = source.slice(i + offset, middle), url = safeChatLink(source.slice(middle + 2, end));
        // A refused target is ordinary text: keep it exactly and skip it once.
        if (!url) { i = end + 1; continue; }
        if (i > start) add({ kind: 'text', text: source.slice(start, i) });
        add(image ? { kind: 'text', text: `[Image omitted: ${label}]` } : { kind: 'link', text: label, url });
        i = end + 1; start = i;
      } else if (source[i] === '*' || source[i] === '`') {
        const marker = source[i];
        let count = 1;
        while (source[i + count] === marker) count++;
        // Ambiguous emphasis stays literal, including shell globs/arithmetic.
        // Fall back once rather than rescanning rejected candidate suffixes.
        if (marker === '*' && (count > 2 || (i > 0 && !/\s/.test(source[i - 1])) ||
          !source[i + count] || /\s/.test(source[i + count]))) return plain();
        const delimiter = marker.repeat(count);
        const end = source.indexOf(delimiter, i + count);
        if (end <= i + count || source[end + count] === marker || source[end - 1] === marker) return plain();
        if (marker === '*' && (/\s/.test(source[end - 1]) ||
          (end + count < source.length && /[\p{L}\p{N}_]/u.test(source[end + count])))) return plain();
        if (i > start) add({ kind: 'text', text: source.slice(start, i) });
        add({ kind: marker === '`' ? 'code' : count === 2 ? 'strong' : 'em', text: source.slice(i + delimiter.length, end) });
        i = end + delimiter.length; start = i;
      } else i++;
    }
    if (start < source.length) add({ kind: 'text', text: source.slice(start) });
    if (--budget < 0) return plain();
    blocks.push({ kind: 'text', spans });
  }
  return { plain: false, text, blocks };
}
