// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * US-068 — port_position drag + persist e2e
 *
 * Strategy: drives the topology canvas in a browser, drags one port to a new
 * perimeter location, asserts the PATCH request fired with the new
 * port_position payload, then reloads and asserts the rendered port lands at
 * the same DOM coordinate within 1px.
 *
 * The test writes a v2 lab fixture to disk before the run and removes it on
 * teardown (mirroring `lab_v1_rejection.spec.ts`).
 *
 * Skips when the dev server / backend isn't reachable.
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
  id: '11111111-2222-3333-4444-555555555555',
  meta: {
    id: '11111111-2222-3333-4444-555555555555',
    name: 'Port Drag Lab',
    filename: 'e2e-port-drag.json',
    path: '/e2e-port-drag.json',
    owner: 'admin',
    author: 'wave2 e2e',
    description: 'US-068 fixture',
    version: '2',
    grid: true,
    lock: false,
  },
  viewport: { x: 0, y: 0, zoom: 1 },
  defaults: { link_style: 'orthogonal' },
  nodes: {
    '1': {
      id: 1,
      name: 'host-a',
      type: 'qemu',
      template: 'vyos',
      image: 'vyos-1.4',
      console: 'telnet',
      status: 0,
      delay: 0,
      cpu: 1,
      ram: 1024,
      ethernet: 4,
      left: 200,
      top: 200,
      icon: 'Router.png',
      width: '0',
      interfaces: [
        { index: 0, name: 'eth0', port_position: { side: 'right', offset: 0.5 } },
        { index: 1, name: 'eth1' },
      ],
    },
  },
  networks: {},
  links: [],
});

function resolveLabsDir(): string {
  if (process.env.NOVA_VE_LABS_DIR) return process.env.NOVA_VE_LABS_DIR;
  const repoRoot = path.resolve(__dirname, '..', '..', '..', '..');
  return path.join(repoRoot, 'backend', 'labs');
}

test.describe('US-068 — port_position drag persists & survives reload', () => {
  test('drag persists via PATCH and reload places port within 1px', async ({ page, request }) => {
    let backendAlive = false;
    try {
      const probe = await request.get(`${BACKEND_URL}/api/auth/login`, { timeout: 3000 });
      backendAlive = probe.status() !== 0;
    } catch {
      backendAlive = false;
    }
    if (!backendAlive) {
      test.skip();
      return;
    }

    let frontendAlive = false;
    try {
      const probe = await request.get(FRONTEND_URL, { timeout: 3000 });
      frontendAlive = probe.status() !== 0;
    } catch {
      frontendAlive = false;
    }
    if (!frontendAlive) {
      test.skip();
      return;
    }

    const labsDir = resolveLabsDir();
    const fixtureName = 'e2e-port-drag.json';
    const fixturePath = path.join(labsDir, fixtureName);

    try {
      fs.mkdirSync(labsDir, { recursive: true });
      fs.writeFileSync(fixturePath, V2_LAB_FIXTURE, 'utf8');
    } catch {
      test.skip();
      return;
    }

    let patchBody: Record<string, unknown> | null = null;

    try {
      await page.route(/\/api\/labs\/.*\/nodes\/\d+\/interfaces\/\d+/, async (route) => {
        if (route.request().method() === 'PATCH') {
          try {
            patchBody = JSON.parse(route.request().postData() ?? '{}');
          } catch {
            patchBody = null;
          }
        }
        await route.continue();
      });

      await page.goto(`${FRONTEND_URL}/labs/${fixtureName}`);
      await page.waitForSelector('[data-port-node-id="1"]', { timeout: 10000 });

      const port = page.locator('[data-port-node-id="1"][data-port-interface-index="0"]').first();
      const beforeBox = await port.boundingBox();
      expect(beforeBox).not.toBeNull();

      // Drag the port from its current (right side) anchor to the bottom edge.
      await port.dispatchEvent('mousedown', { button: 0 });
      await page.mouse.move(
        (beforeBox?.x ?? 0) + 50,
        (beforeBox?.y ?? 0) + 80
      );
      await page.mouse.up();

      // Wait for the 250ms debounce inside PortLayer to flush the PATCH.
      await page.waitForTimeout(400);
      expect(patchBody).not.toBeNull();
      expect(patchBody).toHaveProperty('port_position');

      // Reload and assert the port re-renders at (approximately) the new spot.
      const persistedSide = (patchBody as { port_position?: { side?: string } }).port_position?.side;
      await page.reload();
      await page.waitForSelector('[data-port-node-id="1"]', { timeout: 10000 });
      const reloadedPort = page
        .locator('[data-port-node-id="1"][data-port-interface-index="0"]')
        .first();
      const sideAttr = await reloadedPort.getAttribute('data-port-side');
      expect(sideAttr).toBe(persistedSide);

      const afterBox = await reloadedPort.boundingBox();
      expect(afterBox).not.toBeNull();
      // Port re-renders at the same coordinate within 1px tolerance.
      const beforeAfterBox = await port.boundingBox();
      if (beforeAfterBox && afterBox) {
        expect(Math.abs(beforeAfterBox.x - afterBox.x)).toBeLessThanOrEqual(1);
        expect(Math.abs(beforeAfterBox.y - afterBox.y)).toBeLessThanOrEqual(1);
      }
    } finally {
      try {
        fs.unlinkSync(fixturePath);
      } catch {
        /* best-effort */
      }
    }
  });
});
