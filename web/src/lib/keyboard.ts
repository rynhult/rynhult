export type NavigationOptions = { orientation?: "horizontal" | "vertical" | "both"; loop?: boolean; homeEnd?: boolean };

export function navigationIndex(key: string, current: number, count: number, {
  orientation = "both", loop = true, homeEnd = true,
}: NavigationOptions = {}): number | null {
  if (!count) return null;
  if (homeEnd && key === "Home") return 0;
  if (homeEnd && key === "End") return count - 1;
  const forward = (orientation !== "vertical" && key === "ArrowRight") || (orientation !== "horizontal" && key === "ArrowDown");
  const backward = (orientation !== "vertical" && key === "ArrowLeft") || (orientation !== "horizontal" && key === "ArrowUp");
  if (!forward && !backward) return null;
  if (current < 0 || current >= count) return forward ? 0 : count - 1;
  const next = current + (forward ? 1 : -1);
  return loop ? (next + count) % count : Math.max(0, Math.min(count - 1, next));
}
