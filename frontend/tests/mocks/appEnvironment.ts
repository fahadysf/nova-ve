// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

// Test double for SvelteKit's $app/environment under vitest.
export const browser = typeof window !== 'undefined';
export const dev = false;
export const prerendering = false;
export const version = 'test';
