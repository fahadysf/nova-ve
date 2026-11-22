/*
 * Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
 * SPDX-License-Identifier: Apache-2.0
 */

/** Search support for the session-recording viewer (#291). */

export interface SearchMatch {
  start: number;
  end: number;
}

export interface SearchResult {
  matches: SearchMatch[];
  error: string | null;
}

const MAX_MATCHES = 2000;

export function findMatches(text: string, query: string, regexMode: boolean): SearchResult {
  if (!query) return { matches: [], error: null };

  if (regexMode) {
    let re: RegExp;
    try {
      re = new RegExp(query, 'gi');
    } catch (error) {
      return { matches: [], error: error instanceof Error ? error.message : 'Invalid regular expression' };
    }
    const matches: SearchMatch[] = [];
    for (const match of text.matchAll(re)) {
      if (match[0].length === 0) continue;
      matches.push({ start: match.index, end: match.index + match[0].length });
      if (matches.length >= MAX_MATCHES) break;
    }
    return { matches, error: null };
  }

  const matches: SearchMatch[] = [];
  const haystack = text.toLowerCase();
  const needle = query.toLowerCase();
  let index = haystack.indexOf(needle);
  while (index !== -1 && matches.length < MAX_MATCHES) {
    matches.push({ start: index, end: index + needle.length });
    index = haystack.indexOf(needle, index + needle.length);
  }
  return { matches, error: null };
}

export interface TextSegment {
  text: string;
  matchIndex: number;
}

/** Split text into plain/matched segments for highlighted rendering. */
export function buildSegments(text: string, matches: SearchMatch[]): TextSegment[] {
  if (!matches.length) return [{ text, matchIndex: -1 }];
  const segments: TextSegment[] = [];
  let cursor = 0;
  matches.forEach((match, matchIndex) => {
    if (match.start > cursor) {
      segments.push({ text: text.slice(cursor, match.start), matchIndex: -1 });
    }
    segments.push({ text: text.slice(match.start, match.end), matchIndex });
    cursor = match.end;
  });
  if (cursor < text.length) {
    segments.push({ text: text.slice(cursor), matchIndex: -1 });
  }
  return segments;
}

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${String(secs).padStart(2, '0')}`;
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
