// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { expect, test, type Locator, type Page } from '@playwright/test';

const labPath = '/floating-console-fixture.json';
const labApi = `/api/labs${labPath}`;
const nodeName = 'Router 1';

test.use({ viewport: { width: 1440, height: 900 } });

async function openFloatingConsole(page: Page, secondNode = false) {
  const router = {
    id: 1, name: nodeName, type: 'qemu', template: 'linux', image: 'linux',
    status: 2, console: 'telnet', cpu: 1, ram: 256, ethernet: 0, delay: 0,
    left: 400, top: 160, interfaces: []
  };
  await page.route('**/api/**', async (route) => {
    const { pathname } = new URL(route.request().url());
    const ok = (data: unknown) => route.fulfill({ json: { code: 200, status: 'success', data } });
    if (pathname === '/api/auth') return ok({ username: 'admin', role: 'admin', html5: true });
    if (pathname === labApi) return ok({ id: 'lab-id', name: 'Floating console lab', path: labPath });
    if (pathname === `${labApi}/nodes`) return ok({
      '1': router,
      ...(secondNode ? { '2': { ...router, id: 2, name: 'Router 2', left: 1080 } } : {})
    });
    if (pathname === `${labApi}/networks`) return ok({});
    if (pathname.startsWith(`${labApi}/nodes/`) && pathname.endsWith('/recording')) return ok({ recording: false });
    if (pathname.startsWith(`${labApi}/nodes/`) && pathname.endsWith('/html5')) return route.fulfill({
      contentType: 'text/html',
      body: '<!doctype html><input aria-label="Console input">'
    });
    return ok([]);
  });
  await page.routeWebSocket('**/ws/labs/**', () => {});
  await page.goto(`/labs${labPath}`);
  await page.getByRole('button', { name: 'Floating', exact: true }).click();
  const window = await openNodeConsole(page, 1, nodeName);
  const input = window.frameLocator(`iframe[title="Guacamole console for ${nodeName}"]`)
    .getByRole('textbox', { name: 'Console input' });
  await input.fill('show version');
  return { window, input };
}

async function openNodeConsole(page: Page, nodeId: number, name: string) {
  await page.locator(`.svelte-flow__node[data-id="node${nodeId}"]`).getByTestId('node-icon-container')
    .click({ button: 'right' });
  await page.getByTestId('node-menu-console-primary').click();
  const window = page.getByRole('dialog', { name: `Console window for ${name}` });
  await expect(window).toBeVisible();
  return window;
}

async function visibleBounds(locator: Locator) {
  const bounds = await locator.boundingBox();
  if (!bounds) throw new Error('Expected a visible element with a bounding box');
  return bounds;
}

async function dragCorner(
  page: Page,
  window: Locator,
  corner: 'left' | 'right',
  decreaseWidth: number,
  decreaseHeight: number
) {
  const handle = window.getByRole('button', { name: `Resize ${nodeName} console from bottom ${corner}` });
  const bounds = await visibleBounds(handle);
  const x = Math.round(bounds.x + bounds.width / 2);
  const y = Math.round(bounds.y + bounds.height / 2);
  const direction = corner === 'left' ? 1 : -1;
  const iframePoint = { x: x + 32 * direction, y: y - 32 };
  await expect.poll(() => page.evaluate(({ x, y }) =>
    document.elementFromPoint(x, y)?.tagName, iframePoint)).toBe('IFRAME');

  await page.mouse.move(x, y);
  await page.mouse.down();
  // The first move immediately enters the iframe, outside the handle's original hit area.
  await page.mouse.move(iframePoint.x, iframePoint.y);
  await page.mouse.move(x + decreaseWidth * direction, y - decreaseHeight, { steps: 3 });
  await page.mouse.up();
}

const maximize = (window: Locator, name = nodeName) => window.getByRole('button', { name: `Maximize ${name} console` });
const restore = (window: Locator, name = nodeName) => window.getByRole('button', { name: `Restore ${name} console` });

test('the green button repeatedly restores the standard opening size after a drag resize', async ({ page }) => {
  const { window, input } = await openFloatingConsole(page);
  const opening = await visibleBounds(window);
  await dragCorner(page, window, 'right', 96, 64);
  expect((await visibleBounds(window)).width).toBeLessThan(opening.width);

  for (let cycle = 0; cycle < 2; cycle++) {
    await maximize(window).click();
    const maximized = await visibleBounds(window);
    expect(maximized.width).toBeGreaterThan(opening.width);
    expect(maximized.height).toBeGreaterThan(opening.height);
    await restore(window).click();
    await expect.poll(() => visibleBounds(window)).toEqual(opening);
  }

  await expect(input).toHaveValue('show version');
});

for (const corner of ['left', 'right'] as const) {
  test(`dragging the bottom ${corner} corner shrinks normal and maximized windows`, async ({ page }) => {
    const { window, input } = await openFloatingConsole(page);

    for (const startMaximized of [false, true]) {
      if (startMaximized) await maximize(window).click();
      const before = await visibleBounds(window);
      const decreaseWidth = 104;
      const decreaseHeight = 66;
      await dragCorner(page, window, corner, decreaseWidth, decreaseHeight);

      await expect.poll(() => visibleBounds(window)).toEqual({
        x: corner === 'left' ? before.x + decreaseWidth : before.x,
        y: before.y,
        width: before.width - decreaseWidth,
        height: before.height - decreaseHeight
      });
      await expect(maximize(window)).toBeVisible();
      await expect(restore(window)).toHaveCount(0);
      await expect(input).toHaveValue('show version');
    }
  });
}

test('the floating window stays inside the viewport when the browser shrinks', async ({ page }) => {
  const { window, input } = await openFloatingConsole(page);
  await maximize(window).click();
  await page.setViewportSize({ width: 600, height: 440 });
  await expect.poll(async () => {
    const bounds = await visibleBounds(window);
    return bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= 600 &&
      bounds.y + bounds.height <= 440;
  }).toBe(true);
  await restore(window).click();
  await expect(window.getByRole('button', { name: `Resize ${nodeName} console from bottom right` })).toBeInViewport();
  await expect(input).toHaveValue('show version');
});

test('restoring a second console preserves its own opening position and the first window', async ({ page }) => {
  const { window: first, input } = await openFloatingConsole(page, true);
  await dragCorner(page, first, 'right', 96, 64);
  const resizedFirst = await visibleBounds(first);
  const second = await openNodeConsole(page, 2, 'Router 2');
  const openingSecond = await visibleBounds(second);
  expect(openingSecond.x).not.toBe(resizedFirst.x);

  await maximize(second, 'Router 2').click();
  await restore(second, 'Router 2').click();
  await expect.poll(() => visibleBounds(second)).toEqual(openingSecond);
  await second.getByRole('button', { name: 'Close Router 2 console' }).click();
  await expect.poll(() => visibleBounds(first)).toEqual(resizedFirst);
  await expect(input).toHaveValue('show version');
});
