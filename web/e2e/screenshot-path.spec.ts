import { expect, test } from "./fixtures";
import { wikiShotPath } from "./helpers";

test("unlisted captures use per-test output; only desktop wiki writes accept explicit opt-in", async ({}, testInfo) => {
  for (const name of ["desktop", "desktop-stateful", "mobile", "mobile-stateful"]) {
    const info = { ...testInfo, project: { ...testInfo.project, name }, outputPath: testInfo.outputPath.bind(testInfo) };
    expect(wikiShotPath(info, "example.png", false)).toBe(testInfo.outputPath("example.png"));
    expect(wikiShotPath(info, "example.png", true)).toBe(
      name.startsWith("desktop") ? "../docs/wiki/.attachments/example.png" : testInfo.outputPath("example.png"));
  }
});

test("mobile-only wiki images accept opt-in without allowing shared desktop images", async ({}, testInfo) => {
  const mobileImages = ["settings-dns-mobile.png", "settings-dns-google-mobile.png", "ha-standby-phone.png"];
  for (const name of ["mobile", "mobile-stateful"]) {
    const info = { ...testInfo, project: { ...testInfo.project, name }, outputPath: testInfo.outputPath.bind(testInfo) };
    for (const image of mobileImages) {
      expect(wikiShotPath(info, image, true)).toBe(`../docs/wiki/.attachments/${image}`);
      expect(wikiShotPath(info, image, false)).toBe(testInfo.outputPath(image));
    }
    for (const image of ["troubleshooting-route-loading.png", "unlisted-phone.png"]) {
      expect(wikiShotPath(info, image, true)).toBe(testInfo.outputPath(image));
    }
  }
});
