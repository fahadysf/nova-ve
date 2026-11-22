// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { expect, test, type Page } from '@playwright/test';

const labPath = '/recording-fixtures/console.json';
const labName = 'Console test lab';
const entry = {
  id: '3--router-20261122-120000-session-1.cast',
  node_id: 3,
  node_name: 'Router 3',
  console: 'second',
  filename: 'router-20261122-120000-session-1.cast',
  title: 'Console test lab / Router 3',
  started_at: '2026-11-22T12:00:00Z',
  stopped_at: '2026-11-22T12:00:01Z',
  duration: 1,
  cast_size: 150,
  log_size: 50,
  in_progress: false
};
const recordingApi = `/api/labs${labPath}/recordings`;
const cast = [
  { version: 2, width: 80, height: 24, timestamp: 1795348800 },
  [0.1, 'o', 'hello recorded console\r\n'],
  [0.2, 'o', 'router# ']
].map((line) => JSON.stringify(line)).join('\n');

async function mockRecordingApi(page: Page, recordings = [entry]) {
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const ok = (data: unknown) => route.fulfill({ json: { code: 200, status: 'success', data } });
    if (url.pathname === '/api/auth') return ok({ username: 'admin', name: 'Admin', role: 'admin', html5: true });
    if (url.pathname === '/api/manage/nodes') {
      const nodes = url.searchParams.get('state') === 'running' ? [] : [{
        lab_path: labPath, lab_name: labName, node_id: 3, name: entry.node_name,
        type: 'qemu', state: 'stopped', console: 'telnet', url: null
      }];
      return ok({ nodes, totals: { nodes: nodes.length, running: 0, cpu_allocated: 0, ram_allocated_mb: 0 } });
    }
    if (url.pathname === recordingApi) {
      const nodeId = url.searchParams.get('node_id');
      return ok(nodeId ? recordings.filter((recording) => String(recording.node_id) === nodeId) : recordings);
    }
    if (url.pathname === `${recordingApi}/${entry.id}/cast`) {
      return route.fulfill({
        contentType: 'application/x-asciicast',
        headers: { 'Content-Disposition': `attachment; filename="${entry.filename}"` },
        body: cast
      });
    }
    if (url.pathname.startsWith(`${recordingApi}/`) && url.pathname.endsWith('/content')) {
      return ok({ text: 'hello recorded console\nrouter# show version\nrouter# show ip route', truncated: false });
    }
    const labApi = `/api/labs${labPath}`;
    if (url.pathname === labApi) return ok({ id: 'lab-id', name: labName, path: labPath });
    if (url.pathname === `${labApi}/nodes`) return ok({
      '3': {
        id: 3, name: entry.node_name, type: 'qemu', template: 'linux', image: 'linux',
        status: 0, console: 'telnet', cpu: 1, ram: 256, ethernet: 0, delay: 0,
        left: 120, top: 120, interfaces: []
      }
    });
    if (url.pathname === `${labApi}/networks`) return ok({});
    return ok([]);
  });
  await page.routeWebSocket('**/ws/labs/**', () => {});
}

test('lab toolbar filters recordings and a stopped topology node opens its own history', async ({ page }) => {
  await mockRecordingApi(page, [entry, {
    ...entry, id: '4--switch.cast', node_id: 4, node_name: 'Switch 4'
  }]);
  await page.goto(`/labs${labPath}`);

  await page.getByRole('button', { name: 'Lab recordings' }).click();
  const dialog = page.getByRole('dialog', { name: 'Session recordings' });
  await expect(dialog.getByTestId('recording-entry')).toHaveCount(2);
  await dialog.getByLabel('Filter by node').selectOption('4');
  await expect(dialog.getByTestId('recording-entry')).toHaveCount(1);
  await expect(dialog.getByTestId('recording-entry')).toContainText('Switch 4');
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);

  await page.locator('.svelte-flow__node[data-id="node3"]').getByTestId('node-icon-container').click({ button: 'right' });
  const filtered = page.waitForRequest((request) => request.url().endsWith(`${recordingApi}?node_id=3`));
  await page.getByTestId('node-menu-recordings').click();
  await filtered;
  await expect(dialog).toContainText(`${labName} · ${entry.node_name}`);
  await expect(dialog.getByTestId('recording-entry')).toHaveCount(1);
  await page.mouse.click(4, 4);
  await expect(dialog).toHaveCount(0);
});

