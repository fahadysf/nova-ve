// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ActivityLog from '../../src/lib/components/system/ActivityLog.svelte';
import {
  ACTIVITY_EXPORT_CAP,
  ACTIVITY_MAX_PAGE_SIZE,
  ACTIVITY_PAGE_SIZE,
  activityPredicate,
  type ActivityEntry
} from '../../src/lib/services/activityLog';

const apiRequest = vi.fn();

vi.mock('$lib/api', () => ({
  apiRequest: (...args: unknown[]) => apiRequest(...args)
}));

const T0 = 1_700_000_000;

function trailEntry(overrides: Partial<ActivityEntry>): ActivityEntry {
  return {
    id: 1,
    time: T0,
    actor: 'alice',
    event: 'node.start',
    category: 'node',
    action: 'start',
    outcome: 'success',
    target_type: 'node',
    target_id: '3',
    target_name: 'R1',
    lab_path: '/lab.json',
    lab_name: 'Lab',
    ip: '203.0.113.9',
    detail: { node_type: 'qemu' },
    ...overrides
  };
}

const TRAIL: ActivityEntry[] = [
  trailEntry({ id: 41, time: T0 + 90 }),
  trailEntry({
    id: 40,
    time: T0 + 60,
    actor: null,
    event: 'auth.login_failed',
    category: 'auth',
    action: 'login_failed',
    outcome: 'failure',
    target_type: 'user',
    target_id: 'mallory',
    target_name: null,
    lab_path: null,
    lab_name: null,
    ip: '198.51.100.7',
    detail: null
  }),
  trailEntry({
    id: 39,
    time: T0 + 30,
    actor: 'bob',
    event: 'link.create',
    category: 'link',
    action: 'create',
    target_type: 'link',
    target_id: 'lnk_002',
    target_name: 'R2:eth1 ↔ Lab LAN',
    ip: '203.0.113.10',
    detail: { from: 'R2:eth1', to: 'Lab LAN' }
  })
];

/** ``count`` node events, newest first. */
function many(count: number, actor = 'alice'): ActivityEntry[] {
  return Array.from({ length: count }, (_, index) =>
    trailEntry({ id: 1000 - index, time: T0 + (count - index), actor, target_id: String(index + 1), target_name: `R${index + 1}` })
  );
}

/** The server's contract: filter, count, then slice the requested page. */
function page(url: string, entries: ActivityEntry[], actors: string[]) {
  const params = new URLSearchParams(url.slice(url.indexOf('?') + 1));
  const limit = Number(params.get('limit'));
  const offset = Number(params.get('offset'));
  const q = (params.get('q') ?? '').toLowerCase();
  const category = params.get('category') ?? '';
  const action = params.get('action') ?? '';
  const actor = params.get('actor') ?? '';
  const matches = entries.filter(
    (entry) =>
      (!q || `${entry.actor ?? ''} ${activityPredicate(entry)}`.toLowerCase().includes(q)) &&
      (!category || entry.category === category) &&
      (!action || entry.action === action) &&
      (!actor || entry.actor === actor)
  );
  return {
    code: 200,
    status: 'success',
    message: '',
    data: { entries: matches.slice(offset, offset + limit), total: matches.length, limit, offset, actors }
  };
}

function serve(entries: ActivityEntry[], actors = [...new Set(entries.map((entry) => entry.actor).filter((a): a is string => !!a))]) {
  apiRequest.mockImplementation(async (url: string) => page(url, entries, actors));
}

function requests(): URLSearchParams[] {
  return apiRequest.mock.calls.map(([url]) => new URLSearchParams(url.slice(url.indexOf('?') + 1)));
}

function lastRequest(): URLSearchParams {
  return requests()[requests().length - 1];
}

async function rendered() {
  render(ActivityLog);
  await waitFor(() => expect(screen.getByTestId('activity-range')).not.toHaveTextContent('\u00a0'));
}

const rowIds = () => screen.queryAllByTestId(/^activity-row-\d+$/).map((row) => row.dataset.testid);

function readBlob(blob: Blob): Promise<{ text: string; leadingBytes: number[] }> {
  const read = (mode: 'readAsText' | 'readAsArrayBuffer') =>
    new Promise<string | ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as string | ArrayBuffer);
      reader.onerror = () => reject(reader.error);
      reader[mode](blob);
    });
  return Promise.all([read('readAsText'), read('readAsArrayBuffer')]).then(([text, bytes]) => ({
    text: String(text),
    leadingBytes: Array.from(new Uint8Array(bytes as ArrayBuffer).slice(0, 3))
  }));
}

