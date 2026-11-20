// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

// Test double for SvelteKit's $app/stores under vitest: a readable page
// store defaulting to "/" that tests can retarget per-suite.
import { readable } from 'svelte/store';

export interface MockPage {
  url: { pathname: string; search: string; origin: string };
  params: Record<string, string>;
  route: { id: string | null };
}

let currentPathname = '/';
let currentParams: Record<string, string> = {};

export function setPagePathname(pathname: string): void {
  currentPathname = pathname;
}

export function setPageParams(params: Record<string, string>): void {
  currentParams = params;
}

function makePage(): MockPage {
  return {
    url: { pathname: currentPathname, search: '', origin: 'http://test.local' },
    params: currentParams,
    route: { id: null },
  };
}

export const page = readable(makePage(), (set) => {
  set(makePage());
});
export const navigating = readable(null);
export const updated = readable({ subscribe: () => () => {} });
