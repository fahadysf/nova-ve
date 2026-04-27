// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * US-070 — bulk PUT /layout debounce e2e
 *
 * Drag two nodes within the 500ms debounce window, idle 700ms, then assert
 * exactly ONE PUT /api/labs/{lab}/layout was issued whose body merges both
 * positions. Skips if the dev server / backend isn't reachable.
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
  id: '33333333-4444-5555-6666-777777777777',
  meta: {
    id: '33333333-4444-5555-6666-777777777777',
    name: 'Layout Debounce Lab',
    filename: 'e2e-layout-debounce.json',
    path: '/e2e-layout-debounce.json',
    owner: 'admin',
    author: 'wave2 e2e',
    description: 'US-070 fixture',
    version: '2',
    grid: true,
    lock: false,
  },
  viewport: { x: 0, y: 0, zoom: 1 },
  defaults: { link_style: 'orthogonal' },
  nodes: {
    '1': {
      id: 1, name: 'a', type: 'qemu', template: 'vyos', image: 'vyos-1.4',
      console: 'telnet', status: 0, delay: 0, cpu: 1, ram: 1024, ethernet: 1,
      left: 100, top: 100, icon: 'Router.png', width: '0',
      interfaces: [{ index: 0, name: 'eth0' }],
    },
    '2': {
      id: 2, name: 'b', type: 'qemu', template: 'vyos', image: 'vyos-1.4',
      console: 'telnet', status: 0, delay: 0, cpu: 1, ram: 1024, ethernet: 1,
      left: 400, top: 200, icon: 'Router.png', width: '0',
      interfaces: [{ index: 0, name: 'eth0' }],
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

test.describe('US-070 — bulk PUT /layout debounce', () => {
  test('two drags within 600ms produce a single PUT carrying both positions', async ({
    page,
    request,
  }) => {
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
    const fixtureName = 'e2e-layout-debounce.json';
    const fixturePath = path.join(labsDir, fixtureName);
    try {
      fs.mkdirSync(labsDir, { recursive: true });
      fs.writeFileSync(fixturePath, V2_LAB_FIXTURE, 'utf8');
    } catch {
      test.skip();
      return;
    }

    const layoutPuts: string[] = [];
    try {
      await page.route(/\/api\/labs\/.*\/layout$/, async (route) => {
        if (route.request().method() === 'PUT') {
          layoutPuts.push(route.request().postData() ?? '');
        }
        await route.continue();
      });

      await page.goto(`${FRONTEND_URL}/labs/${fixtureName}`);
      await page.waitForSelector('[data-port-node-id="1"]', { timeout: 10000 });

      const nodeA = page.locator('[data-port-node-id="1"]').first();
      const nodeB = page.locator('[data-port-node-id="2"]').first();
      const a = await nodeA.boundingBox();
      const b = await nodeB.boundingBox();
      expect(a).not.toBeNull();
      expect(b).not.toBeNull();

      // Drag node A
      await page.mouse.move(a!.x + a!.width / 2, a!.y - 10);
      await page.mouse.down();
      await page.mouse.move(a!.x + 50, a!.y + 50, { steps: 3 });
      await page.mouse.up();

      // Drag node B within the 500ms window
      await page.waitForTimeout(150);
      await page.mouse.move(b!.x + b!.width / 2, b!.y - 10);
      await page.mouse.down();
      await page.mouse.move(b!.x - 60, b!.y + 30, { steps: 3 });
      await page.mouse.up();

      // Idle past the debounce window
      await page.waitForTimeout(700);

      expect(layoutPuts).toHaveLength(1);
      const body = JSON.parse(layoutPuts[0]) as {
        nodes?: Array<{ id: number }>;
        viewport?: unknown;
      };
      const nodeIds = (body.nodes ?? []).map((n) => n.id).sort();
      expect(nodeIds).toEqual([1, 2]);
    } finally {
      try {
        fs.unlinkSync(fixturePath);
      } catch {
        /* best-effort */
      }
    }
  });
});
