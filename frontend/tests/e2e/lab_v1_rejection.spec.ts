// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * US-062 — v1 lab.json rejection e2e
 *
 * Strategy: backend-API-only Playwright test (no browser required).
 *
 * There is no upload UI for existing lab.json files — labs are loaded from
 * disk by path. This test places a v1 lab fixture on disk, then exercises
 * the real HTTP path (GET /api/labs/{path}) and asserts the 422 envelope.
 *
 * The backend (US-058) always returns HTTP 200 with a JSON envelope where:
 *   { code: 422, status: "fail", message: "Lab uses legacy schema; run scripts/migrate_lab_v1_to_v2.py", lab_path: "..." }
 *
 * Acceptance criteria:
 *   1. Response JSON code == 422
 *   2. message contains "run scripts/migrate_lab_v1_to_v2.py"
 *   3. lab_path field is present in the response body
 *
 * Environment variables:
 *   BACKEND_URL          — backend origin (default: http://127.0.0.1:8000)
 *   NOVA_VE_LABS_DIR     — absolute path to backend labs dir (default: auto-discovered)
 *   NOVA_VE_ADMIN_USER   — admin username (default: admin)
 *   NOVA_VE_ADMIN_PASS   — admin password (default: admin)
 *
 * To run:
 *   BACKEND_URL=http://127.0.0.1:8000 npx playwright test lab_v1_rejection.spec.ts
 */

import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BACKEND_URL = process.env.BACKEND_URL ?? 'http://127.0.0.1:8000';
const REMEDIATION_HINT = 'run scripts/migrate_lab_v1_to_v2.py';

// v1 lab.json fixture — mirrors backend/tests/fixtures/lab_v1_legacy.json.
// Embedded so this test has no dependency on the backend source tree at runtime.
const V1_LAB_JSON = JSON.stringify({
  id: '8a73c8c9-7af0-4d2c-86a4-b8c2f1c5e612',
  meta: {
    name: 'Legacy Lab',
    author: 'Wave 0 fixture',
    description: 'v1 lab.json fixture — asserts loader rejects schema<2.',
    version: '1',
    scripttimeout: 300,
    countdown: 0,
    linkwidth: '1',
    grid: true,
    lock: false
  },
  nodes: {
    '1': {
      id: 1, name: 'R1', type: 'qemu', template: 'vyos', image: 'vyos-1.4',
      console: 'telnet', status: 0, delay: 0, cpu: 1, ram: 1024, ethernet: 4,
      cpulimit: 1, uuid: '11111111-1111-1111-1111-111111111111',
      firstmac: '50:00:00:01:00:00', left: 240, top: 180, icon: 'Router.png',
      width: '0', config: false, config_list: [], sat: 0, computed_sat: 0,
      interfaces: [
        { name: 'Gi0', network_id: 1 },
        { name: 'Gi1', network_id: 0 }
      ]
    }
  },
  networks: {
    '1': {
      id: 1, name: 'lan-core', type: 'bridge', left: 400, top: 180,
      icon: '01-Cloud-Default.svg', width: 0, style: 'Solid',
      linkstyle: 'Straight', color: '', label: '', visibility: 1, smart: -1, count: 1
    }
  },
  topology: [], textobjects: [], lineobjects: [], pictures: [], tasks: [], configsets: {}
});

/**
 * Discover the backend labs directory. Checks (in order):
 *   1. NOVA_VE_LABS_DIR env var
 *   2. Sibling path: <repo-root>/backend/labs (matches .env LABS_DIR=./labs)
 */
function resolveLabsDir(): string {
  if (process.env.NOVA_VE_LABS_DIR) {
    return process.env.NOVA_VE_LABS_DIR;
  }
  // __dirname = frontend/tests/e2e → repo-root is four levels up
  const repoRoot = path.resolve(__dirname, '..', '..', '..', '..');
  return path.join(repoRoot, 'backend', 'labs');
}

test.describe('US-062 — v1 lab.json rejection', () => {
  test('backend rejects v1 lab with code 422 and remediation hint', async ({ request }) => {
    // ── Step 1: verify the backend is reachable ──────────────────────────────
    let backendAlive = false;
    try {
      const probe = await request.get(`${BACKEND_URL}/api/auth/login`, { timeout: 3000 });
      // Any response (including 405 Method Not Allowed) means the server is up
      backendAlive = probe.status() !== 0;
    } catch {
      backendAlive = false;
    }
    if (!backendAlive) {
      test.skip();
      return;
    }

    // ── Step 2: authenticate ─────────────────────────────────────────────────
    const adminUser = process.env.NOVA_VE_ADMIN_USER ?? 'admin';
    const adminPass = process.env.NOVA_VE_ADMIN_PASS ?? 'admin';

    const loginResp = await request.post(`${BACKEND_URL}/api/auth/login`, {
      data: { username: adminUser, password: adminPass }
    });
    const loginBody = await loginResp.json() as { code: number; status: string };

    if (loginBody.code !== 200 || loginBody.status !== 'success') {
      // Backend is up but credentials are wrong — skip rather than fail.
      test.skip();
      return;
    }

    // ── Step 3: write v1 fixture to the backend labs directory ───────────────
    const labsDir = resolveLabsDir();
    const fixtureName = 'e2e-v1-legacy-test.json';
    const fixturePath = path.join(labsDir, fixtureName);

    try {
      fs.mkdirSync(labsDir, { recursive: true });
      fs.writeFileSync(fixturePath, V1_LAB_JSON, 'utf8');
    } catch {
      // Cannot write to labs dir from this runner — skip.
      test.skip();
      return;
    }

    try {
      // ── Step 4: GET /api/labs/{fixtureName} and assert 422 envelope ─────────
      const resp = await request.get(`${BACKEND_URL}/api/labs/${fixtureName}`);

      // HTTP envelope status is always 200; semantic status is in body.code.
      expect(resp.status()).toBe(200);

      const body = await resp.json() as {
        code: number;
        status: string;
        message: string;
        lab_path?: string;
      };

      // Acceptance criterion 1: JSON code field is 422.
      expect(body.code).toBe(422);

      // Acceptance criterion 2: remediation hint is present in message.
      expect(body.message).toContain(REMEDIATION_HINT);

      // Acceptance criterion 3: lab_path field is present in the response body.
      expect(body).toHaveProperty('lab_path');
      expect(typeof body.lab_path).toBe('string');
      expect((body.lab_path as string).length).toBeGreaterThan(0);
    } finally {
      // Clean up the fixture file regardless of test outcome.
      try { fs.unlinkSync(fixturePath); } catch { /* best-effort */ }
    }
  });
});
