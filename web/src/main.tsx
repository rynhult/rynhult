import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Announcer } from "./components/ui";
import { App } from "./App";
import { ConfirmProvider } from "./components/confirm";
import "./index.css";
import { applyTheme, readTheme } from "./lib/theme";

applyTheme(readTheme());

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 2000, retry: 1, refetchOnWindowFocus: true } } });
const router = createBrowserRouter([{ path: "*", element: <App /> }]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <Announcer />
      <ConfirmProvider>
        <RouterProvider router={router} />
      </ConfirmProvider>
    </QueryClientProvider>
  </StrictMode>,
);

// The service worker only shows push notifications (no offline cache). Browsers allow it on HTTPS or localhost.
if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "127.0.0.1" || location.hostname === "localhost")) {
  navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" })
    .then(registration => registration.update()).catch(() => undefined);
}
