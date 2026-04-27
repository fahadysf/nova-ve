import { toastStore } from '$lib/stores/toasts';

describe('smoke', () => {
	it('resolves $lib alias and exports toastStore', () => {
		expect(typeof toastStore).toBe('object');
		expect(typeof toastStore.push).toBe('function');
	});
});
