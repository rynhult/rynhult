/** Git's C-style quoting of paths: names with a tab, newline, quote or backslash come as "a/tab\tname.txt" (octal
 * escapes are bytes of UTF-8 without core.quotepath=off). */
const ESCAPES: Record<string, number> = { a: 7, b: 8, t: 9, n: 10, v: 11, f: 12, r: 13, '"': 34, "\\": 92 };

/** One quoted path (with its quotes) as the real name. */
export function unquoteGitPath(quoted: string): string {
  const bytes: number[] = [];
  const encoder = new TextEncoder();
  for (let i = 1; i < quoted.length - 1; i++) {
    const c = quoted[i];
    if (c !== "\\") { bytes.push(...encoder.encode(c)); continue; }
    const next = quoted[++i];
    if (/[0-7]/.test(next)) {
      bytes.push(parseInt(quoted.slice(i, i + 3), 8));
      i += 2;
    } else bytes.push(ESCAPES[next] ?? next.charCodeAt(0));
  }
  return new TextDecoder().decode(new Uint8Array(bytes));
}

/** Where a quoted token starting at `start` ends (the index after its closing quote). */
function quotedEnd(s: string, start: number): number {
  for (let i = start + 1; i < s.length; i++) {
    if (s[i] === "\\") i++;
    else if (s[i] === '"') return i + 1;
  }
  return s.length;
}

/** The new-side path of a `diff --git a/x b/x` header line, quoted or not. */
export function diffHeaderPath(line: string): string {
  const rest = line.replace(/^diff --git /, "");
  if (rest.endsWith('"')) {
    const start = rest.startsWith('"') ? quotedEnd(rest, 0) + 1 : rest.lastIndexOf(' "b/') + 1;
    return unquoteGitPath(rest.slice(start)).replace(/^b\//, "");
  }
  const match = / b\/(.*)$/.exec(rest);
  return match ? match[1] : rest;
}
