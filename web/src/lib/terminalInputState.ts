/** Conservative printable-input hint, not a reconstruction of the remote prompt.
 * Escape sequences (including terminal replies and mouse reports) are not drafts.
 * Only an explicit line/interrupt/clear boundary clears the hint; never send one.
 */
export class TerminalInputState {
  partial = false;
  private escape: 'none' | 'esc' | 'csi' | 'osc' | 'osc-end' | 'ss3' = 'none';
  private csi = '';
  private mouse = 0;
  push(data: string) {
    for (const char of data) {
      if (this.mouse) { this.mouse--; continue; }
      if (this.escape === 'osc' || this.escape === 'osc-end') {
        if (char === '\x07' || this.escape === 'osc-end' && char === '\\') this.escape = 'none';
        else this.escape = char === '\x1b' ? 'osc-end' : 'osc';
        continue;
      }
      if (this.escape === 'csi') {
        if (char >= '@' && char <= '~') { if (char === 'M' && !this.csi) this.mouse = 3; this.escape = 'none'; }
        else this.csi = 'parameter'; // no unbounded buffer of escape bytes
        continue;
      }
      if (this.escape === 'esc') {
        this.escape = char === '[' ? 'csi' : char === ']' ? 'osc' : char === 'O' ? 'ss3' : 'none'; this.csi = ''; continue;
      }
      if (this.escape === 'ss3') { this.escape = 'none'; continue; }
      if (char === '\x1b') { this.escape = 'esc'; continue; }
      if (['\r', '\n', '\x03', '\x15'].includes(char)) this.partial = false;
      else if (!/\p{C}/u.test(char)) this.partial = true;
    }
  }
}
