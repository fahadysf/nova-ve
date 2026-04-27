// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  use: {
    baseURL: 'http://localhost:5173',
  },
  // No webServer block: the backend API test targets the backend directly
  // via BACKEND_URL env var (default http://127.0.0.1:8000). No browser
  // needed for the API-only path in lab_v1_rejection.spec.ts.
});
