/*
 * Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
 * SPDX-License-Identifier: Apache-2.0
 */

export function nodeIconSources(
  override: string | undefined,
  templateIcon: string | undefined,
  assets: ReadonlySet<string>
): string[] {
  const sources: string[] = [];
  for (const filename of new Set([override, templateIcon])) {
    if (filename && assets.has(filename)) {
      sources.push(`/api/list/icons/${encodeURIComponent(filename)}`);
    }
  }
  return sources;
}
