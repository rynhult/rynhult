import { expect, test } from "@playwright/test";
import { expectAxeClean } from "./helpers";

test("axe waits for theme transitions instead of sampling intermediate colours", async ({ page }) => {
  await page.setContent(`
    <html lang="en"><head><title>Theme transition</title><style>
      button { color: #ededef; background: #18181c; font: 13px sans-serif;
        transition: color 2s linear, background-color 2s linear; }
      .light button { color: #18181b; background: #f2f2f4; }
    </style></head><body><main><button>Open as text</button></main></body></html>
  `);
  await page.locator("button").evaluate((button) => {
    getComputedStyle(button).color;
    document.documentElement.classList.add("light");
    getComputedStyle(button).color;
    for (const animation of button.getAnimations()) animation.currentTime = 1000;
  });
  await expectAxeClean(page);
  await expect(page.locator("button")).toHaveCSS("color", "rgb(24, 24, 27)");
});

test("axe still rejects low contrast and restores looping animations after failure", async ({ page }) => {
  await page.setContent(`
    <html lang="en"><head><title>Low contrast</title><style>
      @keyframes spin { to { transform: rotate(360deg); } }
      span { display: inline-block; animation: spin 1s linear infinite; }
    </style></head>
    <body><main><span aria-hidden="true">*</span>
      <p style="color: #aaa; background: #fff">Unreadable text</p></main></body></html>
  `);
  await expect(expectAxeClean(page)).rejects.toThrow(/color-contrast/);
  expect(await page.locator("span").evaluate((span) => span.getAnimations()[0].playState)).toBe("running");
});
