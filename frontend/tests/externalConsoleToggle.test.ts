// Copyright (c) 2026 Fahad Yousuf <fahadysuf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

// Lab editor page tests for the external-console toggle (#269) and Zen mode (#280).

import { render, screen, fireEvent, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import LabEditorPage from '../src/routes/labs/[...id]/+page.svelte';
import { ApiError } from '$lib/api';
import { authStore } from '$lib/stores/session';
import { toastStore } from '$lib/stores/toasts';
import { externalConsoleStore } from '$lib/stores/externalConsole';
import { infoPanels } from '$lib/stores/infoPanels';
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
  '1': {
    id: 1,
    name: 'router-1',
    type: 'qemu',
    status: 2,
    cpu: 1,
    ram: 512,
    console: 'telnet',
    interfaces: [],
  },
};

type Payload = { code: number; status: string; message?: string; data?: unknown };

// Per-test path handler; defaults to the happy lab-load payloads.
let servePath: (path: string) => Payload = () => ({ code: 200, status: 'success', data: {} });

function labLoadPayloads(): Record<string, Payload> {
  return {
    '/labs/sample.json': { code: 200, status: 'success', data: META },
    '/labs/sample.json/nodes': { code: 200, status: 'success', data: NODES },
    '/labs/sample.json/networks?include_hidden=true': { code: 200, status: 'success', data: {} },
    '/labs/sample.json/topology': { code: 200, status: 'success', data: [] },
    '/labs/sample.json/links': { code: 200, status: 'success', data: [] },
  };
}

function plantNode(node: Record<string, unknown> | undefined) {
  (globalThis as unknown as { __externalConsoleTestNode?: unknown }).__externalConsoleTestNode = node;
}

function captureToasts() {
  const messages: string[] = [];
  const unsubscribe = toastStore.subscribe((items) => {
    messages.splice(0, messages.length, ...items.map((item) => item.message));
  });
  return { messages, unsubscribe };
}

