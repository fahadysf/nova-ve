// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0
/**
 * Secondary console support (#268) — frontend surfaces:
 *
 * 1. node context menu renders one pill per available console mode and each
 *    pill dispatches the console event with the matching mode;
 * 2. NodeConfigModal exposes the secondary console select and persists it
 *    into the create/edit payloads (explicit null clears it);
 * 3. TemplateDialog edits the template's secondary console field.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import type { NodeCatalog, NodeCatalogTemplate, NodeData } from '$lib/types';

vi.mock('@xyflow/svelte', async () => {
  const SvelteFlow = (await import('./mocks/SvelteFlowMock.svelte')).default;
  const SlotStub = (await import('./mocks/SlotStub.svelte')).default;
  const LeafStub = (await import('./mocks/LeafStub.svelte')).default;

  return {
    SvelteFlow,
    Background: LeafStub,
    MiniMap: LeafStub,
    Panel: SlotStub,
    useSvelteFlow: () => ({
      fitView: vi.fn(async () => {}),
      screenToFlowPosition: vi.fn(() => ({ x: 200, y: 200 })),
      zoomIn: vi.fn(async () => {}),
      zoomOut: vi.fn(async () => {}),
    }),
  };
});

vi.mock('$lib/api', () => {
  const emptyCatalog: NodeCatalog = {
    templates: [],
    icon_options: [],
    create_fields: [],
    edit_fields: [],
    runtime_editability: { always: [], stopped_only: [], immutable: [] },
  };

  return {
    ApiError: class ApiError extends Error {},
    apiRequest: vi.fn(async (path: string) => {
      if (path.endsWith('/node-catalog')) {
        return { data: emptyCatalog };
      }
      return { data: {} };
    }),
    apiGetData: vi.fn(async () => ({})),
  };
});

const { debouncerMocks } = vi.hoisted(() => ({
  debouncerMocks: [] as Array<Record<string, ReturnType<typeof vi.fn>>>,
}));

vi.mock('$lib/services/labApi', () => ({
  LayoutConflictError: class LayoutConflictError extends Error {
    serverRevision: number | null = null;
  },
  createLayoutDebouncer: () => {
    const mock = {
      pushNodePosition: vi.fn(),
      pushNetworkPosition: vi.fn(),
      pushViewport: vi.fn(),
      flush: vi.fn(async () => {}),
      flushKeepalive: vi.fn(),
      cancel: vi.fn(),
      pending: vi.fn(() => false),
      pendingPositionIds: vi.fn(() => ({ nodeIds: new Set(), networkIds: new Set() })),
    };
    debouncerMocks.push(mock);
    return mock;
  },
}));

vi.mock('$lib/services/wsClient', () => ({
  createWsClient: () => ({
    on: vi.fn(),
    connect: vi.fn(),
    close: vi.fn(),
  }),
}));

vi.mock('$lib/stores/labWs', () => ({
  createLabWsStores: () => ({
    liveMacs: writable({}),
    linkStates: writable({}),
    nodeStates: writable({}),
    linkReconciliation: writable({}),
    connected: writable(false),
    onLinkChange: vi.fn(() => vi.fn()),
  }),
}));

vi.mock('$lib/stores/toastStore', () => ({
  toastStore: { push: vi.fn() },
}));

import TopologyCanvasConsoleHarness from './TopologyCanvasConsoleHarness.svelte';
import NodeConfigModalHarness from './NodeConfigModalHarness.svelte';
import TemplateDialogHarness from './TemplateDialogHarness.svelte';
import { labState } from '$lib/stores/labState';
import type { LibraryTemplate } from '$lib/types';

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  labState.reset();
  cleanup();
});

// ---------------------------------------------------------------------------
// TopologyCanvas — context menu console pills
// ---------------------------------------------------------------------------

function makeCanvasNode(id: number, secondConsole: 'telnet' | 'vnc' | 'rdp' | null): NodeData {
  return {
    id,
    name: `node-${id}`,
    type: 'qemu',
    template: 'vyos',
    image: 'vyos-1.4',
    console: 'telnet',
    secondConsole,
    status: 2,
    delay: 0,
    cpu: 1,
    ram: 1024,
    ethernet: 1,
    left: 100 * id,
    top: 120,
    icon: 'Router.png',
    interfaces: [],
  };
}

function renderCanvas(nodes: Record<string, NodeData>) {
  labState.reset();
  labState.applySnapshot({
    nodes,
    networks: {},
    topology: [],
    links: [],
    defaults: { link_style: 'orthogonal' },
  });
  return render(TopologyCanvasConsoleHarness, { props: {} });
}

function openNodeMenu(nodeId: number) {
  const flow = screen.getByTestId('svelte-flow-mock');
  // The mock re-dispatches ``mock-flow-event`` payloads as component
  // events (legacy on: handlers never reach a runes child via props).
  flow.dispatchEvent(
    new CustomEvent('mock-flow-event', {
      detail: {
        type: 'nodecontextmenu',
        detail: {
          node: { id: `node${nodeId}` },
          event: new MouseEvent('contextmenu', { clientX: 40, clientY: 40 }),
        },
      },
    })
  );
}

async function consoleEvents(): Promise<string> {
  await tick();
  return screen.getByTestId('console-events').textContent ?? '';
}

describe('node context menu console pills', () => {
  it('renders one pill per available console mode', async () => {
    renderCanvas({ '1': makeCanvasNode(1, 'vnc') });
    openNodeMenu(1);
    await tick();

    const primary = screen.getByTestId('node-menu-console-primary');
    const secondary = screen.getByTestId('node-menu-console-secondary');
    expect(primary.textContent).toBe('telnet');
    expect(secondary.textContent).toBe('vnc');
  });

  it('keeps the single-console look when no secondary is declared', async () => {
    renderCanvas({ '1': makeCanvasNode(1, null) });
    openNodeMenu(1);
    await tick();

    expect(screen.getByTestId('node-menu-console-primary')).toBeTruthy();
    expect(screen.queryByTestId('node-menu-console-secondary')).toBeNull();
  });

  it('dispatches console with mode second when the secondary pill is clicked', async () => {
    renderCanvas({ '1': makeCanvasNode(1, 'vnc') });
    openNodeMenu(1);
    await tick();

    await fireEvent.click(screen.getByTestId('node-menu-console-secondary'));
    expect(await consoleEvents()).toBe('1:second');
  });

  it('dispatches console with mode primary when the primary pill is clicked', async () => {
    renderCanvas({ '1': makeCanvasNode(1, 'vnc') });
    openNodeMenu(1);
    await tick();

    await fireEvent.click(screen.getByTestId('node-menu-console-primary'));
    expect(await consoleEvents()).toBe('1:primary');
  });

  it('labels a paired-template serial primary as telnet', async () => {
    const node = { ...makeCanvasNode(1, 'vnc'), console: 'serial' as const };
    renderCanvas({ '1': node });
    openNodeMenu(1);
    await tick();

    expect(screen.getByTestId('node-menu-console-primary').textContent).toBe('telnet');
  });
});

// ---------------------------------------------------------------------------
// NodeConfigModal — secondary console select
// ---------------------------------------------------------------------------

function makeTemplate(overrides: Partial<NodeCatalogTemplate['defaults']> = {}): NodeCatalogTemplate {
  return {
    key: 'vyos',
    type: 'qemu',
    name: 'VyOS',
    description: 'VyOS router',
    defaults: {
      type: 'qemu',
      template: 'vyos',
      image: 'vyos-1.4',
      icon_type: 'router',
      icon: 'Router.png',
      cpu: 1,
      ram: 1024,
      ethernet: 4,
      console_type: 'telnet',
      delay: 0,
      cpulimit: 1,
      ...overrides,
    },
    images: [{ image: 'vyos-1.4' }],
    icon_options: ['Router.png'],
    extras_schema: [],
  };
}

function makeCatalog(...templates: NodeCatalogTemplate[]): NodeCatalog {
  return {
    templates,
    icon_options: ['Router.png'],
    create_fields: [],
    edit_fields: [],
    runtime_editability: { always: [], stopped_only: [], immutable: [] },
  };
}

function makeModalNode(overrides: Partial<NodeData> = {}): NodeData {
  return {
    id: 7,
    name: 'vyos-1',
    type: 'qemu',
    template: 'vyos',
    image: 'vyos-1.4',
    console: 'telnet',
    status: 0,
    delay: 0,
    cpu: 1,
    ram: 1024,
    ethernet: 4,
    left: 100,
    top: 120,
    icon: 'Router.png',
    interfaces: [],
    ...overrides,
  };
}

function secondConsoleSelect(): HTMLSelectElement {
  return screen.getByTestId('node-second-console') as HTMLSelectElement;
}

function primaryConsoleSelect(): HTMLSelectElement {
  const label = screen.getByText('Console', { exact: true }).closest('label');
  const select = label?.querySelector('select');
  if (!select) {
    throw new Error('Console select not found');
  }
  return select as HTMLSelectElement;
}

async function submitModalForm() {
  const form = screen.getByRole('dialog').querySelector('form');
  expect(form).not.toBeNull();
  await fireEvent.submit(form as HTMLFormElement);
  await tick();
  return JSON.parse(screen.getByTestId('submit-payload').textContent ?? 'null');
}

describe('NodeConfigModal secondary console', () => {
  it('seeds from the node and persists the declared secondary in the edit payload', async () => {
    const catalog = makeCatalog(makeTemplate());
    const node = makeModalNode({ secondConsole: 'vnc' });
    render(NodeConfigModalHarness, { props: { open: true, mode: 'edit', catalog, node } });
    await tick();

    expect(secondConsoleSelect().value).toBe('vnc');
    const payload = await submitModalForm();
    expect(payload.payload.second_console).toBe('vnc');
  });

  it('offers the type-supported modes except the primary plus none', async () => {
    const catalog = makeCatalog(makeTemplate());
    const node = makeModalNode({ console: 'telnet', secondConsole: null });
    render(NodeConfigModalHarness, { props: { open: true, mode: 'edit', catalog, node } });
    await tick();

    expect([...secondConsoleSelect().options].map((option) => option.value)).toEqual(['', 'vnc']);
  });

  it('clears the secondary via the none option', async () => {
    const catalog = makeCatalog(makeTemplate());
    const node = makeModalNode({ secondConsole: 'vnc' });
    render(NodeConfigModalHarness, { props: { open: true, mode: 'edit', catalog, node } });
    await tick();

    await fireEvent.change(secondConsoleSelect(), { target: { value: '' } });
    await tick();
    const payload = await submitModalForm();
    expect(payload.payload.second_console).toBeNull();
  });

  it('allows any standard secondary for a paired-template serial primary', async () => {
    const catalog = makeCatalog(makeTemplate());
    const node = makeModalNode({ console: 'serial', secondConsole: 'vnc' });
    render(NodeConfigModalHarness, { props: { open: true, mode: 'edit', catalog, node } });
    await tick();

    expect([...secondConsoleSelect().options].map((option) => option.value)).toEqual([
      '',
      'telnet',
      'vnc',
      'rdp',
    ]);
    const payload = await submitModalForm();
    expect(payload.payload.second_console).toBe('vnc');
    expect(payload.payload.console).toBe('serial');
  });

  it('seeds the create flow from the template defaults', async () => {
    const catalog = makeCatalog(makeTemplate({ second_console: 'vnc' }));
    render(NodeConfigModalHarness, { props: { open: true, mode: 'create', catalog, node: null } });
    await tick();

    expect(secondConsoleSelect().value).toBe('vnc');
    const payload = await submitModalForm();
    expect(payload.payload.second_console).toBe('vnc');
  });

  it('defaults to none in the create flow', async () => {
    const catalog = makeCatalog(makeTemplate());
    render(NodeConfigModalHarness, { props: { open: true, mode: 'create', catalog, node: null } });
    await tick();

    expect(secondConsoleSelect().value).toBe('');
    const payload = await submitModalForm();
    expect(payload.payload.second_console).toBeNull();
  });

  it('resets the secondary choice when it collides with a changed primary', async () => {
    const catalog = makeCatalog(makeTemplate());
    const node = makeModalNode({ console: 'telnet', secondConsole: 'vnc' });
    render(NodeConfigModalHarness, { props: { open: true, mode: 'edit', catalog, node } });
    await tick();

    // Switch the primary to vnc — the current secondary (vnc) must reset.
    await fireEvent.change(primaryConsoleSelect(), { target: { value: 'vnc' } });
    await tick();
    expect(secondConsoleSelect().value).toBe('');
  });
});

// ---------------------------------------------------------------------------
// TemplateDialog — template secondary console field
// ---------------------------------------------------------------------------

function makeLibraryTemplate(overrides: Partial<LibraryTemplate> = {}): LibraryTemplate {
  return {
    type: 'qemu',
    key: 'veos',
    name: 'vEOS-QEMU',
    description: '',
    icon_type: 'router',
    cpu: 1,
    ram: 2048,
    ethernet: 4,
    console_type: 'telnet',
    cpulimit: 1,
    image_aliases: ['veos'],
    images: [],
    ...overrides,
  };
}

describe('TemplateDialog second console field', () => {
  it('submits the declared secondary console', async () => {
    render(TemplateDialogHarness, { props: { template: makeLibraryTemplate({ second_console: 'vnc' }) } });
    await tick();

    const select = screen.getByTestId('template-dialog-second-console') as HTMLSelectElement;
    expect(select.value).toBe('vnc');
    await fireEvent.click(screen.getByTestId('template-dialog-save'));
    await tick();

    const edit = JSON.parse(screen.getByTestId('saved-edit').textContent ?? 'null');
    expect(edit.second_console).toBe('vnc');
    expect(edit.console_type).toBe('telnet');
  });

  it('clears the secondary console with the none option', async () => {
    render(TemplateDialogHarness, { props: { template: makeLibraryTemplate({ second_console: 'vnc' }) } });
    await tick();

    const select = screen.getByTestId('template-dialog-second-console') as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: '' } });
    await tick();
    await fireEvent.click(screen.getByTestId('template-dialog-save'));
    await tick();

    const edit = JSON.parse(screen.getByTestId('saved-edit').textContent ?? 'null');
    expect(edit.second_console).toBeNull();
  });

  it('shows none by default for templates without a secondary', async () => {
    render(TemplateDialogHarness, { props: { template: makeLibraryTemplate() } });
    await tick();

    const select = screen.getByTestId('template-dialog-second-console') as HTMLSelectElement;
    expect(select.value).toBe('');
    expect([...select.options].map((option) => option.value)).toEqual(['', 'telnet', 'vnc', 'rdp']);
  });
});
