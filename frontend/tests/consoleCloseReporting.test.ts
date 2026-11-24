// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

// Lab editor page tests for console close reporting (#300).
//
// The server sees a console open but has no other way to learn that one
// closed: closing a tab only unloads an iframe. These tests drive the real
// lab page and check that every way a console can end is reported.

import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import LabEditorPage from '../src/routes/labs/[...id]/+page.svelte';
import { ApiError } from '$lib/api';
import { authStore } from '$lib/stores/session';
import { toastStore } from '$lib/stores/toasts';
import { externalConsoleStore } from '$lib/stores/externalConsole';
import { setPagePathname, setPageParams } from './mocks/appStores';

const apiRequest = vi.fn();

vi.mock('$lib/api', () => {
  class ApiError extends Error {
    statusCode: number;
    constructor(message: string, options?: { statusCode?: number }) {
      super(message);
      this.name = 'ApiError';
      this.statusCode = options?.statusCode ?? 0;
    }
  }
  return {
    ApiError,
    apiRequest: (...args: unknown[]) => apiRequest(...args),
    apiGetData: async (...args: unknown[]) => {
      const response = await (apiRequest as (...a: unknown[]) => unknown)(...args);
      return (response as { data: unknown }).data;
    },
  };
});

vi.mock('@xyflow/svelte', async () => {
  const SlotStub = (await import('./mocks/SlotStub.svelte')).default;
  return { SvelteFlowProvider: SlotStub };
});

vi.mock('../src/lib/components/canvas/TopologyCanvas.svelte', async () => {
  const Stub = (await import('./mocks/TopologyCanvasStub.svelte')).default;
  return { default: Stub };
});

const META = { id: 'sample.json', name: 'Sample Lab', path: 'sample.json' };
const NODES = {
  '1': { id: 1, name: 'router-1', type: 'qemu', status: 2, cpu: 1, ram: 512, console: 'telnet', interfaces: [] },
  '2': { id: 2, name: 'router-2', type: 'qemu', status: 2, cpu: 1, ram: 512, console: 'telnet', interfaces: [] }
} as const;
type NodeId = keyof typeof NODES;

type Payload = { code: number; status: string; message?: string; data?: unknown };

function labLoadPayloads(): Record<string, Payload> {
  return {
    '/labs/sample.json': { code: 200, status: 'success', data: META },
    '/labs/sample.json/nodes': { code: 200, status: 'success', data: NODES },
    '/labs/sample.json/networks?include_hidden=true': { code: 200, status: 'success', data: {} },
    '/labs/sample.json/topology': { code: 200, status: 'success', data: [] },
    '/labs/sample.json/links': { code: 200, status: 'success', data: [] }
  };
}

function plantNode(node: Record<string, unknown> | undefined) {
  (globalThis as unknown as { __externalConsoleTestNode?: unknown }).__externalConsoleTestNode = node;
}

// The page reports closes with a bare keepalive fetch — not apiRequest — so
// that is what these tests observe.
const fetchMock = vi.fn<(...args: unknown[]) => Promise<Response>>();

function closeReports() {
  return fetchMock.mock.calls
    .filter(([url]) => String(url).includes('/console/close'))
    .map(([url, init]) => ({ url: String(url), init }));
}

async function openConsole(nodeId: NodeId) {
  plantNode(NODES[nodeId]);
  await fireEvent.click(screen.getByTestId('canvas-console-button'));
  await screen.findByTitle(`Guacamole console for ${NODES[nodeId].name}`);
}

async function renderPage() {
  render(LabEditorPage);
  await waitFor(() => screen.getByTestId('canvas-console-button'));
}

