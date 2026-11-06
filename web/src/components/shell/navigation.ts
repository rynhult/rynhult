import { House, PanelsTopLeft, SlidersHorizontal, type LucideIcon } from 'lucide-react';
import { navFor, type NavItem } from '../nav';
export type Mode = 'overview' | 'inbox' | 'sessions' | 'projects' | 'workspaces' | 'hosts' | 'grid' | 'tools' | 'settings';
/** Rail glyphs that differ from the standard navigation, so Overview and Grid stay distinct at a glance. */
const railIcons: Partial<Record<Mode, LucideIcon>> = { overview: House, workspaces: PanelsTopLeft };
const modes: [Mode, string][] = [['overview', '/'], ['inbox', '/inbox'], ['sessions', '/sessions'], ['projects', '/projects'], ['workspaces', '/workspaces'], ['hosts', '/host'], ['grid', '/grid'], ['settings', '/settings']];
/** Presentation-only visibility hook; authorization always comes from navFor. */
export function railFor(owner: boolean, groupAdmin = false, features: Record<string, boolean> = {}, visible: (mode: Mode) => boolean = () => true) {
  const nav = navFor(owner, groupAdmin, features).flatMap(g => g.items);
  const items = modes.flatMap(([id, to]) => {
    const n = nav.find(n => n.to === to);
    return n ? [{ ...n, id, to: id === 'hosts' ? '/hosts' : n.to, icon: railIcons[id] ?? n.icon, label: id === 'hosts' ? 'Hosts' : n.label }] : [];
  });
  const tool = nav.find(item => item.to === '/catalog') ?? nav.find(item => !modes.some(([, to]) => to === item.to));
  if (tool) items.splice(Math.max(0, items.length - 1), 0, { id: 'tools', to: tool.to, label: 'Tools', icon: SlidersHorizontal });
  return items.filter(item => visible(item.id));
}
export function toolsFor(owner: boolean, groupAdmin = false, features: Record<string, boolean> = {}, visible: (path: string) => boolean = () => true): NavItem[] {
  return navFor(owner, groupAdmin, features).flatMap(g => g.items).filter(n => !modes.some(([, to]) => to === n.to) && visible(n.to));
}

/** Match whole route segments; use only the person's authorized navigation. */
export function modeForPath(pathname: string, rail: ReturnType<typeof railFor>, tools: NavItem[]): Mode | undefined {
  const matches = (path: string) => pathname === path || (path !== '/' && pathname.startsWith(`${path}/`));
  const primary = rail.find(item => matches(item.to) || (item.id === 'hosts' && matches('/host')) || (item.id === 'sessions' && matches('/multiruns')));
  if (primary) return primary.id;
  if (rail.some(item => item.id === 'tools') && tools.some(item => matches(item.to))) return 'tools';
}
