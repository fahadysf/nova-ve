// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * US-105 — Node-type-aware interface naming.
 *
 * Pure helper that renders an interface name from a format string and a
 * 0-based interface index, using the same placeholder semantics as the
 * backend's ``render_interface_name``:
 *
 * - ``{n}``    — 0-based index  (e.g. ``eth{n}``    → ``eth0``)
 * - ``{slot}`` — alias for ``{n}``
 * - ``{port}`` — 1-based index  (e.g. ``Gi{port}``  → ``Gi1``)
 *
 * When ``scheme`` is null/undefined the caller should fall back to the
 * literal ``iface.name`` stored in the lab JSON.
 */
export function formatInterfaceName(scheme: string | null | undefined, index: number): string {
	if (!scheme) return '';
	return scheme
		.replace(/\{n\}/g, String(index))
		.replace(/\{slot\}/g, String(index))
		.replace(/\{port\}/g, String(index + 1));
}
