// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * US-069 — drag-to-connect confirmation modal e2e
 *
 * Drives the canvas, drags from one node's port to another node's port,
 * asserts the LinkConfirmModal opens, clicks Confirm, and verifies that a
 * POST /api/labs/{lab}/links call fires with the right body and an
 * Idempotency-Key header.
 *
 * A second test verifies ESC during the drag cancels with no POST.
 */

import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BACKEND_URL = process.env.BACKEND_URL ?? 'http://127.0.0.1:8000';
const FRONTEND_URL = process.env.FRONTEND_URL ?? 'http://127.0.0.1:5173';

const V2_LAB_FIXTURE = JSON.stringify({
  schema: 2,
  id: '22222222-3333-4444-5555-666666666666',
  meta: {
    id: '22222222-3333-4444-5555-666666666666',
    name: 'Drag Connect Lab',
    filename: 'e2e-drag-connect.json',
    path: '/e2e-drag-connect.json',
    owner: 'admin',
    author: 'wave2 e2e',
    description: 'US-069 fixture',
    version: '2',
    grid: true,
    lock: false,
  },
  viewport: { x: 0, y: 0, zoom: 1 },
  defaults: { link_style: 'orthogonal' },
  nodes: {
    '1': {
      id: 1,
      name: 'src',
      type: 'qemu',
      template: 'vyos',
      image: 'vyos-1.4',
      console: 'telnet',
      status: 0,
      delay: 0,
      cpu: 1,
      ram: 1024,
      ethernet: 2,
      left: 100,
      top: 200,
      icon: 'Router.png',
      width: '0',
      interfaces: [{ index: 0, name: 'eth0' }],
    },
    '2': {
      id: 2,
      name: 'dst',
      type: 'qemu',
      template: 'vyos',
      image: 'vyos-1.4',
      console: 'telnet',
      status: 0,
      delay: 0,
      cpu: 1,
      ram: 1024,
      ethernet: 2,
      left: 500,
      top: 200,
      icon: 'Router.png',
      width: '0',
      interfaces: [{ index: 0, name: 'eth0' }],
    },
  },
  networks: {
    '5': {
      id: 5,
      name: 'bridge0',
      type: 'linux_bridge',
      left: 300,
      top: 350,
      icon: 'lan.png',
      width: 0,
      style: 'Solid',
      linkstyle: 'Straight',
      color: '',
      label: '',
      visibility: true,
      implicit: false,
      smart: 0,
      config: {},
    },
  },
  links: [],
});

function resolveLabsDir(): string {
  if (process.env.NOVA_VE_LABS_DIR) return process.env.NOVA_VE_LABS_DIR;
  const repoRoot = path.resolve(__dirname, '..', '..', '..', '..');
  return path.join(repoRoot, 'backend', 'labs');
}

async function setupOrSkip(page: import('@playwright/test').Page, request: import('@playwright/test').APIRequestContext): Promise<{ fixturePath: string } | null> {
  let backendAlive = false;
  try {
    const probe = await request.get(`${BACKEND_URL}/api/auth/login`, { timeout: 3000 });
    backendAlive = probe.status() !== 0;
  } catch {
    backendAlive = false;
  }
  if (!backendAlive) return null;

  let frontendAlive = false;
  try {
    const probe = await request.get(FRONTEND_URL, { timeout: 3000 });
    frontendAlive = probe.status() !== 0;
  } catch {
    frontendAlive = false;
  }
  if (!frontendAlive) return null;

  const labsDir = resolveLabsDir();
  const fixtureName = 'e2e-drag-connect.json';
  const fixturePath = path.join(labsDir, fixtureName);
  try {
    fs.mkdirSync(labsDir, { recursive: true });
    fs.writeFileSync(fixturePath, V2_LAB_FIXTURE, 'utf8');
  } catch {
    return null;
  }
  return { fixturePath };
}

