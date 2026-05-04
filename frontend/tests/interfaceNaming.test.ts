// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
	formatInterfaceName,
	validateInterfaceNamingScheme,
} from '$lib/services/interfaceNaming';

describe('formatInterfaceName — single-string form (back-compat)', () => {
	const cases: [string | null | undefined, number, string][] = [
		['eth{n}', 0, 'eth0'],
		['eth{n}', 3, 'eth3'],
		['Gi0/{slot}', 0, 'Gi0/0'],
		['Gi0/{slot}', 2, 'Gi0/2'],
		['Gi{port}', 0, 'Gi1'],
		['Gi{port}', 3, 'Gi4'],
		['xe-0/0/{n}', 0, 'xe-0/0/0'],
		['xe-0/0/{n}', 5, 'xe-0/0/5'],
		['eth-{port}', 0, 'eth-1'],
		['eth-{port}', 2, 'eth-3'],
		['Gi0/{n}', 1, 'Gi0/1'],
		[null, 0, ''],
		[undefined, 0, ''],
		['', 2, ''],
	];

	it.each(cases)('formatInterfaceName(%s, %d) → %s', (scheme, index, expected) => {
		expect(formatInterfaceName(scheme, index)).toBe(expected);
	});

	it('replaces all occurrences of {n}', () => {
		expect(formatInterfaceName('{n}-{n}', 2)).toBe('2-2');
	});

	it('replaces all occurrences of {port}', () => {
		expect(formatInterfaceName('{port}/{port}', 0)).toBe('1/1');
	});
});

describe('formatInterfaceName — comma-separated list (#179)', () => {
	const cases: [string, number, string][] = [
		// fixed name + trailing format with relative {n}
		['mgmt0,eth{n}', 0, 'mgmt0'],
		['mgmt0,eth{n}', 1, 'eth0'],
		['mgmt0,eth{n}', 4, 'eth3'],
		// two fixed names + trailing format
		['mgmt0,mgmt1,eth{n}', 0, 'mgmt0'],
		['mgmt0,mgmt1,eth{n}', 1, 'mgmt1'],
		['mgmt0,mgmt1,eth{n}', 2, 'eth0'],
		// juniper-style fxp0 + ge-0/0/{n}
		['fxp0,ge-0/0/{n}', 0, 'fxp0'],
		['fxp0,ge-0/0/{n}', 1, 'ge-0/0/0'],
		['fxp0,ge-0/0/{n}', 5, 'ge-0/0/4'],
		// arista mgmt + ethernet{port} (1-based, relative)
		['Management1,Ethernet{port}', 0, 'Management1'],
		['Management1,Ethernet{port}', 1, 'Ethernet1'],
		['Management1,Ethernet{port}', 4, 'Ethernet4'],
		// whitespace tolerated around items
		[' mgmt0 , eth{n} ', 1, 'eth0'],
	];

	it.each(cases)('formatInterfaceName(%s, %d) → %s', (scheme, index, expected) => {
		expect(formatInterfaceName(scheme, index)).toBe(expected);
	});
});

describe('validateInterfaceNamingScheme', () => {
	it('accepts empty input (default)', () => {
		expect(validateInterfaceNamingScheme('')).toBeNull();
		expect(validateInterfaceNamingScheme('   ')).toBeNull();
	});

	it('accepts a single value with a placeholder', () => {
		expect(validateInterfaceNamingScheme('eth{n}')).toBeNull();
		expect(validateInterfaceNamingScheme('Gi{port}')).toBeNull();
		expect(validateInterfaceNamingScheme('xe-0/0/{slot}')).toBeNull();
	});

	it('rejects a single bare value with no placeholder', () => {
		expect(validateInterfaceNamingScheme('eth0')).toMatch(/contain \{n\}/);
		expect(validateInterfaceNamingScheme('mgmt0')).toMatch(/contain \{n\}/);
	});

	it('accepts a comma list with placeholder only on the last entry', () => {
		expect(validateInterfaceNamingScheme('mgmt0,eth{n}')).toBeNull();
		expect(validateInterfaceNamingScheme('mgmt0,mgmt1,eth{n}')).toBeNull();
		expect(validateInterfaceNamingScheme('fxp0,ge-0/0/{n}')).toBeNull();
		expect(validateInterfaceNamingScheme(' mgmt0 , eth{n} ')).toBeNull();
	});

	it('rejects placeholders in non-last entries', () => {
		expect(validateInterfaceNamingScheme('mgmt{n},Gi{port}')).toMatch(/fixed name/);
		expect(validateInterfaceNamingScheme('eth{n},mgmt0')).toMatch(/fixed name/);
		expect(validateInterfaceNamingScheme('a,b{slot},c{n}')).toMatch(/fixed name/);
	});

	it('rejects when last entry has no placeholder', () => {
		expect(validateInterfaceNamingScheme('mgmt0,mgmt1')).toMatch(/Last entry/);
		expect(validateInterfaceNamingScheme('a,b,c')).toMatch(/Last entry/);
	});

	it('rejects empty entries', () => {
		expect(validateInterfaceNamingScheme('mgmt0,,eth{n}')).toMatch(/Empty entry/);
		expect(validateInterfaceNamingScheme(',eth{n}')).toMatch(/Empty entry/);
		expect(validateInterfaceNamingScheme('mgmt0,')).toMatch(/Empty entry/);
	});
});
