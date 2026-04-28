// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * US-105 — formatInterfaceName table-driven tests.
 */

import { describe, expect, it } from 'vitest';
import { formatInterfaceName } from '$lib/services/interfaceNaming';

describe('formatInterfaceName', () => {
	const cases: [string | null | undefined, number, string][] = [
		// {n} — 0-based
		['eth{n}', 0, 'eth0'],
		['eth{n}', 3, 'eth3'],
		// {slot} — alias for {n}
		['Gi0/{slot}', 0, 'Gi0/0'],
		['Gi0/{slot}', 2, 'Gi0/2'],
		// {port} — 1-based
		['Gi{port}', 0, 'Gi1'],
		['Gi{port}', 3, 'Gi4'],
		// juniper-style
		['xe-0/0/{n}', 0, 'xe-0/0/0'],
		['xe-0/0/{n}', 5, 'xe-0/0/5'],
		// mikrotik-style
		['eth-{port}', 0, 'eth-1'],
		['eth-{port}', 2, 'eth-3'],
		// cisco iosv l2
		['Gi0/{n}', 1, 'Gi0/1'],
		// null scheme returns empty string (caller should fall back to iface.name)
		[null, 0, ''],
		[undefined, 0, ''],
		// empty string returns empty string
		['', 2, ''],
	];

	it.each(cases)('formatInterfaceName(%s, %d) → %s', (scheme, index, expected) => {
		expect(formatInterfaceName(scheme, index)).toBe(expected);
	});

	it('replaces all occurrences of {n} in the format string', () => {
		expect(formatInterfaceName('{n}-{n}', 2)).toBe('2-2');
	});

	it('replaces all occurrences of {port} in the format string', () => {
		expect(formatInterfaceName('{port}/{port}', 0)).toBe('1/1');
	});
});