test.describe('US-069 — drag-to-connect', () => {
  test('confirm flow POSTs /links with Idempotency-Key', async ({ page, request }) => {
    const setup = await setupOrSkip(page, request);
    if (!setup) {
      test.skip();
      return;
    }

    const captured: { headers: Record<string, string>; body: string }[] = [];
    try {
      await page.route(/\/api\/labs\/.*\/links$/, async (route) => {
        if (route.request().method() === 'POST') {
          captured.push({
            headers: route.request().headers(),
            body: route.request().postData() ?? '',
          });
        }
        await route.continue();
      });

      await page.goto(`${FRONTEND_URL}/labs/e2e-drag-connect.json`);
      await page.waitForSelector('[data-port-node-id="1"]', { timeout: 10000 });

      const sourcePort = page.locator('[data-port-node-id="1"][data-port-interface-index="0"]').first();
      const targetPort = page.locator('[data-port-node-id="2"][data-port-interface-index="0"]').first();
      const sourceBox = await sourcePort.boundingBox();
      const targetBox = await targetPort.boundingBox();
      expect(sourceBox).not.toBeNull();
      expect(targetBox).not.toBeNull();

      await page.mouse.move((sourceBox!.x + sourceBox!.width / 2), (sourceBox!.y + sourceBox!.height / 2));
      await page.mouse.down();
      // intermediate move so the dragLink store transitions to "dragging"
      await page.mouse.move(((sourceBox!.x + targetBox!.x) / 2), ((sourceBox!.y + targetBox!.y) / 2), { steps: 5 });
      // hover the target port and release on it
      await targetPort.hover();
      await page.mouse.up();

      const modal = page.locator('[data-testid="link-confirm-modal"]');
      await modal.waitFor({ state: 'visible', timeout: 5000 });

      await page.locator('[data-testid="link-confirm-button"]').click();
      await page.waitForTimeout(500);

      expect(captured.length).toBeGreaterThanOrEqual(1);
      const firstPost = captured[0];
      expect(firstPost.headers['idempotency-key']).toBeTruthy();
      const body = JSON.parse(firstPost.body) as { from?: { node_id?: number }; to?: { node_id?: number } };
      expect(body.from?.node_id).toBe(1);
      expect(body.to?.node_id).toBe(2);
    } finally {
      try {
        fs.unlinkSync(setup.fixturePath);
      } catch {
        /* best-effort */
      }
    }
  });

  test('ESC during drag cancels with no POST', async ({ page, request }) => {
    const setup = await setupOrSkip(page, request);
    if (!setup) {
      test.skip();
      return;
    }

    const posts: string[] = [];
    try {
      await page.route(/\/api\/labs\/.*\/links$/, async (route) => {
        if (route.request().method() === 'POST') {
          posts.push(route.request().postData() ?? '');
        }
        await route.continue();
      });

      await page.goto(`${FRONTEND_URL}/labs/e2e-drag-connect.json`);
      await page.waitForSelector('[data-port-node-id="1"]', { timeout: 10000 });

      const sourcePort = page.locator('[data-port-node-id="1"][data-port-interface-index="0"]').first();
      const sourceBox = await sourcePort.boundingBox();
      expect(sourceBox).not.toBeNull();

      await page.mouse.move(sourceBox!.x + sourceBox!.width / 2, sourceBox!.y + sourceBox!.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox!.x + 100, sourceBox!.y + 50, { steps: 3 });
      await page.keyboard.press('Escape');
      await page.mouse.up();
      await page.waitForTimeout(300);

      expect(posts).toHaveLength(0);
    } finally {
      try {
        fs.unlinkSync(setup.fixturePath);
      } catch {
        /* best-effort */
      }
    }
  });

  // US-081: reverse drag — drag from a network port to a node port and confirm
  // the POST body uses node-side ``from`` + network-side ``to`` regardless of
  // the drag direction.
  test('reverse flow: network → interface POSTs {from:{node_id,...}, to:{network_id}}', async ({ page, request }) => {
    const setup = await setupOrSkip(page, request);
    if (!setup) {
      test.skip();
      return;
    }

    const captured: { headers: Record<string, string>; body: string }[] = [];
    try {
      await page.route(/\/api\/labs\/.*\/links$/, async (route) => {
        if (route.request().method() === 'POST') {
          captured.push({
            headers: route.request().headers(),
            body: route.request().postData() ?? '',
          });
        }
        await route.continue();
      });

      await page.goto(`${FRONTEND_URL}/labs/e2e-drag-connect.json`);
      await page.waitForSelector('[data-network-port-id="5"]', { timeout: 10000 });

      // Pick the bottom side of the network bridge as the drag origin so the
      // pointer travels up toward the node ports placed at top of the canvas.
      const networkPort = page
        .locator('[data-network-port-id="5"][data-network-port-side="bottom"]')
        .first();
      const interfacePort = page
        .locator('[data-port-node-id="1"][data-port-interface-index="0"]')
        .first();

      const networkBox = await networkPort.boundingBox();
      const ifaceBox = await interfacePort.boundingBox();
      expect(networkBox).not.toBeNull();
      expect(ifaceBox).not.toBeNull();

      await page.mouse.move(
        networkBox!.x + networkBox!.width / 2,
        networkBox!.y + networkBox!.height / 2
      );
      await page.mouse.down();
      await page.mouse.move(
        (networkBox!.x + ifaceBox!.x) / 2,
        (networkBox!.y + ifaceBox!.y) / 2,
        { steps: 5 }
      );
      await interfacePort.hover();
      await page.mouse.up();

      const modal = page.locator('[data-testid="link-confirm-modal"]');
      await modal.waitFor({ state: 'visible', timeout: 5000 });

      await page.locator('[data-testid="link-confirm-button"]').click();
      await page.waitForTimeout(500);

      expect(captured.length).toBeGreaterThanOrEqual(1);
      const post = captured[0];
      expect(post.headers['idempotency-key']).toBeTruthy();
      const body = JSON.parse(post.body) as {
        from?: { node_id?: number; interface_index?: number; network_id?: number };
        to?: { node_id?: number; interface_index?: number; network_id?: number };
      };
      // The wire contract requires the node-side endpoint as ``from`` so the
      // server can resolve the interface index regardless of which way the
      // user dragged.
      expect(body.from?.node_id).toBe(1);
      expect(body.from?.interface_index).toBe(0);
      expect(body.to?.network_id).toBe(5);
    } finally {
      try {
        fs.unlinkSync(setup.fixturePath);
      } catch {
        /* best-effort */
      }
    }
  });
});
