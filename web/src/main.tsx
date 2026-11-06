import { StrictMode, lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Announcer } from "./components/ui";
import { App } from "./App";
import { ConfirmProvider } from "./components/confirm";
import "./index.css";
import { applyTheme, readTheme } from "./lib/theme";

const PersonalThemeRuntime = lazy(() => import("./components/PersonalThemeRuntime"));
const TerminalOwner = import.meta.env.VITE_P2_TERMINAL_HOST === '1'
  ? lazy(() => import('./components/PersistentTerminals').then(module => ({default: module.PersistentTerminalProvider})))
  : null;

applyTheme(readTheme());

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 2000, retry: 1, refetchOnWindowFocus: true } } });
const router = createBrowserRouter([{ path: "*", element: <App /> }]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <Announcer />
      <Suspense fallback={null}><PersonalThemeRuntime /></Suspense>
      <ConfirmProvider>
        <Suspense fallback={null}>{TerminalOwner ? <TerminalOwner><RouterProvider router={router}/></TerminalOwner> : <RouterProvider router={router}/>}</Suspense>
      </ConfirmProvider>
    </QueryClientProvider>
  </StrictMode>,
);

// The service worker only shows push notifications (no offline cache). Browsers allow it on HTTPS or localhost.
if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "127.0.0.1" || location.hostname === "localhost")) {
  navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" })
    .then(registration => registration.update()).catch(() => undefined);
}
