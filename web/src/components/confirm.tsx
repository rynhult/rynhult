import { createContext, type ReactNode, useCallback, useContext, useRef, useState } from "react";
import { Button, Dialog } from "./ui";

/** `hold`: milliseconds the confirm button stays disabled, with focus on Cancel, for confirmations whose dialog an
 *  untrusted page can make appear under a person's next click or Enter (the preview picker). */
interface Ask { title: string; body?: ReactNode; confirm?: string; danger?: boolean; hold?: number }
const ConfirmContext = createContext<(ask: Ask) => Promise<boolean>>(async () => false);

/** In-app replacement for window.confirm: styled, keyboard-friendly, states the consequence. */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [ask, setAsk] = useState<Ask | null>(null);
  const [held, setHeld] = useState(false);
  const resolver = useRef<(ok: boolean) => void>(undefined);
  const holding = useRef<ReturnType<typeof setTimeout>>(undefined);
  const confirm = useCallback((next: Ask) => new Promise<boolean>((resolve) => {
    resolver.current = resolve;
    clearTimeout(holding.current);
    setHeld(!!next.hold);
    if (next.hold) holding.current = setTimeout(() => setHeld(false), next.hold);
    setAsk(next);
  }), []);
  const done = (ok: boolean) => { if (ok && held) return; clearTimeout(holding.current); resolver.current?.(ok); setAsk(null); };
  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <Dialog open={!!ask} onClose={() => done(false)} dismissible={false} title={ask?.title ?? ""}
        footer={<>
          <Button variant="ghost" onClick={() => done(false)} autoFocus={!!ask?.hold}>Cancel</Button>
          <Button variant={ask?.danger ? "danger" : "primary"} onClick={() => done(true)} disabled={held} autoFocus={!ask?.hold}>{ask?.confirm ?? "Confirm"}</Button>
        </>}>
        <div className="text-[13px] text-muted">{ask?.body}</div>
      </Dialog>
    </ConfirmContext.Provider>
  );
}

export const useConfirm = () => useContext(ConfirmContext);
