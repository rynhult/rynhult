import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { markdownComponents } from "./Markdown";

export default function FileMarkdownPreview({ text }: { text: string }) {
  return <div className="wiki-prose break-words">
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
      ...markdownComponents,
      img: ({ alt }) => <span>[Image: {alt}]</span>,
      a: ({ children, href }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
    }}>{text}</ReactMarkdown>
  </div>;
}
