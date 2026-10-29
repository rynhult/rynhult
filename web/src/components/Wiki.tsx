import { markdownComponents } from "./Markdown";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { BookOpen, ChevronRight, FileText, GitCommitHorizontal, ListTree } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Link, useLocation, useNavigate } from "react-router-dom";
import remarkGfm from "remark-gfm";
import { api } from "../lib/api";
import { ago } from "../lib/format";
import { Button, Card, Dialog, Empty, ErrorNote, SearchInput, Skeleton } from "./ui";

/* Wikis follow Azure DevOps "publish code as wiki" (docs/wiki in the repo): Name.md pages, a peer folder Name/ for
   subpages, .order files for sequence, /.attachments for images, absolute links like /User-Guide/Sessions. */

interface WikiNode { name: string; title: string; path: string | null; children: WikiNode[] }
interface WikiTree { exists: boolean; pages: WikiNode[] }
interface WikiPage { path: string; content: string; commit: string | null; committed_at: string | null; author: string | null }

const firstPage = (nodes: WikiNode[]): WikiNode | undefined => {
  for (const n of nodes) { if (n.path) return n; const inner = firstPage(n.children); if (inner) return inner; }
  return undefined;
};
const toRoute = (path: string) => path.replace(/\.md$/, "");
const toFile = (route: string) => `${route}.md`;

/** decodeURIComponent that keeps malformed escapes (e.g. "%E0%A4%A") as they are instead of throwing. */
const decode = (value: string) => { try { return decodeURIComponent(value); } catch { return value; } };