describe('lab editor controls', () => {
  let clickedHrefs: string[];
  let clickSpy: ReturnType<typeof vi.spyOn> | null = null;

  beforeEach(() => {
    window.localStorage.clear();
    window.document.cookie = 'novaVeConsoleMode=tabbed; path=/';
    authStore.setUser({ username: 'admin', role: 'admin', email: '', name: '' } as never);
    setPagePathname('/labs/sample.json');
    setPageParams({ id: 'sample.json' });
    clickedHrefs = [];
    servePath = (path) =>
      labLoadPayloads()[path] ?? { code: 200, status: 'success', data: {} };
    // Mirror the real apiRequest semantics: fail payloads reject with ApiError.
    apiRequest.mockReset();
    apiRequest.mockImplementation(async (path: string) => {
      const payload = servePath(path);
      if (payload.code >= 400 || payload.status === 'fail') {
        throw new ApiError(payload.message || 'Request failed.', { statusCode: payload.code });
      }
      return payload;
    });
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement
    ) {
      clickedHrefs.push(this.href);
    });
  });

  afterEach(() => {
    clickSpy?.mockRestore();
    clickSpy = null;
    plantNode(undefined);
    externalConsoleStore.setMode('off');
    infoPanels.reset();
  });

  it('warns about legacy nodes returned by lab reconciliation', async () => {
    servePath = (path) => path.endsWith('/runtime/reconcile')
      ? { code: 200, status: 'success', data: { legacy_nodes: [{ node_id: 1, name: 'router-1', container_name: 'private-container', network_mode: 'bridge', ownership: 'unverified' }] } }
      : labLoadPayloads()[path] ?? { code: 200, status: 'success', data: {} };
    render(LabEditorPage);

    const warning = await screen.findByRole('alert');
    expect(warning).toHaveTextContent('Potential legacy networked nodes: router-1');
    expect(warning).toHaveTextContent('verify the container belongs to this lab');
    expect(warning).toHaveTextContent('stop and remove it on the host');
    expect(warning).toHaveTextContent('Start the node again before creating links');
    expect(warning).not.toHaveTextContent('private-container');
  });

  it('does not show a legacy warning when reconciliation returns no legacy nodes', async () => {
    render(LabEditorPage);
    await screen.findByTestId('topology-canvas-stub');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('warns when Docker networking could not be inspected', async () => {
    servePath = (path) => path.endsWith('/runtime/reconcile')
      ? { code: 200, status: 'success', data: { legacy_nodes: [], legacy_inspection_unavailable: [{ node_id: 1, name: 'router-1' }] } }
      : labLoadPayloads()[path] ?? { code: 200, status: 'success', data: {} };
    render(LabEditorPage);

    const warning = await screen.findByRole('alert');
    expect(warning).toHaveTextContent('Could not inspect Docker networking: router-1');
    expect(warning).toHaveTextContent('check Docker connectivity, then retry');
  });

  it.each(['tabbed', 'floating'])(
    'Zen mode hides the chrome and preserves the canvas and %s console',
    async (consoleMode) => {
      document.cookie = `novaVeConsoleMode=${consoleMode}; path=/`;
      const { container } = render(LabEditorPage);
      const canvas = await screen.findByTestId('topology-canvas-stub');
      infoPanels.open('node', 1);
      const panel = await screen.findByTestId('info-panel');
      plantNode(NODES['1']);
      await fireEvent.click(screen.getByTestId('canvas-console-button'));
      const iframe = await screen.findByTitle('Guacamole console for router-1');
      const dialog = iframe.closest<HTMLElement>('[role="dialog"]');
      const toggle = screen.getByRole('button', { name: 'Enter Zen mode' });
      const header = container.querySelector('header');
      const originalSrc = iframe.getAttribute('src');

      expect(header).toBeVisible();
      expect(dialog).toBeVisible();
      expect(dialog).toHaveAccessibleName(
        consoleMode === 'tabbed' ? 'Console workspace' : 'Console window for router-1'
      );
      expect(panel).toBeVisible();
      expect(canvas).toHaveAttribute('data-zen-mode', 'false');
      apiRequest.mockClear();

      await fireEvent.click(toggle);

      expect(toggle).toHaveFocus();
      expect(screen.getByRole('button', { name: 'Exit Zen mode' })).toBe(toggle);
      expect(toggle).toHaveAttribute('aria-expanded', 'false');
      expect(header).not.toBeVisible();
      expect(panel).not.toBeVisible();
      expect(dialog).not.toBeVisible();
      expect(dialog?.inert).toBe(true);
      expect(iframe).toBeInTheDocument();
      expect(screen.getByTestId('topology-canvas-stub')).toBe(canvas);
      expect(canvas).toHaveAttribute('data-zen-mode', 'true');

      await fireEvent.click(screen.getByTestId('canvas-console-button'));
      expect(dialog).toBeVisible();
      expect(dialog?.inert).toBe(false);
      expect(screen.getByTitle('Guacamole console for router-1')).toBe(iframe);
      expect(header).not.toBeVisible();

      await fireEvent.click(toggle);

      expect(toggle).toHaveFocus();
      expect(screen.getByRole('button', { name: 'Enter Zen mode' })).toBe(toggle);
      expect(toggle).toHaveAttribute('aria-expanded', 'true');
      expect(header).toBeVisible();
      expect(panel).toBeVisible();
      expect(dialog).toBeVisible();
      expect(dialog?.inert).toBe(false);
      expect(screen.getByTitle('Guacamole console for router-1')).toBe(iframe);
      expect(iframe).toHaveAttribute('src', originalSrc);
      expect(screen.getByTestId('topology-canvas-stub')).toBe(canvas);
      expect(canvas).toHaveAttribute('data-zen-mode', 'false');
      expect(apiRequest).not.toHaveBeenCalled();
    }
  );

  it('toggle lives in the top bar, presses, and persists the choice', async () => {
    render(LabEditorPage);

    const toggle = await waitFor(() => screen.getByTestId('external-console-toggle'));
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    expect(toggle.getAttribute('title')).toMatch(/client registered/i);

    fireEvent.click(toggle);

    await waitFor(() => expect(toggle).toHaveAttribute('aria-pressed', 'true'));
    expect(window.localStorage.getItem('nova-ve.external-console')).toBe('on');

    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute('aria-pressed', 'false'));
    expect(window.localStorage.getItem('nova-ve.external-console')).toBe('off');
  });

  it('resizes only the selected floating window and preserves its maximized state when minimized', async () => {
    document.cookie = 'novaVeConsoleMode=floating; path=/';
    render(LabEditorPage);
    const openConsole = await screen.findByTestId('canvas-console-button');
    plantNode(NODES['1']);
    await fireEvent.click(openConsole);
    const first = await screen.findByRole('dialog', { name: 'Console window for router-1' });
    const firstIframe = within(first).getByTitle('Guacamole console for router-1');
    const openingWidth = first.style.width;

    plantNode({ ...NODES['1'], id: 2, name: 'router-2' });
    await fireEvent.click(openConsole);
    const second = await screen.findByRole('dialog', { name: 'Console window for router-2' });
    const secondBounds = [second.style.left, second.style.top, second.style.width, second.style.height];

    const firstLeft = first.style.left;
    await fireEvent.keyDown(within(first).getByRole('button', { name: 'Resize router-1 console from bottom right' }), {
      key: 'ArrowLeft'
    });
    expect(parseFloat(first.style.width)).toBeLessThan(parseFloat(openingWidth));
    expect(first.style.left).toBe(firstLeft);
    expect([second.style.left, second.style.top, second.style.width, second.style.height]).toEqual(secondBounds);

    await fireEvent.click(within(first).getByRole('button', { name: 'Maximize router-1 console' }));
    const maximizedWidth = first.style.width;
    expect(parseFloat(maximizedWidth)).toBeGreaterThan(parseFloat(openingWidth));
    await fireEvent.click(within(first).getByRole('button', { name: 'Minimize router-1 console' }));
    expect(first).not.toBeVisible();
    plantNode(NODES['1']);
    await fireEvent.click(openConsole);
    expect(first).toBeVisible();
    expect(first.style.width).toBe(maximizedWidth);

    await fireEvent.click(within(first).getByRole('button', { name: 'Restore router-1 console' }));
    expect(first.style.width).toBe(openingWidth);
    expect(within(first).getByTitle('Guacamole console for router-1')).toBe(firstIframe);
  });

  it('external mode: console action emits telnet:// without opening the workspace', async () => {
    externalConsoleStore.setMode('on');
    const { container } = render(LabEditorPage);
    await waitFor(() => screen.getByTestId('canvas-console-button'));

    plantNode({ id: 1, name: 'router-1', type: 'qemu', status: 2, console: 'telnet' });
    servePath = (path) => {
      if (path === '/api/labs/sample.json/nodes/1/external-console') {
        return {
          code: 200,
          status: 'success',
          data: { mode: 'primary', scheme: 'telnet', host: '192.168.7.9', port: 47501, console: 'telnet' },
        };
      }
      return labLoadPayloads()[path] ?? { code: 200, status: 'success', data: {} };
    };

    fireEvent.click(screen.getByTestId('canvas-console-button'));

    await waitFor(() => expect(clickedHrefs).toContain('telnet://192.168.7.9:47501'));
    expect(apiRequest).toHaveBeenCalledWith(
      '/api/labs/sample.json/nodes/1/external-console',
      expect.objectContaining({ suppressToast: true })
    );
    // The embedded workspace must not open in external mode.
    expect(container.querySelector('iframe')).toBeNull();
  });

  it('external mode: failure toasts and falls back to the in-browser console', async () => {
    externalConsoleStore.setMode('on');
    const { messages, unsubscribe } = captureToasts();
    const { container } = render(LabEditorPage);
    await waitFor(() => screen.getByTestId('canvas-console-button'));

    plantNode({ id: 1, name: 'router-1', type: 'qemu', status: 2, console: 'telnet' });
    servePath = (path) => {
      if (path === '/api/labs/sample.json/nodes/1/external-console') {
        return { code: 400, status: 'fail', message: 'Node is not running: 1.' };
      }
      return labLoadPayloads()[path] ?? { code: 200, status: 'success', data: {} };
    };

    fireEvent.click(screen.getByTestId('canvas-console-button'));

    await waitFor(() =>
      expect(messages.some((message) => message.includes('Node is not running'))).toBe(true)
    );
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull());
    expect((container.querySelector('iframe') as HTMLIFrameElement).getAttribute('src')).toContain(
      '/nodes/1/html5'
    );
    unsubscribe();
  });

  it('toggle off: console action opens the embedded workspace and never calls the endpoint', async () => {
    const { container } = render(LabEditorPage);
    await waitFor(() => screen.getByTestId('canvas-console-button'));

    plantNode({ id: 1, name: 'router-1', type: 'qemu', status: 2, console: 'telnet' });

    fireEvent.click(screen.getByTestId('canvas-console-button'));

    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull());
    const calledPaths = apiRequest.mock.calls.map((call) => String(call[0]));
    expect(calledPaths.some((path) => path.includes('external-console'))).toBe(false);
  });
});
