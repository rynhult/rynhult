import { StrictMode, useRef, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { useDismissableLayer, useRovingFocus } from "../src/components/keyboard";
import { Popover } from "../src/components/ui";

function Navigation() {
  const root = useRef<HTMLDivElement>(null);
  const [value, setValue] = useState("Alpha");
  const [prevented, setPrevented] = useState(false);
  const [generation, setGeneration] = useState(0);
  const onKeyDown = useRovingFocus(root, {
    selector: "[data-item]", orientation: "horizontal", typeahead: true,
    onNavigate: (item) => setValue(item.textContent!),
  });
  return <div>
    <div key={generation} ref={root} role="toolbar" aria-label="Navigation" onKeyDown={(event) => { onKeyDown(event); setPrevented(event.defaultPrevented); }}>
      {["Alpha", "Apricot", "Banana", "Blueberry", "Bravo", "Basil", "Charlie"].map((name) =>
        <button key={name} data-item disabled={name === "Banana"} hidden={name === "Blueberry"}
          tabIndex={value === name ? 0 : -1} onClick={() => setValue(name)}>{name}</button>)}
      <div inert><button data-item>Blocked</button></div>
    </div>
    <output aria-label="Selected">{value}</output><output aria-label="Prevented">{String(prevented)}</output>
    <button>After navigation</button>
    <button onClick={() => { setGeneration((v) => v + 1); setValue("Alpha"); }}>Remount navigation</button>
  </div>;
}

function Scope({ label, children, onClose }: { label: string; children: ReactNode; onClose: () => void }) {
  const root = useRef<HTMLDivElement>(null);
  const { onFocusCapture } = useDismissableLayer({ open: true, root, onDismiss: onClose, modal: true });
  return <div ref={root} role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} onFocusCapture={onFocusCapture}>{children}</div>;
}

function Layers() {
  const [primary, setPrimary] = useState(false);
  const [secondary, setSecondary] = useState(false);
  const [empty, setEmpty] = useState(false);
  const [popup, setPopup] = useState(false);
  const [tailMenu, setTailMenu] = useState(false);
  const [version, setVersion] = useState(0);
  const [closedVersion, setClosedVersion] = useState(-1);
  const root = useRef<HTMLDivElement>(null);
  const { onFocusCapture } = useDismissableLayer({ open: popup, root, dismissOnOutside: true,
    onDismiss: () => setPopup(false), initialFocus: (box) => box.querySelector("[data-popup-item]") });
  return <div>
    <button onClick={() => setPrimary(true)}>Open primary</button>
    <button onClick={() => setEmpty(true)}>Open empty</button>
    <button onClick={() => setTailMenu(true)}>Open menu modal</button>
    <button onClick={() => setVersion((v) => v + 1)}>Rerender</button>
    <output aria-label="Closed version">{closedVersion}</output>
    {primary && <Scope label="Primary" onClose={() => { setPrimary(false); setClosedVersion(version); }}>
      <input aria-label="First field" autoFocus />
      <input aria-label="Not tabbable" tabIndex={-1} />
      <input aria-label="Own Escape" onKeyDown={(event) => { if (event.key === "Escape") event.preventDefault(); }} />
      <button disabled>Disabled</button>
      <fieldset disabled><button>Disabled by fieldset</button></fieldset>
      <button hidden>Hidden</button>
      <div inert><button>Inert</button></div>
      <button onClick={() => setSecondary(true)}>Open secondary</button>
      <div ref={root} onFocusCapture={onFocusCapture}>
        <button onClick={() => setPopup((v) => !v)}>Open popup</button>
        {popup && <div role="group" aria-label="Popup"><button data-popup-item>Popup item</button></div>}
      </div>
      <button onClick={() => setPrimary(false)}>Close primary</button>
    </Scope>}
    {secondary && <Scope label="Secondary" onClose={() => setSecondary(false)}><input aria-label="Second field" autoFocus /><button>Last secondary</button></Scope>}
    {empty && <Scope label="Empty" onClose={() => setEmpty(false)}>No focusable controls.</Scope>}
    {tailMenu && <Scope label="Menu modal" onClose={() => setTailMenu(false)}>
      <input aria-label="Menu first field" autoFocus />
      <Popover menu label="Tail actions" trigger="Tail actions">{close =>
        <button role="menuitem" tabIndex={-1} onClick={close}>Final action</button>}
      </Popover>
    </Scope>}
  </div>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><Navigation /><Layers />
  <Popover menu label="Link actions" trigger="Link actions">{close =>
    <a role="menuitem" tabIndex={-1} href="#chosen" onClick={close}>Go to notes</a>}
  </Popover>
</StrictMode>);
