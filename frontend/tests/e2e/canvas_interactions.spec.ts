// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { expect, test, type Page } from '@playwright/test';

// Mock all APIs, including lifecycle and link mutations: this spec never
// reads or changes a real lab or its workloads.
const labPath = '/canvas-interactions.json';
const labApi = `/api/labs${labPath}`;
const frontend = process.env.FRONTEND_URL ?? 'http://127.0.0.1:5173';

test.use({ viewport: { width: 1440, height: 900 }, hasTouch: false });

async function openLab(page: Page) {
  const node = (id: number, left: number, top: number) => ({
    id, name: `Node ${id}`, type: 'qemu', template: 'linux', image: 'linux',
    status: 0, console: 'telnet', cpu: 1, ram: 256, ethernet: 2, delay: 0,
    left, top, interfaces: [{ index: 0, name: 'eth0' }, { index: 1, name: 'eth1' }]
  });
  const nodes = { '1': node(1, 100, 120), '2': node(2, 470, 120), '3': node(3, 850, 120) };
  const networks = { '9': { id: 9, name: 'Shared bridge', type: 'linux_bridge', left: 290, top: 320, icon: 'lan.png', visibility: true, implicit: false, config: {} } };
  const links = [1, 2].map((id) => ({ id: `link-${id}`, from: { node_id: id, interface_index: 0 }, to: { network_id: 9 }, style_override: null, label: '', color: '', width: '1' }));
  await page.route('**/api/**', async (route) => {
    const { pathname } = new URL(route.request().url());
    const ok = (data: unknown) => route.fulfill({ json: { code: 200, status: 'success', data } });
    if (pathname === '/api/auth') return ok({ username: 'admin', role: 'admin', html5: true });
    if (pathname === labApi) return ok({ id: 'canvas-interactions', name: 'Canvas interactions', path: labPath });
    if (pathname === `${labApi}/nodes/1/start`) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      nodes['1'].status = 2;
      return ok({});
    }
    if (pathname === `${labApi}/nodes`) return ok(nodes);
    if (pathname === `${labApi}/networks`) return ok(networks);
    if (pathname === `${labApi}/links`) return ok(links);
    if (pathname === `${labApi}/node-catalog`) return ok({ templates: [], paired_templates: [] });
    return ok([]);
  });
  await page.routeWebSocket('**/ws/labs/**', () => {});
  await page.goto(`${frontend}/labs${labPath}`);
  await expect(page.locator('.svelte-flow__node[data-id="node1"]')).toBeVisible();
}

test('ports reveal on hover and while a connection is being dragged', async ({ page }) => {
  await openLab(page);
  const first = page.locator('.svelte-flow__node[data-id="node1"]');
  const third = page.locator('.svelte-flow__node[data-id="node3"]');
  const source = first.locator('[data-port-new-connection="true"]');
  const target = third.locator('[data-port-new-connection="true"]');
  const network = page.locator('.svelte-flow__node[data-id="network9"]');
  const networkPort = network.locator('[data-network-port-open="true"]');
  await page.mouse.move(10, 10);
  await expect(source).toHaveCSS('opacity', '0');
  await expect(target).toHaveCSS('opacity', '0');
  await expect(networkPort).toHaveCSS('opacity', '0');
  await network.getByTestId('network-node').hover();
  await expect(networkPort).toHaveCSS('opacity', '1');
  await first.getByTestId('node-icon-container').hover();
  await expect(source).toHaveCSS('opacity', '1');
  await expect(target).toHaveCSS('opacity', '0');
  const box = await source.boundingBox();
  if (!box) throw new Error('The source port has no bounds');
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 25, box.y + box.height / 2 + 25, { steps: 4 });
  await expect(target).toHaveCSS('opacity', '1');
  await page.keyboard.press('Escape');
  await page.mouse.up();
});

