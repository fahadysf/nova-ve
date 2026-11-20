// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

// Test double for SvelteKit's $app/navigation under vitest (the virtual
// module is unresolvable without the full kit compiler).
export const goto = (url: string): void => {
  if (typeof window !== 'undefined') window.location.assign(url);
};
export const invalidateAll = (): void => {};
export const prefetch = (): Promise<void> => Promise.resolve();
