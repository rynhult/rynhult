import { defineConfig, devices } from "@playwright/test";
import base from "./playwright.config";
export default defineConfig({ ...base, workers: 1, projects: [
  { name: "setup", testMatch: /00-setup\.spec\.ts/, use: { ...devices["Desktop Chrome"] } },
  { name: "files-upload", dependencies: ["setup"], testMatch: /12-files(-upload)?\.spec\.ts/, workers: 1,
    use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
] });
