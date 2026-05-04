// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * US-105 / #179 — interface naming helpers.
 *
 * The scheme is a comma-separated list of names. Earlier items must be
 * fixed names; only the last item may carry a placeholder ({n}/{slot}/
 * {port}). The last item — when it carries a placeholder — covers all
 * remaining ports with relative numbering ({n}/{slot} 0-based, {port}
 * 1-based, both relative to the start of the trailing format).
 *
 * Single-string input (no commas) is the common case and behaves like
 * a one-element list.
 */

const PLACEHOLDER_RE = /\{(?:n|slot|port)\}/;

function splitItems(scheme: string): string[] {
	return scheme
		.split(',')
		.map((s) => s.trim())
		.filter((s) => s.length > 0);
}

function applyPlaceholders(fmt: string, relIndex: number): string {
	return fmt
		.replace(/\{n\}/g, String(relIndex))
		.replace(/\{slot\}/g, String(relIndex))
		.replace(/\{port\}/g, String(relIndex + 1));
}

export function formatInterfaceName(scheme: string | null | undefined, index: number): string {
	if (!scheme) return '';
	const items = splitItems(scheme);
	if (items.length === 0) return '';
	const fixedCount = items.length - 1;
	if (index < fixedCount) return items[index];
	const last = items[items.length - 1];
	const relIndex = index - fixedCount;
	return applyPlaceholders(last, relIndex);
}

/**
 * Validate a user-typed naming scheme. Returns null when valid, otherwise
 * a short error message suitable for inline display.
 *
 * Rules:
 * - Empty input is valid (means "use template/platform default").
 * - Each comma-separated item must be non-empty after trimming.
 * - Placeholders may appear only in the last item.
 * - The last item must contain at least one of {n}, {slot}, {port}
 *   — including when the input is a single bare value.
 */
export function validateInterfaceNamingScheme(scheme: string): string | null {
	const trimmed = scheme.trim();
	if (!trimmed) return null;

	const raw = scheme.split(',').map((s) => s.trim());
	if (raw.some((s) => s.length === 0)) {
		return 'Empty entry in list. Remove stray commas or fill in the name.';
	}

	for (let i = 0; i < raw.length - 1; i++) {
		if (PLACEHOLDER_RE.test(raw[i])) {
			return `"${raw[i]}" must be a fixed name; only the last entry may use {n}, {slot}, or {port}.`;
		}
	}

	const last = raw[raw.length - 1];
	if (!PLACEHOLDER_RE.test(last)) {
		return raw.length === 1
			? 'Must contain {n}, {slot}, or {port}.'
			: `Last entry "${last}" must contain {n}, {slot}, or {port}.`;
	}

	return null;
}
