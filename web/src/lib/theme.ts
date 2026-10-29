/* Theme: System (the default, following the device), Light or Dark (UX review NAV-5, owner decision 0008 Q7).
   The choice is stored per browser as "acc-theme"; no value means System. */
import { useEffect } from "react";

export type ThemeChoice = "system" | "light" | "dark";

export const THEME_LABEL: Record<ThemeChoice, string> = { system: "System", light: "Light", dark: "Dark" };
export const NEXT_THEME: Record<ThemeChoice, ThemeChoice> = { system: "light", light: "dark", dark: "system" };

export function readTheme(): ThemeChoice {
  try {
    const value = localStorage.getItem("acc-theme");
    return value === "light" || value === "dark" ? value : "system";
  } catch { return "system"; }
}

export function saveTheme(choice: ThemeChoice) {
  try {
    if (choice === "system") localStorage.removeItem("acc-theme"); else localStorage.setItem("acc-theme", choice);
  } catch { /* storage unavailable: the choice lasts until reload */ }
}

const systemDark = () => (typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)").matches : true);

export function applyTheme(choice: ThemeChoice) {
  document.documentElement.classList.toggle("dark", choice === "dark" || (choice === "system" && systemDark()));
  const background = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
  document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]').forEach(meta => { meta.content = background; });
}

export function useAppliedTheme(choice: ThemeChoice = readTheme()) {
  useEffect(() => {
    applyTheme(choice);
    if (choice !== "system" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const follow = () => applyTheme("system");
    media.addEventListener("change", follow);
    return () => media.removeEventListener("change", follow);
  }, [choice]);
}
