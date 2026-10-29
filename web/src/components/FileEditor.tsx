import { useEffect, useRef } from "react";
import { basicSetup } from "codemirror";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { HighlightStyle, syntaxHighlighting, LanguageDescription } from "@codemirror/language";
import { tags } from "@lezer/highlight";
import { languages } from "@codemirror/language-data";
import { lazyImport } from "../lib/lazyImport";

/** CodeMirror and language grammars ship with Rynhult; language chunks load from this origin. */
export default function FileEditor({ path, value, readOnly, onChange }: {
  path: string; value: string; readOnly?: boolean; onChange: (value: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const change = useRef(onChange);
  change.current = onChange;
  const initial = useRef(value);
  initial.current = value;
  useEffect(() => {
    let view: EditorView | undefined;
    let cancelled = false;
    async function mount() {
      const language = LanguageDescription.matchFilename(languages, path);
      const support = language ? await lazyImport(() => language.load()) : [];
      if (cancelled || !host.current) return;
      view = new EditorView({ parent: host.current, state: EditorState.create({ doc: initial.current, extensions: [
        basicSetup, support, EditorView.lineWrapping,
        syntaxHighlighting(HighlightStyle.define([
          { tag: tags.keyword, color: "var(--accent)" },
          { tag: tags.string, color: "var(--ok)" },
          { tag: [tags.variableName, tags.propertyName], color: "var(--text)" },
          { tag: [tags.number, tags.bool, tags.null], color: "var(--info)" },
          { tag: tags.comment, color: "var(--muted)" },
          { tag: tags.typeName, color: "var(--wait)" },
          { tag: tags.heading, color: "var(--text)", fontWeight: "bold" },
          { tag: tags.link, color: "var(--accent)", textDecoration: "underline" },
          { tag: tags.invalid, color: "var(--bad)" },
        ])),
        EditorState.readOnly.of(!!readOnly), EditorView.editable.of(!readOnly),
        EditorView.contentAttributes.of({ "aria-label": "File content", role: "textbox", "aria-multiline": "true" }),
        EditorView.updateListener.of((u) => { if (u.docChanged) change.current(u.state.doc.toString()); }),
        EditorView.theme({
          "&": { color: "var(--text)", backgroundColor: "var(--panel)", fontSize: "13px" },
          ".cm-content": { fontFamily: "var(--font-mono)", minHeight: "24rem", caretColor: "var(--accent)" },
          ".cm-scroller": { overflow: "auto" },
          ".cm-gutters": { backgroundColor: "var(--panel-2)", color: "var(--muted)", borderColor: "var(--border)" },
          ".cm-activeLine, .cm-activeLineGutter": { backgroundColor: "var(--panel-2)" },
          ".cm-cursor": { borderLeftColor: "var(--accent)" },
          "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": { backgroundColor: "color-mix(in srgb, var(--accent) 25%, transparent) !important" },
        }),
      ] }) });
    }
    void mount();
    return () => { cancelled = true; view?.destroy(); };
  }, [path, readOnly]);
  return <div className="min-w-0 overflow-hidden rounded-md border border-line-input" ref={host} />;
}