describe('lab page reports console closes (#300)', () => {
  let clickSpy: ReturnType<typeof vi.spyOn> | null = null;

  beforeEach(() => {
    window.localStorage.clear();
    window.document.cookie = 'novaVeConsoleMode=tabbed; path=/';
    authStore.setUser({ username: 'admin', role: 'admin', email: '', name: '' } as never);
    setPagePathname('/labs/sample.json');
    setPageParams({ id: 'sample.json' });
    apiRequest.mockReset();
    apiRequest.mockImplementation(async (path: string) => {
      const payload = labLoadPayloads()[path] ?? { code: 200, status: 'success', data: {} };
      if (payload.code >= 400 || payload.status === 'fail') {
        throw new ApiError(payload.message || 'Request failed.', { statusCode: payload.code });
      }
      return payload;
    });
    fetchMock.mockReset();
    fetchMock.mockImplementation(async () => new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        void this.href;
      });
  });

  afterEach(() => {
    clickSpy?.mockRestore();
    clickSpy = null;
    vi.unstubAllGlobals();
    plantNode(undefined);
    externalConsoleStore.setMode('off');
  });

  it('reports nothing for opening a console, and once for closing its tab', async () => {
    await renderPage();
    await openConsole('1');
    expect(closeReports()).toEqual([]);

    await fireEvent.click(screen.getByRole('button', { name: 'Close router-1' }));

    await waitFor(() => expect(closeReports()).toHaveLength(1));
    expect(closeReports()).toEqual([
      {
        url: '/api/labs/sample.json/nodes/1/console/close?console=primary',
        // keepalive: it must still go out while the page is being unloaded.
        init: { method: 'POST', credentials: 'include', keepalive: true }
      }
    ]);
    expect(screen.queryByTitle('Guacamole console for router-1')).not.toBeInTheDocument();
  });

  it('closing one tab reports only that console', async () => {
    await renderPage();
    await openConsole('1');
    await openConsole('2');

    await fireEvent.click(screen.getByRole('button', { name: 'Close router-1' }));

    await waitFor(() => expect(closeReports()).toHaveLength(1));
    expect(closeReports()[0].url).toBe('/api/labs/sample.json/nodes/1/console/close?console=primary');
    expect(screen.getByTitle('Guacamole console for router-2')).toBeInTheDocument();
  });

  it('closing the whole workspace reports every open console', async () => {
    await renderPage();
    await openConsole('1');
    await openConsole('2');

    await fireEvent.click(screen.getByRole('button', { name: 'Close console workspace' }));

    await waitFor(() => expect(closeReports()).toHaveLength(2));
    expect(closeReports().map((report) => report.url).sort()).toEqual([
      '/api/labs/sample.json/nodes/1/console/close?console=primary',
      '/api/labs/sample.json/nodes/2/console/close?console=primary'
    ]);
  });

  it('reloading a console ends the old session and keeps tracking the new one', async () => {
    await renderPage();
    await openConsole('1');

    await fireEvent.click(screen.getByRole('button', { name: 'Reload console' }));

    // A reload starts a fresh session server-side, so the old one is over.
    await waitFor(() => expect(closeReports()).toHaveLength(1));
    expect(screen.getByTitle('Guacamole console for router-1')).toBeInTheDocument();

    await fireEvent.click(screen.getByRole('button', { name: 'Close router-1' }));

    await waitFor(() => expect(closeReports()).toHaveLength(2));
  });

  it('works the same for floating console windows', async () => {
    window.document.cookie = 'novaVeConsoleMode=floating; path=/';
    await renderPage();
    await openConsole('1');
    expect(closeReports()).toEqual([]);

    await fireEvent.click(screen.getByRole('button', { name: 'Close router-1 console' }));

    await waitFor(() => expect(closeReports()).toHaveLength(1));
    expect(closeReports()[0].url).toBe('/api/labs/sample.json/nodes/1/console/close?console=primary');
  });

  it('leaving the page reports the consoles still open, once each', async () => {
    const { unmount } = render(LabEditorPage);
    await waitFor(() => screen.getByTestId('canvas-console-button'));
    await openConsole('1');
    await openConsole('2');

    unmount();

    await waitFor(() => expect(closeReports()).toHaveLength(2));
    expect(closeReports().map((report) => report.url)).toEqual([
      '/api/labs/sample.json/nodes/1/console/close?console=primary',
      '/api/labs/sample.json/nodes/2/console/close?console=primary'
    ]);
  });

  it('unloading the page (tab closed, reloaded, site left) reports the consoles still open', async () => {
    await renderPage();
    await openConsole('1');
    await openConsole('2');
    expect(closeReports()).toEqual([]);

    // No click, no component teardown: the page is simply going away.
    window.dispatchEvent(new Event('pagehide'));

    expect(closeReports().map((report) => report.url)).toEqual([
      '/api/labs/sample.json/nodes/1/console/close?console=primary',
      '/api/labs/sample.json/nodes/2/console/close?console=primary'
    ]);
    // Every report carries what an unloading page needs to get it out.
    for (const { init } of closeReports()) {
      expect(init).toEqual({ method: 'POST', credentials: 'include', keepalive: true });
    }
  });

  it('an external console opens no tab, so there is nothing to report', async () => {
    externalConsoleStore.setMode('on');
    apiRequest.mockImplementation(async (path: string) => {
      if (path === '/api/labs/sample.json/nodes/1/external-console') {
        return {
          code: 200,
          status: 'success',
          data: { mode: 'primary', scheme: 'telnet', host: '192.168.7.9', port: 47501, console: 'telnet' }
        };
      }
      return labLoadPayloads()[path] ?? { code: 200, status: 'success', data: {} };
    });
    await renderPage();
    plantNode(NODES['1']);
    await fireEvent.click(screen.getByTestId('canvas-console-button'));
    await waitFor(() =>
      expect(apiRequest.mock.calls.some(([path]) => String(path).endsWith('/external-console'))).toBe(true)
    );

    expect(document.querySelector('iframe')).toBeNull();
    window.dispatchEvent(new Event('pagehide'));
    expect(closeReports()).toEqual([]);
  });

  it('a report that cannot be sent is dropped quietly', async () => {
    const messages: string[] = [];
    const unsubscribe = toastStore.subscribe((items) => {
      messages.splice(0, messages.length, ...items.map((item) => item.message));
    });
    fetchMock.mockImplementation(async () => {
      throw new TypeError('network down');
    });
    await renderPage();
    await openConsole('1');

    await fireEvent.click(screen.getByRole('button', { name: 'Close router-1' }));

    await waitFor(() => expect(closeReports()).toHaveLength(1));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(messages).toEqual([]);
    expect(screen.queryByTitle('Guacamole console for router-1')).not.toBeInTheDocument();
    unsubscribe();
  });
});
