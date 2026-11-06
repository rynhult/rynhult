import { expect, type Page } from '@playwright/test';

/** A document's load event does not include its later lazy route imports.
 * Account for actual downloads and initial data before checking the working view;
 * preserve each UI assertion's deadline instead of consuming it on cold IO.
 */
export async function gotoLoadedPage(page: Page, url: string, chunks: string[], dataPaths: string[] = []) {
  await loadPage(page, () => page.goto(url), chunks, dataPaths);
}

export async function reloadLoadedPage(page: Page, chunks: string[], dataPaths: string[] = []) {
  await loadPage(page, () => page.reload(), chunks, dataPaths);
}

export async function loadPage(page: Page, navigate: () => Promise<unknown>, chunks: string[], dataPaths: string[]) {
  const downloads = chunks.map(async chunk => {
    const response = await page.waitForResponse(response => {
      const path = new URL(response.url()).pathname;
      return path.startsWith(`/assets/${chunk}-`) && path.endsWith('.js');
    });
    expect(response.status(), `${chunk} route chunk`).toBe(200);
    expect(await response.finished(), `${chunk} route chunk body`).toBeNull();
  });
  const data = dataPaths.map(async path => {
    const response = await page.waitForResponse(response =>
      new URL(response.url()).pathname === path && response.request().method() === 'GET');
    expect(response.status(), `${path} initial data`).toBe(200);
    expect(await response.finished(), `${path} initial data body`).toBeNull();
  });
  await Promise.all([navigate(), ...downloads, ...data]);
}
