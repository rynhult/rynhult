import { useEffect, useState } from "react";
import { readTheme, type ThemeChoice } from "./theme";

export const THEME_LABEL: Record<ThemeChoice, string> = { system: "System", light: "Light", dark: "Dark" };
export const NEXT_THEME: Record<ThemeChoice, ThemeChoice> = { system: "light", light: "dark", dark: "system" };

export function saveTheme(choice: ThemeChoice) {
  try {
    if (choice === "system") localStorage.removeItem("acc-theme"); else localStorage.setItem("acc-theme", choice);
  } catch { /* storage unavailable: the choice lasts until reload */ }
  window.dispatchEvent(new CustomEvent("acc-theme-change", { detail: choice }));
}

/** Keep the account control and existing quick toggle in sync without changing preference scope. */
export function useThemeChoice() {
  const [choice, setChoice] = useState<ThemeChoice>(readTheme);
  useEffect(() => {
    const update = (event: Event) => setChoice(event instanceof CustomEvent ? event.detail : readTheme());
    window.addEventListener('acc-theme-change', update);
    window.addEventListener('storage', update);
    return () => { window.removeEventListener('acc-theme-change', update); window.removeEventListener('storage', update); };
  }, []);
  return choice;
}
