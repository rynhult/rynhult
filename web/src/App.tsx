import { Feature } from "./components/Feature";
import { useQuery } from "@tanstack/react-query";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { desktopBuild } from "./lib/desktop";
import { api } from "./lib/api";
import type { AuthState } from "./lib/types";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Card, Empty, Page } from "./components/ui";
import { useRole, useHealth } from "./lib/queries";
import { Lock } from "lucide-react";
import { lazy, type ReactNode, Suspense } from "react";
import { lazyImport } from "./lib/lazyImport";
import { ChunkReloadBanner } from "./components/ChunkReloadBanner";

const DesktopExecution = import.meta.env.VITE_DESKTOP === "1"
  ? lazy(() => lazyImport(() => import("./components/DesktopExecution")).then(m => ({ default: m.DesktopExecution })))
  : () => null;

const Layout = lazy(() => lazyImport(() => import("./components/Layout")).then((m) => ({ default: m.Layout })));
const HighAvailability = lazy(() => lazyImport(() => import("./pages/HighAvailability")).then((m) => ({ default: m.HighAvailability })));
const WorkspaceFiles = lazy(() => lazyImport(() => import("./pages/WorkspaceFiles")).then((m) => ({ default: m.WorkspaceFiles })));
const Overview = lazy(() => lazyImport(() => import("./pages/Overview")).then((m) => ({ default: m.Overview })));
const Projects = lazy(() => lazyImport(() => import("./pages/Projects")).then((m) => ({ default: m.Projects })));
const SessionPage = lazy(() => lazyImport(() => import("./pages/SessionPage")).then((m) => ({ default: m.SessionPage })));
const InviteAccept = lazy(() => lazyImport(() => import("./pages/Auth")).then((m) => ({ default: m.InviteAccept })));
const Login = lazy(() => lazyImport(() => import("./pages/Auth")).then((m) => ({ default: m.Login })));
const Setup = lazy(() => lazyImport(() => import("./pages/Auth")).then((m) => ({ default: m.Setup })));
const PasskeyStepUp = lazy(() => lazyImport(() => import("./pages/Auth")).then((m) => ({ default: m.PasskeyStepUp })));
const Inbox = lazy(() => lazyImport(() => import("./pages/Inbox")).then((m) => ({ default: m.Inbox })));
const Publishing = lazy(() => lazyImport(() => import("./pages/Publishing")).then((m) => ({ default: m.Publishing })));
const CatalogPage = lazy(() => lazyImport(() => import("./pages/CatalogPage")).then((m) => ({ default: m.CatalogPage })));
const ProjectPage = lazy(() => lazyImport(() => import("./pages/ProjectPage")).then((m) => ({ default: m.ProjectPage })));
const BackupTargetSetup = lazy(() => lazyImport(() => import("./pages/BackupTargetSetup")).then((m) => ({ default: m.BackupTargetSetup })));
const Host = lazy(() => lazyImport(() => import("./pages/Host")).then((m) => ({ default: m.Host })));
const Providers = lazy(() => lazyImport(() => import("./pages/Providers")).then((m) => ({ default: m.Providers })));
const Audit = lazy(() => lazyImport(() => import("./pages/Audit")).then((m) => ({ default: m.Audit })));
const Schedules = lazy(() => lazyImport(() => import("./pages/Schedules")).then((m) => ({ default: m.Schedules })));
const Sessions = lazy(() => lazyImport(() => import("./pages/Sessions")).then((m) => ({ default: m.Sessions })));
const MultiRunPage = lazy(() => lazyImport(() => import("./pages/MultiRunPage")).then((m) => ({ default: m.MultiRunPage })));
const Workspaces = lazy(() => lazyImport(() => import("./pages/Workspaces")).then((m) => ({ default: m.Workspaces })));
const Groups = lazy(() => lazyImport(() => import("./pages/Groups")).then((m) => ({ default: m.Groups })));
const Settings = lazy(() => lazyImport(() => import("./pages/Settings")).then((m) => ({ default: m.Settings })));
const Search = lazy(() => lazyImport(() => import("./pages/Search")).then((m) => ({ default: m.Search })));
const Grid = lazy(() => lazyImport(() => import("./pages/Grid")).then((m) => ({ default: m.Grid })));
const Board = lazy(() => lazyImport(() => import("./pages/Board")).then((m) => ({ default: m.Board })));

// The wiki pulls in the Markdown renderer; load it only when a wiki is opened.
const ProjectWiki = lazy(() => lazyImport(() => import("./pages/WikiPage")).then((m) => ({ default: m.ProjectWiki })));
const WorkspaceWiki = lazy(() => lazyImport(() => import("./pages/WikiPage")).then((m) => ({ default: m.WorkspaceWiki })));
const GuideWiki = lazy(() => lazyImport(() => import("./pages/WikiPage")).then((m) => ({ default: m.GuideWiki })));
const RemoteAccess = lazy(() => lazyImport(() => import("./pages/RemoteAccess")).then((m) => ({ default: m.RemoteAccess })));
const PairDevice = lazy(() => lazyImport(() => import("./pages/PairDevice")).then((m) => ({ default: m.PairDevice })));

