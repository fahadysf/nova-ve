import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import path from 'path';

export default defineConfig({
	plugins: [svelte({ hot: false })],
	resolve: {
		alias: {
			$lib: path.resolve('./src/lib'),
			// SvelteKit virtual modules are not resolvable under the plain
			// svelte plugin vitest uses — point them at test doubles.
			'$app/navigation': path.resolve('./tests/mocks/appNavigation.ts'),
			'$app/stores': path.resolve('./tests/mocks/appStores.ts'),
			'$app/environment': path.resolve('./tests/mocks/appEnvironment.ts'),
		},
		// Resolve the browser entry of Svelte/SvelteKit so component mount() is available
		// inside the JSDOM environment (Svelte 5 + @testing-library/svelte component tests).
		conditions: ['browser'],
	},
	test: {
		environment: 'jsdom',
		globals: true,
		setupFiles: ['./tests/setup.ts'],
		include: [
			'tests/**/*.{test,spec}.ts',
			'tests/e2e/bench/**/*.spec.ts',
			'src/**/*.test.ts',
		],
		// Exclude Playwright e2e tests but keep router bench specs runnable in vitest.
		exclude: ['tests/e2e/!(bench)/**', 'tests/e2e/*.spec.ts', 'node_modules/**'],
	},
});
