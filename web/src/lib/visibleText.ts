/** Keep in sync with server approval_cards.visible_text. Newlines/tabs stay readable. */
export function visibleText(text: string): string {
  return text.replace(/[\p{Cf}\u0000-\u0008\u000b-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]/gu,
    ch => `⟨U+${ch.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')}⟩`);
}
