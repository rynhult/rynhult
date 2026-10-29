import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Network, X } from "lucide-react";
import { api, post } from "../lib/api";
import { Badge, Button, Card, CardHeader, ErrorNote, IconButton } from "./ui";

export interface PortEntry { port: number; range: "dev" | "prod" | "other"; project_id: number | null; project: string | null; mine: boolean }

/** A project's reserved ports (Rynhult's port registry): what its services listen on, and what it may publish. */
export function PortsCard({ projectId }: { projectId: number }) {
  const queryClient = useQueryClient();
  const ports = useQuery({ queryKey: ["ports"], queryFn: () => api<PortEntry[]>("/api/ports") });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["ports"] });
  const reserve = useMutation({ mutationFn: (range: "dev" | "prod") => post(`/api/projects/${projectId}/ports`, { range }), onSuccess: refresh });
  const release = useMutation({ mutationFn: (port: number) => post(`/api/ports/${port}/release`), onSuccess: refresh });
  const mine = (ports.data ?? []).filter((p) => p.project_id === projectId);
  return (
    <Card>
      <CardHeader title="Ports" icon={Network} subtitle="Reserved for this project: its services listen here, and only these can be published"
        action={<span className="flex gap-1">
          <Button size="sm" onClick={() => reserve.mutate("dev")} disabled={reserve.isPending}>+ Dev port</Button>
          <Button size="sm" onClick={() => reserve.mutate("prod")} disabled={reserve.isPending}>+ Prod port</Button>
        </span>} />
      <div className="flex flex-wrap gap-2 p-4">
        {mine.length ? mine.map((p) => (
          <span key={p.port} className="inline-flex items-center gap-1.5 rounded-md border border-line py-0.5 pl-2.5 pr-1 font-mono text-[13px]">
            {p.port} <Badge>{p.range}</Badge>
            <IconButton label={`Release port ${p.port}`} icon={X} onClick={() => release.mutate(p.port)} />
          </span>
        )) : <p className="text-xs text-faint">None yet. Dev ports are 8000–8099, prod 8100–8199; Rynhult picks the next free one.</p>}
      </div>
      <div className="px-4 pb-3"><ErrorNote error={reserve.error ?? release.error ?? ports.error} /></div>
    </Card>
  );
}
