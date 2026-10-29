/** A Mermaid diagram rendered to SVG in the browser (orchestrator v2, slice 6: the plan's roadmap).
 *
 *  The source comes from Rynhult (planning.mermaid), built from agent-written titles that the server already strips
 *  of Mermaid syntax. In the browser it is rendered with securityLevel "strict" (Mermaid encodes label HTML and
 *  disables click handlers) and without HTML labels (no foreignObject), with the style and label options locked
 *  against a diagram's own init directive (`secure`), then the SVG is sanitized again with
 *  DOMPurify's SVG profile before it reaches the page. Mermaid loads on first use, so other pages don't pay for it.
 *  If rendering fails, the source is shown as text. */
import { useEffect, useId, useRef, useState } from 'react';

let ready: Promise<typeof import('mermaid').default> | null = null;
let theme: 'dark' | 'default' | null = null;

function load(dark: boolean) {
  const wanted = dark ? 'dark' : 'default';
  if (!ready || theme !== wanted) {
    theme = wanted;
    ready = import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false, securityLevel: 'strict', theme: wanted, htmlLabels: false,
        flowchart: { htmlLabels: false, useMaxWidth: true }, fontFamily: 'inherit',
        // A diagram's own %%{init}%% directive can't change these: strict mode alone still lets it add CSS
        // (themeCSS, font and theme variables) or turn HTML labels back on. Mermaid's defaults come first.
        secure: ['secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'suppressErrorRendering', 'maxEdges',
          'theme', 'themeCSS', 'themeVariables', 'fontFamily', 'altFontFamily', 'htmlLabels', 'flowchart', 'darkMode', 'look'],
      });
      return mermaid;
    });
  }
  return ready;
}

export async function renderSvg(id: string, source: string, dark: boolean): Promise<string> {
  const [mermaid, { default: DOMPurify }] = await Promise.all([load(dark), import('dompurify')]);
  const { svg } = await mermaid.render(id, source);
  return DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: ['foreignObject', 'script', 'a', 'image', 'use'],
    FORBID_ATTR: ['href', 'xlink:href'],
  });
}

function prefersDark() {  // the app follows the system theme
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
}

export function MermaidDiagram({ source, label }: { source: string; label: string }) {
  const id = `mermaid-${useId().replace(/[^a-zA-Z0-9-]/g, '')}`;
  const ref = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    renderSvg(id, source, prefersDark()).then((svg) => {
      if (live && ref.current) { ref.current.innerHTML = svg; setFailed(false); }
    }).catch(() => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [id, source]);
  if (failed) return <pre className="overflow-x-auto rounded-md bg-panel-2 p-3 text-xs">{source}</pre>;
  // The SVG is a picture of the roadmap list next to it, which carries the same information for screen readers.
  return <div ref={ref} role="img" aria-label={label} className="overflow-x-auto rounded-md bg-panel-2 p-3 [&_svg]:mx-auto [&_svg]:h-auto [&_svg]:max-w-full" />;
}