/** Owner-only pages: members who follow a link there get an explanation, not a failed request. */
function OwnersOnly({ children }: { children: ReactNode }) {
  const { owner, known } = useRole();
  if (!known || owner) return <>{children}</>;
  return (
    <Page>
      <Card><Empty icon={Lock}>This page is for Rynhult's owners. Ask one if you need something from it.</Empty></Card>
    </Page>
  );
}

function RouteLoading() {
  return <div role="status" className="grid min-h-40 place-items-center text-sm text-muted">Loading page…</div>;
}

export function App() {
  const location = useLocation();
  return <div className="flex h-dvh flex-col">
    <ChunkReloadBanner />
    <div className="min-h-0 flex-1 overflow-auto">
      <ErrorBoundary resetKey={location.pathname} reloadOnRetry>
        <Suspense fallback={<RouteLoading />}><AppContent /></Suspense>
      </ErrorBoundary>
    </div>
  </div>;
}

function AppContent() {
  const location = useLocation();
  const health = useHealth();
  const auth = useQuery({ queryKey: ["auth"], queryFn: () => api<AuthState>("/api/auth/state") });
  // Only the first health result gates routing; failed background polls keep the current page mounted.
  if (health.isPending && !health.isFetched) return <div className="grid h-full place-items-center text-sm text-muted">Loading…</div>;
  if (health.data?.ha && (health.data.ha.role === "standby" || location.pathname === "/ha")) {
    return <HighAvailability status={health.data.ha} error={health.error} />;
  }
  if (location.pathname === "/pair-device") return <PairDevice />;
  if (!auth.data) return <div className="grid h-full place-items-center text-sm text-muted">{auth.error ? "Rynhult is unreachable." : "Loading…"}</div>;
  const { setup_required, authenticated, username } = auth.data;
  if (authenticated && auth.data.step_up_required) return <PasskeyStepUp />;
  if (setup_required && location.pathname !== "/setup") return <Navigate to="/setup" replace />;
  const invite = location.pathname.startsWith("/invite/");
  if (!setup_required && !authenticated && location.pathname !== "/login" && !invite) return <Navigate to="/login" replace />;
  if (desktopBuild && location.pathname === "/") return <Navigate to="/projects" replace />;
  return (
    <Routes>
      <Route path="/setup" element={setup_required ? <Setup /> : <Navigate to="/" replace />} />
      <Route path="/login" element={authenticated ? <Navigate to="/" replace /> : <Login />} />
      <Route path="/invite/:token" element={<InviteAccept />} />
      <Route path="/board" element={<Board />} />{/* full screen, without the sidebar: for a large display */}
      <Route path="*" element={
        <Layout username={username ?? ""}>
          {desktopBuild && <Suspense fallback={null}><DesktopExecution /></Suspense>}
          <Suspense fallback={<RouteLoading />}>
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/projects" element={<Projects />} />
            <Route path="/workspaces/:folder/files" element={<WorkspaceFiles />} />
            <Route path="/workspaces" element={<Workspaces />} />
            <Route path="/workspaces/:folder/wiki/*" element={<Suspense fallback={<RouteLoading />}><WorkspaceWiki /></Suspense>} />
            <Route path="/projects/:id" element={<ProjectPage />} />
            <Route path="/projects/:id/wiki/*" element={<Suspense fallback={<RouteLoading />}><ProjectWiki /></Suspense>} />
            <Route path="/guide/*" element={<Suspense fallback={<RouteLoading />}><GuideWiki /></Suspense>} />
            <Route path="/inbox" element={<Inbox />} />
            <Route path="/accounts" element={<Navigate to="/providers" replace />} />
            <Route path="/publishing" element={<Feature name="publishing" page><Publishing /></Feature>} />
            <Route path="/catalog" element={<CatalogPage />} />
            <Route path="/host/backups/setup" element={<OwnersOnly><BackupTargetSetup /></OwnersOnly>} />
            <Route path="/host" element={<Host />} />
            <Route path="/providers" element={<Providers />} />
            <Route path="/audit" element={<OwnersOnly><Audit /></OwnersOnly>} />
            <Route path="/groups" element={<Feature name="groups" page><Groups /></Feature>} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/settings/remote-access" element={<OwnersOnly><Feature name="remote_access" page><RemoteAccess /></Feature></OwnersOnly>} />
            <Route path="/search" element={<Search />} />
            <Route path="/grid" element={<Grid />} />
            <Route path="/schedules" element={<Feature name="schedules" page><Schedules /></Feature>} />
            <Route path="/sessions" element={<Sessions />} />
            <Route path="/multiruns/:id" element={<Feature name="multirun" page><MultiRunPage /></Feature>} />
            <Route path="/sessions/:id" element={<SessionPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
          </Suspense>
        </Layout>
      } />
    </Routes>
  );
}
