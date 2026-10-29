import type { AttentionItem } from "../lib/types";
import { Badge } from "./ui";

export function RiskLabel({ risk }: { risk: AttentionItem["risk"] }) {
  if (!risk) return null;
  return (
    <span role="note" aria-label={`Advisory risk: ${risk.label}`} title="Advisory only. You decide; this never answers a prompt."
      className="inline-flex min-w-0 max-w-full flex-wrap items-center gap-x-2 gap-y-1">
      <Badge tone={risk.label === "Risky" ? "bad" : risk.label === "Review" ? "wait" : "neutral"}>{risk.label}</Badge>
      <span className="min-w-0 break-words text-xs text-muted">{risk.reason}</span>
    </span>
  );
}