/** jsdom cannot download: capture the blob, the filename, and nothing navigates. */
function stubDownloads(): { blobs: Blob[]; downloads: string[] } {
  const captured = { blobs: [] as Blob[], downloads: [] as string[] };
  Object.defineProperties(URL, {
    createObjectURL: {
      configurable: true,
      value: (blob: Blob) => {
        captured.blobs.push(blob);
        return 'blob:activity';
      }
    },
    revokeObjectURL: { configurable: true, value: () => undefined }
  });
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    captured.downloads.push(this.download);
  });
  return captured;
}

describe('Activity log view (#297)', () => {
  beforeEach(() => {
    apiRequest.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    delete (URL as { createObjectURL?: unknown }).createObjectURL;
    delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL;
  });

  it('lists the trail newest first: who did what, from where', async () => {
    serve(TRAIL);
    await rendered();

    expect(apiRequest).toHaveBeenCalledTimes(1);
    expect(apiRequest.mock.calls[0][0]).toMatch(/^\/system\/logs\/activity\?/);
    expect(lastRequest().get('limit')).toBe(String(ACTIVITY_PAGE_SIZE));
    expect(lastRequest().get('offset')).toBe('0');

    expect(rowIds()).toEqual(['activity-row-41', 'activity-row-40', 'activity-row-39']);
    expect(screen.getByTestId('activity-actor-41')).toHaveTextContent('alice');
    expect(screen.getByTestId('activity-text-41')).toHaveTextContent('started node R1 in lab Lab');
    expect(screen.getByTestId('activity-row-41')).toHaveTextContent('203.0.113.9');

    // A failed sign-in by someone who was never authenticated.
    expect(screen.getByTestId('activity-row-40')).toHaveAttribute('data-outcome', 'failure');
    expect(screen.getByTestId('activity-actor-40')).toHaveTextContent('unknown user');
    expect(screen.getByTestId('activity-text-40')).toHaveTextContent(
      'failed to sign in as mallory from 198.51.100.7'
    );
    expect(screen.getByTestId('activity-text-39')).toHaveTextContent('connected R2:eth1 ↔ Lab LAN in lab Lab');

    expect(screen.getByTestId('activity-range')).toHaveTextContent('Showing 1–3 of 3');
    expect(screen.getByTestId('activity-newer')).toBeDisabled();
    expect(screen.getByTestId('activity-older')).toBeDisabled();
  });

  it('offers the users seen in the trail as a filter', async () => {
    serve(TRAIL);
    await rendered();

    const options = Array.from(screen.getByTestId('activity-actor').querySelectorAll('option')).map(
      (option) => option.textContent
    );
    expect(options).toEqual(['All users', 'alice', 'bob']);
  });

  it('searches once the typing settles, not on every keystroke', async () => {
    serve(TRAIL);
    await rendered();
    apiRequest.mockClear();

    const search = screen.getByTestId('activity-search');
    await fireEvent.input(search, { target: { value: 'mallory' } });
    await fireEvent.input(search, { target: { value: 'mallory ' } });
    expect(apiRequest).not.toHaveBeenCalled();

    await waitFor(() => expect(apiRequest).toHaveBeenCalledTimes(1));
    expect(lastRequest().get('q')).toBe('mallory');

    await waitFor(() => expect(rowIds()).toEqual(['activity-row-40']));
  });

  it('applies a search as soon as Enter is pressed', async () => {
    serve(TRAIL);
    await rendered();
    apiRequest.mockClear();

    await fireEvent.input(screen.getByTestId('activity-search'), { target: { value: 'R2' } });
    await fireEvent.keyDown(screen.getByTestId('activity-search'), { key: 'Enter' });

    await waitFor(() => expect(apiRequest).toHaveBeenCalledTimes(1));
    expect(lastRequest().get('q')).toBe('R2');
    await waitFor(() => expect(rowIds()).toEqual(['activity-row-39']));
  });

  it('sends the area, action, user and time filters to the server', async () => {
    serve(TRAIL);
    await rendered();
    const from = Math.floor(new Date('2026-11-01T08:30').getTime() / 1000);
    const to = Math.floor(new Date('2026-11-30T23:59').getTime() / 1000) + 59;

    await fireEvent.change(screen.getByTestId('activity-category'), { target: { value: 'node' } });
    await waitFor(() => expect(lastRequest().get('category')).toBe('node'));
    await fireEvent.change(screen.getByTestId('activity-action'), { target: { value: 'start' } });
    await waitFor(() => expect(lastRequest().get('action')).toBe('start'));
    await fireEvent.change(screen.getByTestId('activity-actor'), { target: { value: 'alice' } });
    await waitFor(() => expect(lastRequest().get('actor')).toBe('alice'));
    await fireEvent.change(screen.getByTestId('activity-from'), { target: { value: '2026-11-01T08:30' } });
    await waitFor(() => expect(lastRequest().get('from')).toBe(String(from)));
    await fireEvent.change(screen.getByTestId('activity-to'), { target: { value: '2026-11-30T23:59' } });
    await waitFor(() => expect(lastRequest().get('to')).toBe(String(to)));

    // Every change carries the whole filter set — a control never lags one
    // change behind — and starts again from the newest row.
    const expected = { category: 'node', action: 'start', actor: 'alice', from: String(from), to: String(to), offset: '0' };
    const last = lastRequest();
    for (const [key, value] of Object.entries(expected)) {
      expect(last.get(key), key).toBe(value);
    }
    expect(requests().every((params) => params.get('offset') === '0')).toBe(true);
    expect(rowIds()).toEqual(['activity-row-41']);
  });

  it('drops a filter set back to "everything"', async () => {
    serve(TRAIL);
    await rendered();

    await fireEvent.change(screen.getByTestId('activity-category'), { target: { value: 'auth' } });
    await waitFor(() => expect(rowIds()).toEqual(['activity-row-40']));
    await fireEvent.input(screen.getByTestId('activity-search'), { target: { value: 'nobody' } });
    await waitFor(() => expect(screen.getByTestId('activity-empty')).toBeInTheDocument());

    await fireEvent.click(screen.getByTestId('activity-reset'));

    await waitFor(() => expect(rowIds()).toHaveLength(3));
    for (const key of ['q', 'category', 'actor', 'from', 'to']) {
      expect(lastRequest().has(key), key).toBe(false);
    }
    expect((screen.getByTestId('activity-search') as HTMLInputElement).value).toBe('');
    expect((screen.getByTestId('activity-category') as HTMLSelectElement).value).toBe('');
  });

  it('pages through the trail a hundred rows at a time', async () => {
    serve(many(250));
    await rendered();

    const range = () => screen.getByTestId('activity-range');
    await waitFor(() => expect(range()).toHaveTextContent('Showing 1–100 of 250'));
    expect(screen.getByTestId('activity-newer')).toBeDisabled();
    expect(screen.getByTestId('activity-older')).toBeEnabled();

    await fireEvent.click(screen.getByTestId('activity-older'));
    await waitFor(() => expect(range()).toHaveTextContent('Showing 101–200 of 250'));
    expect(lastRequest().get('offset')).toBe('100');
    expect(rowIds()[0]).toBe('activity-row-900');

    await fireEvent.click(screen.getByTestId('activity-older'));
    await waitFor(() => expect(range()).toHaveTextContent('Showing 201–250 of 250'));
    expect(rowIds()).toHaveLength(50);
    expect(screen.getByTestId('activity-older')).toBeDisabled();

    await fireEvent.click(screen.getByTestId('activity-newer'));
    await waitFor(() => expect(range()).toHaveTextContent('Showing 101–200 of 250'));
    expect(lastRequest().get('offset')).toBe('100');
  });

  it('says so when the trail is empty', async () => {
    serve([]);
    await rendered();

    expect(rowIds()).toEqual([]);
    expect(screen.getByTestId('activity-empty')).toHaveTextContent('No activity has been recorded yet.');
  });

  it('distinguishes "nothing recorded" from "nothing matches"', async () => {
    serve(TRAIL);
    await rendered();

    await fireEvent.input(screen.getByTestId('activity-search'), { target: { value: 'zzz' } });
    await waitFor(() => expect(screen.getByTestId('activity-empty')).toBeInTheDocument());

    expect(screen.getByTestId('activity-empty')).toHaveTextContent('No activity matches these filters.');
  });

  it('refresh reloads the page being looked at, with the filters still applied', async () => {
    serve(many(250));
    await rendered();
    await fireEvent.click(screen.getByTestId('activity-older'));
    await waitFor(() => expect(screen.getByTestId('activity-range')).toHaveTextContent('Showing 101–200 of 250'));
    await fireEvent.change(screen.getByTestId('activity-actor'), { target: { value: 'alice' } });
    await waitFor(() => expect(lastRequest().get('actor')).toBe('alice'));
    await fireEvent.click(screen.getByTestId('activity-older'));
    await waitFor(() => expect(lastRequest().get('offset')).toBe('100'));
    const before = apiRequest.mock.calls.length;

    await fireEvent.click(screen.getByTestId('activity-refresh'));

    await waitFor(() => expect(apiRequest.mock.calls.length).toBe(before + 1));
    expect(lastRequest().get('offset')).toBe('100');
    expect(lastRequest().get('actor')).toBe('alice');
  });

  it('lets only the newest response paint the rows', async () => {
    let release: () => void = () => {};
    let first = true;
    apiRequest.mockImplementation(async (url: string) => {
      if (first) {
        first = false;
        return new Promise((resolve) => {
          // The first request is slow; a second one overtakes it.
          release = () => resolve(page(url, many(1, 'stale-user'), ['stale-user']));
        });
      }
      return page(url, many(1, 'fresh-user'), ['fresh-user']);
    });
    render(ActivityLog);
    await waitFor(() => expect(apiRequest).toHaveBeenCalledTimes(1));

    await fireEvent.click(screen.getByTestId('activity-refresh'));
    await waitFor(() => expect(screen.getByTestId('activity-actor-1000')).toHaveTextContent('fresh-user'));

    release();
    await new Promise((resolve) => setTimeout(resolve, 30));

    expect(screen.getByTestId('activity-actor-1000')).toHaveTextContent('fresh-user');
    expect(screen.getByTestId('activity-range')).toHaveTextContent('Showing 1–1 of 1');
  });

  it('exports every matching row as one CSV download', async () => {
    serve(many(1500));
    await rendered();
    const { blobs, downloads } = stubDownloads();

    await fireEvent.click(screen.getByTestId('activity-export'));

    await waitFor(() => expect(downloads).toHaveLength(1));
    // The server pages at its maximum, until the total is covered.
    const exported = requests().filter((params) => params.get('limit') === String(ACTIVITY_MAX_PAGE_SIZE));
    expect(exported.map((params) => params.get('offset'))).toEqual(['0', '1000']);
    expect(downloads[0]).toMatch(/^nova-ve-activity-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.csv$/);
    expect(blobs[0].type).toBe('text/csv;charset=utf-8');
    // A UTF-8 byte-order mark (EF BB BF), so spreadsheets decode names
    // correctly; a text reader strips it, so the check looks at the bytes.
    const { text, leadingBytes } = await readBlob(blobs[0]);
    expect(leadingBytes).toEqual([0xef, 0xbb, 0xbf]);
    expect(text.split('\r\n')).toHaveLength(1501); // header + one row per entry
    expect(text).toContain('alice started node R1 in lab Lab');

    await waitFor(() => expect(screen.getByTestId('activity-export')).toBeEnabled());
  });

  it('keeps an export to a bounded number of rows and says so', async () => {
    serve(many(ACTIVITY_EXPORT_CAP + 5000));
    await rendered();
    stubDownloads();
    const { toastStore } = await import('../../src/lib/stores/toasts');
    const pushed: string[] = [];
    const unsubscribe = toastStore.subscribe((items) => {
      for (const item of items) if (!pushed.includes(item.message)) pushed.push(item.message);
    });

    await fireEvent.click(screen.getByTestId('activity-export'));

    await waitFor(() =>
      expect(pushed.some((message) => /^Export covers the newest [\d,]+ matching entries\.$/.test(message))).toBe(true)
    );
    unsubscribe();
    const exported = requests().filter((params) => params.get('limit') === String(ACTIVITY_MAX_PAGE_SIZE));
    expect(exported).toHaveLength(ACTIVITY_EXPORT_CAP / ACTIVITY_MAX_PAGE_SIZE);
  });

  it('leaves the rows on screen when the request fails', async () => {
    serve(TRAIL);
    await rendered();
    apiRequest.mockRejectedValue(new Error('backend down'));

    await fireEvent.click(screen.getByTestId('activity-refresh'));
    await waitFor(() => expect(apiRequest).toHaveBeenCalled());

    expect(rowIds()).toHaveLength(3);
  });
});
