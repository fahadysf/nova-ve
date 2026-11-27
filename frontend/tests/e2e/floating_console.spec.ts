// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { expect, test, type Locator, type Page, type WebSocketRoute } from '@playwright/test';

const labPath = '/floating-console-fixture.json';
const labApi = `/api/labs${labPath}`;
const nodeName = 'Router 1';

test.use({ viewport: { width: 1440, height: 900 } });

async function setUpConsoleLab(
  page: Page,
  secondNode = false,
  onWebSocket?: (socket: WebSocketRoute) => void,
  onNodes?: (nodes: Record<string, { id: number; name: string; left: number }>) => void
) {
  const router = {
    id: 1, name: nodeName, type: 'qemu', template: 'linux', image: 'linux',
    status: 2, console: 'telnet', cpu: 1, ram: 256, ethernet: 0, delay: 0,
    left: 400, top: 160, interfaces: []
  };
  await page.route('**/api/**', async (route) => {
    const { pathname } = new URL(route.request().url());
    const ok = (data: unknown) => route.fulfill({ json: { code: 200, status: 'success', data } });
    if (pathname === '/api/auth') return ok({ username: 'admin', role: 'admin', html5: true });
    if (pathname === labApi) return ok({ id: 'lab-id', name: 'Console fixture lab', path: labPath, revision: 1 });
    if (pathname === `${labApi}/nodes`) {
      const nodes = {
        '1': router,
        ...(secondNode ? { '2': { ...router, id: 2, name: 'Router 2', left: 1080 } } : {})
      };
      onNodes?.(nodes);
      return ok(nodes);
    }
    if (pathname === `${labApi}/networks`) return ok({});
    if (pathname.startsWith(`${labApi}/nodes/`) && pathname.endsWith('/recording')) return ok({ recording: false });
    if (pathname.startsWith(`${labApi}/nodes/`) && pathname.endsWith('/html5')) return route.fulfill({
      contentType: 'text/html',
      body: '<!doctype html><input aria-label="Console input">'
    });
    return ok([]);
  });
  await page.routeWebSocket('**/ws/labs/**', (socket) => {
    if (onWebSocket) onWebSocket(socket);
    else socket.send(JSON.stringify({ type: 'hello', seq: 1 }));
  });
  await page.goto(`/labs${labPath}`);
}

async function openFloatingConsole(page: Page, secondNode = false) {
  await setUpConsoleLab(page, secondNode);
  await page.getByRole('button', { name: 'Floating', exact: true }).click();
  const window = await openNodeConsole(page, 1, nodeName);
  const input = window.frameLocator(`iframe[title="Guacamole console for ${nodeName}"]`)
    .getByRole('textbox', { name: 'Console input' });
  await input.fill('show version');
  return { window, input };
}

async function clickNodeConsole(page: Page, nodeId: number) {
  const icon = page.locator(`.svelte-flow__node[data-id="node${nodeId}"]`).getByTestId('node-icon-container');
  await expect(icon).toBeVisible();
  await icon.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    element.dispatchEvent(new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, button: 2,
      clientX: rect.x + rect.width / 2, clientY: rect.y + rect.height / 2,
    }));
  });
  await page.getByTestId('node-menu-console-primary').click();
}