/** Resolve a link or image reference from the page at `pagePath` to a path inside the wiki root. */
function resolve(target: string, pagePath: string): string {
  const [clean] = target.split(/[?#]/);
  if (clean.startsWith("/")) return decode(clean.slice(1));
  const parts = pagePath.split("/").slice(0, -1);
  for (const part of decode(clean).split("/")) {
    if (part === "..") parts.pop(); else if (part && part !== ".") parts.push(part);
  }
  return parts.join("/");
}

function Tree({ nodes, current, base, filter, depth = 0 }: { nodes: WikiNode[]; current: string; base: string; filter: string; depth?: number }) {
  const [closed, setClosed] = useState<Record<string, boolean>>({});
  const matches = (n: WikiNode): boolean => !filter || n.title.toLowerCase().includes(filter) || n.children.some(matches);
  return (
    <ul className={clsx(depth > 0 && "ml-3 border-l border-line pl-1.5")}>
      {nodes.filter(matches).map((n) => {
        const route = n.path ? toRoute(n.path) : null;
        const active = route === current;
        const open = !!filter || !closed[n.name] || current.startsWith(`${route ?? n.name}/`);
        return (
          <li key={n.name}>
            <div className={clsx("flex items-center rounded-md text-[13px]", active ? "bg-panel-3 text-fg" : "text-muted hover:bg-panel-2 hover:text-fg")}>
              {n.children.length > 0
                ? <button onClick={() => setClosed({ ...closed, [n.name]: open })} aria-label={open ? `Collapse ${n.title}` : `Expand ${n.title}`}
                    className="grid size-6 shrink-0 place-items-center text-faint hover:text-fg">
                    <ChevronRight size={13} className={clsx("transition-transform", open && "rotate-90")} aria-hidden="true" />
                  </button>
                : <span className="w-6 shrink-0" />}
              {route
                ? <Link to={`${base}/${route}`} aria-current={active ? "page" : undefined} className={clsx("flex-1 truncate py-1 pr-2", active && "font-medium")}>{n.title}</Link>
                : <span className="flex-1 truncate py-1 pr-2">{n.title}</span>}
            </div>
            {n.children.length > 0 && open && <Tree nodes={n.children} current={current} base={base} filter={filter} depth={depth + 1} />}
          </li>
        );
      })}
    </ul>
  );
}

type MdNode = { type: string; value?: string; children?: MdNode[]; data?: { hName?: string; hProperties?: { id?: string } } };
/** `<kbd>C</kbd>` in a page renders as a key cap (the wiki is also read on Azure DevOps, which renders it), without
 *  allowing any other raw HTML (UX review GU-2). */
export function remarkKbd() {
  const walk = (node: MdNode) => {
    const kids = node.children;
    if (!kids) return;
    for (let i = 0; i + 2 < kids.length; i++) {
      const [open, text, close] = [kids[i], kids[i + 1], kids[i + 2]];
      if (open.type === "html" && open.value === "<kbd>" && text.type === "text" && close.type === "html" && close.value === "</kbd>")
        kids.splice(i, 3, { type: "inlineCode", value: text.value ?? "", data: { hName: "kbd" } });
    }
    kids.forEach(walk);
  };
  return walk;
}

/** GitHub-style section IDs let Guide links reach the prerequisite section after loading. */
export function remarkHeadings() {
  const text = (node: MdNode): string => node.value ?? (node.children ?? []).map(text).join("");
  return (root: MdNode) => {
    const seen = new Map<string, number>();
    const walk = (node: MdNode) => {
      if (node.type === "heading") {
        const slug = text(node).toLowerCase().replace(/[^\p{L}\p{N}_ -]/gu, "").replace(/ /g, "-");
        const count = seen.get(slug) ?? 0;
        seen.set(slug, count + 1);
        node.data = { ...node.data, hProperties: { id: count ? `${slug}-${count}` : slug } };
      }
      node.children?.forEach(walk);
    };
    walk(root);
  };
}

export function Wiki({ kind, scopeKey, base, page: route, empty }: {
  kind: "project" | "workspace" | "guide"; scopeKey: string; base: string; page: string; empty?: ReactNode;
}) {
  const navigate = useNavigate();
  const { hash } = useLocation();
  const [filter, setFilter] = useState("");
  const [contents, setContents] = useState(false);
  const apiBase = `/api/wiki/${kind}/${encodeURIComponent(scopeKey)}`;
  const tree = useQuery({ queryKey: ["wiki", kind, scopeKey], queryFn: () => api<WikiTree>(apiBase), refetchInterval: 30000 });
  const home = useMemo(() => (tree.data ? firstPage(tree.data.pages) : undefined), [tree.data]);
  const current = route || (home?.path ? toRoute(home.path) : "");
  const page = useQuery({
    queryKey: ["wiki-page", kind, scopeKey, current],
    queryFn: () => api<WikiPage>(`${apiBase}/page?path=${encodeURIComponent(toFile(current))}`),
    enabled: !!current, retry: false,
  });

  useEffect(() => {
    // The article is not mounted until the independent tree request finishes.
    if (!tree.isLoading && hash && page.data?.path === toFile(current)) {
      document.getElementById(decode(hash.slice(1)))?.scrollIntoView({ block: "start" });
    }
  }, [hash, current, page.data, tree.isLoading]);
  useEffect(() => setContents(false), [current]);  // a page picked from Contents closes it
  if (tree.isLoading) return <Card className="p-6"><Skeleton className="h-64" /></Card>;
  if (tree.error) return <ErrorNote error={tree.error} />;
  if (!tree.data?.exists || !tree.data.pages.length) {
    return <Card><Empty icon={BookOpen}>{empty ?? <>No wiki yet. Agents build it in <span className="font-mono text-fg">docs/wiki/</span> as they work (acc-workflow skill), in the Azure DevOps wiki layout.</>}</Empty></Card>;
  }
  const crumbs = current.split("/");
  const pages = (
    <>
      <SearchInput value={filter} onChange={setFilter} placeholder="Filter pages" className="mb-2 w-full min-w-0" />
      <nav aria-label="Wiki pages" className="max-h-[70vh] overflow-y-auto">
        <Tree nodes={tree.data.pages} current={current} base={base} filter={filter.trim().toLowerCase()} />
      </nav>
    </>
  );
  return (
    <div className="grid min-w-0 grid-cols-1 gap-6 lg:grid-cols-[16rem_minmax(0,1fr)]">
      {/* Below lg the article comes first and the page tree opens from Contents (UX review GU-1). */}
      <Card className="h-fit p-2 max-lg:hidden lg:sticky lg:top-4">{!contents && pages}</Card>
      <Dialog sheet open={contents} onClose={() => setContents(false)} title="Contents">
        <div className="p-2">{pages}</div>
      </Dialog>
      <Card className="min-w-0">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-3 text-xs text-muted sm:px-6">
          <Button size="sm" className="lg:hidden" aria-haspopup="dialog" aria-expanded={contents} onClick={() => setContents(true)}>
            <ListTree size={14} aria-hidden="true" /> Contents</Button>
          <nav aria-label="Page path" className="flex min-w-0 flex-wrap items-center gap-1">
            {crumbs.map((c, i) => (
              <span key={i} className="flex items-center gap-1">
                {i > 0 && <ChevronRight size={12} className="text-faint" aria-hidden="true" />}
                {i < crumbs.length - 1
                  ? <Link to={`${base}/${crumbs.slice(0, i + 1).join("/")}`} className="hover:text-fg">{c.replace(/-/g, " ")}</Link>
                  : <span className="text-fg">{c.replace(/-/g, " ")}</span>}
              </span>
            ))}
          </nav>
          {page.data?.commit
            ? <span className="inline-flex items-center gap-1.5"><GitCommitHorizontal size={13} aria-hidden="true" />{page.data.author} · {ago(page.data.committed_at)} · <span className="font-mono">{page.data.commit}</span></span>
            : page.data && <span className="inline-flex items-center gap-1.5"><FileText size={13} aria-hidden="true" />not committed yet</span>}
        </div>
        {page.error ? <div className="p-6"><ErrorNote error={page.error} /></div> : !page.data ? <div className="p-6"><Skeleton className="h-48" /></div> : (
          <article className="wiki-prose px-4 py-5 sm:px-6">
            <ReactMarkdown remarkPlugins={[remarkGfm, remarkKbd, remarkHeadings]} components={{ ...markdownComponents,
              a: ({ href = "", children }) => {
                if (/^[a-z]+:/i.test(href)) return <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>;
                if (href.startsWith("#")) return <a href={href}>{children}</a>;
                const target = toRoute(resolve(href, current + ".md")) + (href.includes("#") ? href.slice(href.indexOf("#")) : "");
                return <a href={`${base}/${target}`} onClick={(e) => { e.preventDefault(); navigate(`${base}/${target}`); }}>{children}</a>;
              },
              img: ({ src = "", alt }) => {
                const source = typeof src === "string" ? src : "";
                const url = /^(data:|https?:)/.test(source) ? source : `${apiBase}/attachment?path=${encodeURIComponent(resolve(source, current + ".md"))}`;
                return <img src={url} alt={alt ?? ""} loading="lazy" />;
              },
            }}>{page.data.content}</ReactMarkdown>
          </article>
        )}
      </Card>
    </div>
  );
}
