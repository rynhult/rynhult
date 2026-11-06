import { useDocumentTitle } from "../components/ui";
import type { ReactNode } from "react";

export function AuthShell({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  useDocumentTitle();
  return (
    <div className="grid min-h-full place-items-center bg-[radial-gradient(60rem_30rem_at_50%_-10%,color-mix(in_srgb,var(--accent)_12%,transparent),transparent)] p-4">
      <main className="min-w-0 w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <div className="grid size-11 shrink-0 place-items-center" aria-hidden="true">
            <img src="/rynhult-mark-light.svg" alt="" className="hidden w-1/2 dark:block" />
            <img src="/rynhult-mark-dark.svg" alt="" className="w-1/2 dark:hidden" />
          </div>
          <div>
            <h1 className="text-[20px] font-semibold tracking-[-0.01em]">{title}</h1>
            <p className="mt-0.5 text-[13px] text-muted">{subtitle}</p>
          </div>
        </div>
        <div className="rounded-xl border border-line bg-panel p-6 shadow-pop">{children}</div>
        <p className="mt-4 text-center text-xs text-faint">Your agents. Your machines. Your call.</p>
      </main>
    </div>
  );
}