test('Management Nodes opens a stopped node recording for playback, transcript search and downloads', async ({ page }) => {
  await mockRecordingApi(page);
  await page.goto('/manage/nodes');
  await page.getByTestId('nodes-state-filter').selectOption('stopped');
  const open = page.getByRole('button', { name: 'Recordings for Router 3' });
  await open.click();

  const dialog = page.getByRole('dialog', { name: 'Session recordings' });
  await expect(dialog).toContainText(`${labName} · ${entry.node_name}`);
  await dialog.getByTestId('recording-entry').click();
  await expect(dialog.getByTestId('download-cast')).toHaveAttribute('href', `${recordingApi}/${entry.id}/cast`);
  await expect(dialog.getByTestId('download-log')).toHaveAttribute('href', `${recordingApi}/${entry.id}/log`);
  await dialog.locator('.ap-play-button').click();
  await expect(dialog.locator('.ap-player')).toContainText('hello recorded console');
  await expect(dialog).toContainText('2nd console');
  const downloaded = page.waitForEvent('download');
  await dialog.getByTestId('download-cast').click();
  expect((await downloaded).suggestedFilename()).toBe(entry.filename);

  await dialog.getByRole('button', { name: 'Transcript' }).click();
  await expect(dialog.locator('.ap-player')).toHaveCount(0);
  await expect(dialog.locator('pre')).toContainText('show version');
  await dialog.getByRole('textbox', { name: 'Find in transcript' }).fill('show');
  await expect(dialog.getByTestId('match-count')).toHaveText('1/2');
  await dialog.getByRole('button', { name: 'Next match' }).click();
  await expect(dialog.getByTestId('match-count')).toHaveText('2/2');
  await dialog.getByRole('button', { name: 'Close recordings' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(open).toBeFocused();
});

test('recordings show individual file sizes and require confirmation before deleting', async ({ page }) => {
  const recordings = [
    { ...entry, cast_size: 1536, log_size: 1024 ** 2 },
    { ...entry, id: '3--live.cast', in_progress: true }
  ];
  await mockRecordingApi(page, recordings);
  let deleteRequests = 0;
  await page.route(`**${recordingApi}/${entry.id}`, async (route) => {
    if (route.request().method() !== 'DELETE') return route.fallback();
    deleteRequests += 1;
    if (deleteRequests === 1) {
      return route.fulfill({ json: { code: 409, status: 'fail', message: 'Stop the recording before deleting it.' } });
    }
    recordings.shift();
    return route.fulfill({ json: { code: 200, status: 'success', data: { id: entry.id } } });
  });

  await page.goto(`/labs${labPath}`);
  await page.getByRole('button', { name: 'Lab recordings' }).click();
  const dialog = page.getByRole('dialog', { name: 'Session recordings' });
  const entries = dialog.getByTestId('recording-entry');
  await expect(entries).toHaveCount(2);
  const remove = dialog.getByRole('button', { name: 'Delete recording' });

  await entries.nth(1).click();
  await expect(remove).toBeDisabled();
  await entries.first().click();
  await expect(dialog.getByTestId('recording-sizes')).toHaveText('Cast 1.5 KiB · Log 1.0 MiB');
  await expect(entries.first()).toContainText('1.0 MiB total');

  await remove.click();
  const confirmation = page.getByRole('dialog', { name: 'Delete recording?' });
  await expect(confirmation).toContainText('Are you sure?');
  const cancel = confirmation.getByRole('button', { name: 'Cancel' });
  await expect(cancel).toBeFocused();
  await cancel.click();
  await expect(confirmation).toHaveCount(0);
  await expect(entries).toHaveCount(2);
  expect(deleteRequests).toBe(0);

  await remove.click();
  const confirm = confirmation.getByRole('button', { name: 'Delete recording' });
  await confirm.click();
  await expect(confirmation.getByRole('alert')).toHaveText('Stop the recording before deleting it.');
  await confirm.click();
  await expect(confirmation).toHaveCount(0);
  await expect(dialog.getByRole('status')).toHaveText('Recording deleted from the host.');
  await expect(entries).toHaveCount(1);
  expect(deleteRequests).toBe(2);
});
