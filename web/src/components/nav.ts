import { desktopBuild } from "../lib/desktop";
import { MonitorDot, CalendarClock, Activity, BookOpen, LayoutGrid, Search, FlaskConical, FolderGit2, Gauge, Globe, Inbox, LayoutDashboard, LifeBuoy, type LucideIcon, ScrollText, Settings, TerminalSquare } from "lucide-react";

export interface NavItem { to: string; label: string; icon: LucideIcon; end?: boolean; ownerOnly?: boolean; groupAdminOnly?: boolean }

/* One navigation model for the sidebar, the phone tab bar and the command palette. */
export const NAV: { label?: string; items: NavItem[] }[] = [
  { items: [
    { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
    { to: "/inbox", label: "Inbox", icon: Inbox },
    { to: "/sessions", label: "Sessions", icon: TerminalSquare, end: true },
    { to: "/grid", label: "Grid", icon: LayoutGrid },
    { to: "/projects", label: "Projects", icon: FolderGit2 },
    { to: "/workspaces", label: "Workspaces", icon: FlaskConical },
    { to: "/search", label: "Search", icon: Search },
  ] },
  { label: "Operate", items: [
    { to: "/schedules", label: "Schedules", icon: CalendarClock },
    { to: "/publishing", label: "Publishing", icon: Globe },
    { to: "/host", label: "Host", icon: Activity },
    { to: "/providers", label: "Providers", icon: Gauge },
    { to: "/board", label: "Status board", icon: MonitorDot, ownerOnly: true },
  ] },
  { label: "Configure", items: [
    { to: "/catalog", label: "Catalog", icon: BookOpen },
    { to: "/audit", label: "Audit", icon: ScrollText, ownerOnly: true },
    { to: "/groups", label: "Groups", icon: FolderGit2, groupAdminOnly: true },
    { to: "/settings", label: "Settings", icon: Settings },
    { to: "/guide", label: "Guide", icon: LifeBuoy },
  ] },
];

/** NAV without what this person can't open (members don't see the host or the audit log). */
export const navFor = (owner: boolean, groupAdmin = false, features: Record<string, boolean> = {}) =>
  NAV.map((g) => ({ ...g, items: g.items.filter((n) => (features[({ "/schedules": "schedules", "/publishing": "publishing", "/groups": "groups" } as Record<string, string>)[n.to]] !== false) && (!desktopBuild || ["/inbox", "/sessions", "/projects", "/search", "/providers", "/audit", "/guide"].includes(n.to)) && (owner || (!n.ownerOnly && (!n.groupAdminOnly || groupAdmin)))) })).filter((g) => g.items.length);

export const PHONE_TABS = ["/", "/inbox", "/sessions", "/projects"];