test('focus is explicit, preserves neighbors, and can be exited', async ({ page }) => {
  await openLab(page);
  const first = page.locator('.svelte-flow__node[data-id="node1"]');
  const neighbor = page.locator('.svelte-flow__node[data-id="node2"]');
  const unrelated = page.locator('.svelte-flow__node[data-id="node3"]');
  await first.getByTestId('node-icon-container').click();
  await expect(page.getByTestId('exit-focus')).toHaveCount(0);
  await expect(unrelated).not.toHaveClass(/nv-focus-dimmed/);
  await first.getByTestId('node-icon-container').click({ button: 'right' });
  await page.getByTestId('node-menu-focus').click();
  await expect(page.getByTestId('exit-focus')).toBeVisible();
  await expect(first).not.toHaveClass(/nv-focus-dimmed/);
  await expect(neighbor).not.toHaveClass(/nv-focus-dimmed/);
  await expect(unrelated).toHaveClass(/nv-focus-dimmed/);
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('exit-focus')).toHaveCount(0);
  await expect(unrelated).not.toHaveClass(/nv-focus-dimmed/);
});


test('the in-card start control uses lifecycle handling and blocks duplicate requests', async ({ page }) => {
  let startRequests = 0;
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === `${labApi}/nodes/1/start`) startRequests += 1;
  });
  await openLab(page);
  const first = page.locator('.svelte-flow__node[data-id="node1"]');
  const control = first.getByTestId('node-lifecycle-button');
  await first.hover();
  await expect(control).toHaveAccessibleName('Start Node 1');
  await control.click();
  await expect(control).toBeDisabled();
  await expect(control).toHaveAccessibleName('Starting Node 1');
  await expect(control).toHaveAccessibleName('Stop Node 1', { timeout: 10000 });
  expect(startRequests).toBe(1);
});


test('node context menu opens its information panel', async ({ page }) => {
  await openLab(page);
  const first = page.locator('.svelte-flow__node[data-id="node1"]');
  await first.getByTestId('node-icon-container').click({ button: 'right' });
  await page.getByTestId('node-menu-info').click();
  await expect(page.getByTestId('info-panel')).toContainText('Node 1');
});

test.describe('touch input', () => {
  test.use({ hasTouch: true, isMobile: true });

  test('connection affordances remain available without hover', async ({ page }) => {
    await openLab(page);
    const source = page.locator('.svelte-flow__node[data-id="node1"] [data-port-new-connection="true"]');
    await page.mouse.move(10, 10);
    await expect(source).toHaveCSS('opacity', '1');
  });
});

test('network connection frame expands without moving nodes and collapses at low zoom', async ({ page }) => {
  let layoutWrites = 0;
  page.on('request', (request) => {
    if (request.method() !== 'GET' && new URL(request.url()).pathname === `${labApi}/layout`) layoutWrites += 1;
  });
  await openLab(page);
  const network = page.locator('.svelte-flow__node[data-id="network9"]');
  const toggle = network.getByTestId('network-frame-toggle');
  const rows = network.getByTestId('network-frame-rows');
  await expect(toggle).toBeVisible();
  await toggle.click();
  // fitView may start below the drill-in threshold on a small viewport.
  for (let i = 0; i < 8 && await rows.count() === 0; i++) {
    await page.getByRole('button', { name: 'zoom in' }).click();
    await page.waitForTimeout(150);
  }
  await expect(rows).toContainText('Node 1');
  await expect(rows).toContainText('Node 2');
  for (const id of ['link-1', 'link-2']) {
    const row = await rows.locator(`[data-link-id="${id}"]`).boundingBox();
    const handle = await network.locator(`[data-network-port-link-id="${id}"]`).boundingBox();
    if (!row || !handle) throw new Error(`Missing frame row or edge handle for ${id}`);
    expect(Math.abs((row.y + row.height / 2) - (handle.y + handle.height / 2))).toBeLessThan(3);
  }
  // Zoom changes can persist the viewport; frame toggles must not persist layout.
  await page.waitForTimeout(800);
  const baseline = layoutWrites;
  await toggle.click();
  await expect(rows).toHaveCount(0);
  await toggle.click();
  await expect(rows).toBeVisible();
  await page.waitForTimeout(500);
  expect(layoutWrites).toBe(baseline);
  for (let i = 0; i < 8 && await rows.count() > 0; i++) {
    await page.getByRole('button', { name: 'zoom out' }).click();
    await page.waitForTimeout(220);
  }
  await expect(rows).toHaveCount(0);
  await expect(toggle).toHaveAttribute('aria-label', 'Close network connections');
  for (let i = 0; i < 12 && await rows.count() === 0; i++) {
    await page.getByRole('button', { name: 'zoom in' }).click();
    await page.waitForTimeout(220);
  }
  await expect(rows).toBeVisible();

});