async function openNodeConsole(page: Page, nodeId: number, name: string) {
  await clickNodeConsole(page, nodeId);
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
  const maximized = await visibleBounds(window);
  await restore(window).click();
  const restored = await visibleBounds(window);
  expect(restored.width).toBeLessThan(maximized.width);
  expect(restored.height).toBeLessThan(maximized.height);
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

for (const closing of ['active', 'inactive'] as const) {
  test(`closing the ${closing} tab returns keyboard input to the remaining console`, async ({ page }) => {
    await setUpConsoleLab(page, true);
    await page.getByRole('button', { name: 'Tabbed', exact: true }).click();
    await clickNodeConsole(page, 1);
    const workspace = page.getByRole('dialog', { name: 'Console workspace' });
    const iframeSelector = (node: number) => `iframe[title="Guacamole console for Router ${node}"]`;
    const input = (node: number) => workspace.frameLocator(iframeSelector(node))
      .locator('input[aria-label="Console input"]');
    await input(1).fill('show version');
    await workspace.getByLabel('Console target').selectOption('2');
    await input(2).fill('show interfaces');

    const closedNode = closing === 'active' ? 2 : 1;
    const remainingNode = closing === 'active' ? 1 : 2;
    const remainingInput = input(remainingNode);
    await remainingInput.evaluate((element) => {
      element.ownerDocument.addEventListener('keydown', (event) => {
        element.setAttribute('data-last-key', event.key);
      });
    });
    const close = workspace.getByRole('button', { name: `Close Router ${closedNode}` });
    if (closing === 'active') {
      await close.click();
    } else {
      await close.focus();
      await page.keyboard.press('Enter');
    }
    await expect(workspace.locator(iframeSelector(remainingNode))).toBeFocused();
    await expect(remainingInput).toHaveValue(closing === 'active' ? 'show version' : 'show interfaces');
    await page.keyboard.press('x');
    await expect(remainingInput).toHaveAttribute('data-last-key', 'x');
  });
}

for (const activation of ['pointer', 'keyboard'] as const) {
  test(`switching loaded tabs by ${activation} preserves sessions and routes immediate typing only to the selected console`, async ({ page }) => {
    await setUpConsoleLab(page, true);
    await page.getByRole('button', { name: 'Tabbed', exact: true }).click();
    await clickNodeConsole(page, 1);
    const workspace = page.getByRole('dialog', { name: 'Console workspace' });
    const iframe = (node: number) => workspace.locator(`iframe[title="Guacamole console for Router ${node}"]`);
    const input = (node: number) => workspace.frameLocator(`iframe[title="Guacamole console for Router ${node}"]`)
      .locator('input[aria-label="Console input"]');
    const tab = (node: number) => workspace.getByRole('button', { name: `Router ${node}`, exact: true });
    await input(1).fill('first');
    await clickNodeConsole(page, 2);
    await input(2).fill('second');

    const firstIdentity = await iframe(1).evaluateHandle((element) => element);
    const secondIdentity = await iframe(2).evaluateHandle((element) => element);
    const firstSrc = await iframe(1).getAttribute('src');
    const secondSrc = await iframe(2).getAttribute('src');
    const expected = { 1: 'first', 2: 'second' };

    for (const selected of [1, 2] as const) {
      if (activation === 'pointer') {
        await tab(selected).click();
      } else {
        await tab(selected).focus();
        await page.keyboard.press('Enter');
      }
      const typed = selected === 1 ? '-one' : '-two';
      await page.keyboard.type(typed);
      expected[selected] += typed;
      await expect(iframe(selected)).toBeFocused();
      await expect(input(selected)).toHaveValue(expected[selected]);
      await expect(input(selected === 1 ? 2 : 1)).toHaveValue(expected[selected === 1 ? 2 : 1]);
    }

    expect(await iframe(1).evaluate((element, previous) => element === previous, firstIdentity)).toBe(true);
    expect(await iframe(2).evaluate((element, previous) => element === previous, secondIdentity)).toBe(true);
    await expect(iframe(1)).toHaveAttribute('src', firstSrc!);
    await expect(iframe(2)).toHaveAttribute('src', secondSrc!);
    await firstIdentity.dispose();
    await secondIdentity.dispose();
  });
}

test('a topology reconnect and resnapshot preserve two tabbed consoles and keyboard focus', async ({ page }) => {
  let socket: WebSocketRoute | undefined;
  let reconnect = false;
  let socketConnections = 0;
  const closeReports: string[] = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (request.method() === 'POST' && url.pathname.endsWith('/console/close')) {
      closeReports.push(`${url.pathname}${url.search}`);
    }
  });

  await setUpConsoleLab(page, true, (connection) => {
    socket = connection;
    socketConnections += 1;
    connection.send(JSON.stringify({ type: 'hello', seq: 1 }));
    if (reconnect) {
      connection.send(JSON.stringify({ type: 'force_resnapshot' }));
      connection.send(JSON.stringify({ type: 'lab_topology', seq: 2, payload: { id: 'lab-id' } }));
    }
  }, (nodes) => {
    if (reconnect) nodes['3'] = { ...nodes['1'], id: 3, name: 'Resnapshot applied', left: 720 };
  });
  await page.getByRole('button', { name: 'Tabbed', exact: true }).click();
  await clickNodeConsole(page, 1);
  const workspace = page.getByRole('dialog', { name: 'Console workspace' });
  const iframe = (node: number) => workspace.locator(`iframe[title="Guacamole console for Router ${node}"]`);
  const input = (node: number) => workspace.frameLocator(`iframe[title="Guacamole console for Router ${node}"]`)
    .locator('input[aria-label="Console input"]');
  await input(1).fill('show version');
  await workspace.getByLabel('Console target').selectOption('2');
  await input(2).fill('show interfaces');
  await input(2).evaluate((element) => {
    element.ownerDocument.addEventListener('keydown', (event) => {
      element.setAttribute('data-last-key', event.key);
    });
  });

  const firstIdentity = await iframe(1).evaluateHandle((element) => element);
  const secondIdentity = await iframe(2).evaluateHandle((element) => element);
  const firstSrc = await iframe(1).getAttribute('src');
  const secondSrc = await iframe(2).getAttribute('src');
  reconnect = true;
  socket?.close();
  await expect.poll(() => socketConnections).toBe(2);
  await expect(page.locator('.svelte-flow__node').filter({ hasText: 'Resnapshot applied' })).toBeVisible();

  expect(await iframe(1).evaluate((element, previous) => element === previous, firstIdentity)).toBe(true);
  expect(await iframe(2).evaluate((element, previous) => element === previous, secondIdentity)).toBe(true);
  await expect(iframe(1)).toHaveAttribute('src', firstSrc!);
  await expect(iframe(2)).toHaveAttribute('src', secondSrc!);
  await expect(workspace.getByLabel('Console target')).toHaveValue('2');
  await expect(input(1)).toHaveValue('show version');
  await expect(input(2)).toHaveValue('show interfaces');
  await expect(iframe(2)).toBeFocused();
  await page.keyboard.press('x');
  await expect(input(2)).toHaveAttribute('data-last-key', 'x');
  await expect(input(2)).toHaveValue('show interfacesx');
  expect(closeReports).toEqual([]);
  await firstIdentity.dispose();
  await secondIdentity.dispose();
  await workspace.getByRole('button', { name: 'Close console workspace' }).click();
  await expect.poll(() => closeReports.length).toBe(2);
});
