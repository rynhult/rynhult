import { expect, test } from "@playwright/test";
import { wikiShotPath } from "./helpers";

test("ordinary captures use per-test output; wiki writes require explicit opt-in", async ({}, testInfo) => {
  expect(wikiShotPath(testInfo, "example.png", false)).toBe(testInfo.outputPath("example.png"));
  expect(wikiShotPath(testInfo, "example.png", true)).toBe("../docs/wiki/.attachments/example.png");
});
