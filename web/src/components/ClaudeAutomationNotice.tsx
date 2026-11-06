import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
export type AutomationRefusal = { code: string; message: string; docs: string; keys?: string };
export function ClaudeApiKeyLink({ href = "/settings?claude_api=1#keys" }: { href?: string }) {
  const projectId = Number(new URL(href, window.location.origin).searchParams.get("project_id"));
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<{ id: number; name: string }[]>("/api/projects"), enabled: !!projectId });
  if (projectId && projects.isPending) return <span>Loading project key settings…</span>;
  if (projectId && projects.isError) return <span>Project key settings could not be loaded. Refresh to try again.</span>;
  if (projectId && !projects.data?.some(p => p.id === projectId)) return <span>For group projects, ask a group administrator to configure a Claude API key through the group key settings.</span>;
  return <a className="underline" href={href}>Use an API key for this project</a>;
}
export function ClaudeAutomationNotice({ refusal }: { refusal?: AutomationRefusal | null }) {
  if (!refusal) return null;
  return <p role="status" className="text-xs text-wait">{refusal.message}{' '}
    <ClaudeApiKeyLink href={refusal.keys} />{" "}
    <a className="underline" href={refusal.docs}>Claude guide</a></p>;
}
