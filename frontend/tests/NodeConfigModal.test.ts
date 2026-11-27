// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0
/**
 * Tests for US-305: capabilities banner in NodeConfigModal.
 *
 * NOTE: Full reactive-lifecycle tests (banner visible after template auto-select)
 * are deferred to US-305b. @testing-library/svelte v5 + Svelte 5 legacy reactive
 * labels ($:) require flushSync / rerender-based patterns that are out of scope
 * here. The tests below verify:
 *   1. The modal renders without error for each capability variant.
 *   2. The "no capabilities" path confirms banner absence on a freshly mounted
 *      modal whose catalog template has no capabilities field.
 *   3. The TemplateCapabilities TypeScript type is structurally correct.
 *
 * TODO(US-305b): add full reactive banner-visibility tests once the team picks
 * a canonical Svelte 5 testing pattern (flushSync or rerender).
 */

import { fireEvent, render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import NodeConfigModal from '$lib/components/canvas/NodeConfigModal.svelte';
import NodeConfigModalHarness from './NodeConfigModalHarness.svelte';
import { apiGetData, apiRequest } from '$lib/api';
import { formatInterfaceName } from '$lib/services/interfaceNaming';
import type { NodeCatalog, NodeCatalogExtraField, NodeCatalogTemplate, NodeData, TemplateCapabilities } from '$lib/types';

vi.mock('$lib/api', () => ({
  apiGetData: vi.fn(),
  apiRequest: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

it('updates Dynamips Ethernet totals and preserves an explicitly empty slot 0', async () => {
  const platform: NodeCatalogExtraField = {
    key: 'platform', label: 'Platform', type: 'select', default: 'c3725',
    options: [{ value: 'c3725' }, { value: 'c7200' }],
  };
  const slot = (index: number, options: NodeCatalogExtraField['options']): NodeCatalogExtraField => ({
    key: `slot${index}`, label: `Slot ${index}`, type: 'select', default: options?.[0]?.value, options,
  });
  const c3725 = [
    platform,
    slot(0, [{ value: 'GT96100-FE', ethernetPorts: 2 }]),
    slot(1, [
      { value: '', label: '(empty)', ethernetPorts: 0 },
      { value: 'NM-1FE-TX', ethernetPorts: 1 },
      { value: 'NM-4T', ethernetPorts: 0 },
    ]),
  ];
  const c7200 = [
    { ...platform, default: 'c7200' },
    slot(0, [
      { value: 'C7200-IO-FE', ethernetPorts: 1 },
      { value: 'none', label: '(empty)', ethernetPorts: 0 },
    ]),
    slot(1, [{ value: '', label: '(empty)', ethernetPorts: 0 }, { value: 'PA-GE', ethernetPorts: 1 }]),
  ];
  const base = makeTemplate();
  const template: NodeCatalogTemplate = {
    ...base,
    key: 'c3725', name: 'C3725', type: 'dynamips',
    defaults: { ...base.defaults, type: 'dynamips', template: 'c3725' },
    extras_schema: c3725,
  };
  const catalog = makeCatalog(template, {
    ...template,
    key: 'c7200', name: 'C7200',
    defaults: { ...template.defaults, template: 'c7200', image: 'c7200-image' },
    images: [{ image: 'c7200-image' }],
    extras_schema: c7200,
  });
  catalog.dynamips_profiles = { c3725, c7200 };
  render(NodeConfigModalHarness, { props: { open: true, mode: 'create', catalog } });
  await tick();
  expect(screen.getByText('2 (derived from slots)')).toBeVisible();
  await fireEvent.change(screen.getByLabelText('Slot 1'), { target: { value: 'NM-1FE-TX' } });
  expect(screen.getByText('3 (derived from slots)')).toBeVisible();
  await fireEvent.change(screen.getByLabelText('Slot 1'), { target: { value: 'NM-4T' } });
  expect(screen.getByText('2 (derived from slots)')).toBeVisible();
  expect(screen.getByLabelText('Platform')).toBeDisabled();
  await fireEvent.change(screen.getByLabelText('Template'), { target: { value: 'dynamips:c7200' } });
  expect(screen.getByText('1 (derived from slots)')).toBeVisible();
  expect(screen.getByLabelText('Slot 1')).not.toHaveTextContent('NM-1FE-TX');
  await fireEvent.change(screen.getByLabelText('Slot 0'), { target: { value: 'none' } });
  expect(screen.getByText('0 (derived from slots)')).toBeVisible();
  expect(screen.getByText('Slot 0 is empty. This router has no built-in I/O card.')).toBeVisible();
  await fireEvent.submit(screen.getByRole('dialog').querySelector('form')!);
  expect(JSON.parse(screen.getByTestId('submit-payload').textContent ?? 'null')).toMatchObject({
    mode: 'create', payload: {
      template: 'c7200', image: 'c7200-image', extras: { platform: 'c7200', slot0: 'none', slot1: '' },
    },
  });
});

function makeTemplate(caps?: TemplateCapabilities): NodeCatalogTemplate {
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
    },
    images: [{ image: 'vyos-1.4' }],
    icon_options: ['Router.png'],
    extras_schema: [],
    ...(caps !== undefined ? { capabilities: caps } : {}),
  };
}

function makeDockerTemplate(): NodeCatalogTemplate {
  return {
    key: 'docker',
    type: 'docker',
    name: 'Docker',
    description: 'Docker container',
    defaults: {
      type: 'docker',
      template: 'docker',
      image: 'accetto/ubuntu-vnc-xfce-firefox-g3:latest',
      icon_type: 'server',
      icon: 'Server.png',
      cpu: 1,
      ram: 1024,
      ethernet: 1,
      console_type: 'vnc',
      delay: 0,
      cpulimit: 1,
      extras: { vnc_port: 5900 },
    },
    images: [
      {
        image: 'accetto/ubuntu-vnc-xfce-firefox-g3:latest',
        source: 'docker',
        vnc_port: 5901,
      },
    ],
    icon_options: ['Server.png'],
    extras_schema: [
      {
        key: 'vnc_port',
        label: 'VNC port',
        type: 'number',
        default: 5900,
        stoppedOnly: true,
        runtime: true,
      },
    ],
    capabilities: { hotplug: true, max_nics: 99, machine: null },
  };
}

function makeIolTemplate(caps?: TemplateCapabilities): NodeCatalogTemplate {
  return {
    key: 'iol',
    type: 'iol',
    name: 'Cisco IOL',
    description: 'IOL image',
    defaults: {
      type: 'iol',
      template: 'iol',
      image: 'i86bi_Linux.bin',
      icon_type: 'router',
      icon: 'Router.png',
      cpu: 1,
      ram: 512,
      ethernet: 4,
      console_type: 'telnet',
      delay: 0,
      cpulimit: 1,
    },
    images: [{ image: 'i86bi_Linux.bin' }],
    icon_options: ['Router.png'],
    extras_schema: [],
    ...(caps !== undefined ? { capabilities: caps } : {}),
  };
}

function makeWindowsTemplate(): NodeCatalogTemplate {
  const base = makeTemplate();
  return {
    ...base,
    key: 'win',
    name: 'Windows',
    description: 'Windows VM',
    defaults: {
      ...base.defaults,
      template: 'win',
      image: 'win',
      icon_type: 'server',
      icon: 'Server.png',
      cpu: 4,
      ram: 4096,
      ethernet: 1,
      console_type: 'rdp',
    },
    images: [{ image: 'win' }],
    icon_options: ['Server.png'],
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

function makeNode(overrides: Partial<NodeData> = {}): NodeData {
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

function interfaceNamingInput(): HTMLInputElement {
  const input = screen
    .getAllByRole('textbox')
    .find((element) => (element as HTMLInputElement).placeholder.includes('mgmt0'));
  if (!input) {
    throw new Error('Interface naming input not found');
  }
  return input as HTMLInputElement;
}

function consoleSelect(): HTMLSelectElement {
  const label = screen.getByText('Console').closest('label');
  const select = label?.querySelector('select');
  if (!select) {
    throw new Error('Console select not found');
  }
  return select as HTMLSelectElement;
}

describe('NodeConfigModal capabilities banner — static paths', () => {
  it('renders without error when catalog template has hotplug=true', async () => {
    const catalog = makeCatalog(makeTemplate({ hotplug: true, max_nics: 8, machine: 'q35' }));
    render(NodeConfigModal, { props: { open: true, mode: 'create', catalog, node: null } });
    await tick();
    expect(screen.getByRole('dialog')).toBeTruthy();
  });

  it('renders without error when catalog template has hotplug=false', async () => {
    const catalog = makeCatalog(makeTemplate({ hotplug: false, max_nics: 8, machine: 'pc' }));
    render(NodeConfigModal, { props: { open: true, mode: 'create', catalog, node: null } });
    await tick();
    expect(screen.getByRole('dialog')).toBeTruthy();
  });

  it('renders without error for docker template with max_nics=99 and machine=null', async () => {
    const catalog = makeCatalog(makeTemplate({ hotplug: true, max_nics: 99, machine: null }));
    render(NodeConfigModal, { props: { open: true, mode: 'create', catalog, node: null } });
    await tick();
    expect(screen.getByRole('dialog')).toBeTruthy();
  });

  it('no capabilities banner when template has no capabilities field (backward compat)', async () => {
    const catalog = makeCatalog(makeTemplate(undefined));
    render(NodeConfigModal, { props: { open: true, mode: 'create', catalog, node: null } });
    await tick();
    expect(screen.getByRole('dialog')).toBeTruthy();
    // Banner must be absent — capabilities field is undefined on this template
    expect(screen.queryByTestId('capabilities-banner')).toBeNull();
  });

  it('shows IOL as restart-required even if the catalog marks hotplug true', async () => {
    const catalog = makeCatalog(makeIolTemplate({ hotplug: true, max_nics: 8, machine: null }));
    vi.mocked(apiGetData).mockResolvedValueOnce({
      installed: true,
      directory: '/var/lib/nova-ve/iourc',
      path: '/var/lib/nova-ve/iourc/iourc',
      filename: 'iourc',
      size: 12,
      hostname: 'nova-host',
      fqdn: 'nova-host',
      ips: ['192.0.2.10'],
      host_id: '00000000',
    });

    render(NodeConfigModal, { props: { open: true, mode: 'create', catalog, node: null } });
    await tick();

    expect(screen.getByTestId('capabilities-banner')).toBeTruthy();
    expect(screen.getByText('Live link changes: boot NICs only')).toBeTruthy();
    expect(screen.queryByText('Hot-plug supported')).toBeNull();
  });

  it('TemplateCapabilities type accepts all three variants (type-level smoke)', () => {
    const qemu: TemplateCapabilities = { hotplug: true, max_nics: 8, machine: 'q35' };
    const docker: TemplateCapabilities = { hotplug: true, max_nics: 99, machine: null };
    const legacy: TemplateCapabilities = { hotplug: false, max_nics: 8, machine: 'pc' };

    expect(qemu.machine).toBe('q35');
    expect(docker.machine).toBeNull();
    expect(legacy.hotplug).toBe(false);
  });

  it('does not offer rdp for qemu templates and falls back to vnc', async () => {
    const catalog = makeCatalog(makeWindowsTemplate());
    render(NodeConfigModal, { props: { open: true, mode: 'create', catalog, node: null } });
    await tick();

    const select = consoleSelect();
    expect(select.value).toBe('vnc');
    expect([...select.options].map((option) => option.value)).toEqual(['vnc', 'telnet']);
  });

  it('offers RDP for either console on a Docker node', async () => {
    const catalog = makeCatalog(makeDockerTemplate());
    render(NodeConfigModal, { props: { open: true, mode: 'create', catalog, node: null } });
    await tick();

    const primary = consoleSelect();
    expect([...primary.options].map((option) => option.value)).toEqual(['telnet', 'vnc', 'rdp']);
    expect(primary.querySelector('option[value="rdp"]')?.textContent).toBe('RDP');

    const second = screen.getByTestId('node-second-console');
    expect(second.querySelector('option[value="rdp"]')?.textContent).toBe('RDP');
    await fireEvent.change(primary, { target: { value: 'rdp' } });
    expect(second.querySelector('option[value="rdp"]')).toBeNull();
  });

  it('limits iol templates to telnet consoles', async () => {
    const catalog = makeCatalog(makeIolTemplate());
    vi.mocked(apiGetData).mockResolvedValueOnce({
      installed: true,
      directory: '/var/lib/nova-ve/iourc',
      path: '/var/lib/nova-ve/iourc/iourc',
      filename: 'iourc',
      size: 12,
      hostname: 'nova-host',
      fqdn: 'nova-host',
      ips: ['192.0.2.10'],
      host_id: '00000000',
    });
    render(NodeConfigModal, { props: { open: true, mode: 'create', catalog, node: null } });
    await tick();

    expect([...consoleSelect().options].map((option) => option.value)).toEqual(['telnet']);
  });

  it('shows missing IOURC status and opens upload dialog with host details', async () => {
    const catalog = makeCatalog(makeIolTemplate());
    vi.mocked(apiGetData).mockResolvedValueOnce({
      installed: false,
      directory: '/var/lib/nova-ve/iourc',
      path: null,
      filename: null,
      size: null,
      hostname: 'nova-host',
      fqdn: 'nova-host.example',
      ips: ['192.0.2.10'],
      host_id: 'abc12345',
    });

    render(NodeConfigModal, { props: { open: true, mode: 'create', catalog, node: null } });

    expect(await screen.findByText('Not available')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Upload IOURC' }));
    await tick();

    expect(screen.getByRole('dialog', { name: 'Upload IOURC license' })).toBeTruthy();
    expect(screen.getByText('nova-host')).toBeTruthy();
    expect(screen.getByText('192.0.2.10')).toBeTruthy();
  });

  it('uploads IOURC file and refreshes installed state', async () => {
    const catalog = makeCatalog(makeIolTemplate());
    vi.mocked(apiGetData).mockResolvedValueOnce({
      installed: false,
      directory: '/var/lib/nova-ve/iourc',
      path: null,
      filename: null,
      size: null,
      hostname: 'nova-host',
      fqdn: 'nova-host',
      ips: [],
      host_id: null,
    });
    vi.mocked(apiRequest).mockResolvedValueOnce({
      code: 200,
      status: 'success',
      message: 'IOURC license uploaded.',
      data: {
        installed: true,
        directory: '/var/lib/nova-ve/iourc',
        path: '/var/lib/nova-ve/iourc/iourc',
        filename: 'iourc',
        size: 10,
        hostname: 'nova-host',
        fqdn: 'nova-host',
        ips: [],
        host_id: null,
      },
    });

    render(NodeConfigModal, { props: { open: true, mode: 'create', catalog, node: null } });

    await screen.findByText('Not available');
    await fireEvent.click(screen.getByRole('button', { name: 'Upload IOURC' }));
    const input = screen.getByLabelText('License file') as HTMLInputElement;
    const file = new File(['[license]\n'], 'iourc', { type: 'text/plain' });
    await fireEvent.change(input, { target: { files: [file] } });
    await fireEvent.click(screen.getByRole('button', { name: 'Upload' }));
    await tick();

    expect(vi.mocked(apiRequest)).toHaveBeenCalledWith(
      '/system/iourc',
      expect.objectContaining({ method: 'POST', body: expect.any(FormData) }),
    );
    expect(screen.queryByRole('dialog', { name: 'Upload IOURC license' })).toBeNull();
    expect(screen.getByText('Installed')).toBeTruthy();
  });

  it('normalizes legacy qemu rdp nodes to vnc on edit', async () => {
    const catalog = makeCatalog(makeWindowsTemplate());
    const node = makeNode({
      template: 'win',
      image: 'win',
      console: 'rdp',
      icon: 'Server.png',
    });
    render(NodeConfigModal, { props: { open: true, mode: 'edit', catalog, node } });
    await tick();

    const select = consoleSelect();
    expect(select.value).toBe('vnc');
    expect([...select.options].map((option) => option.value)).toEqual(['vnc', 'telnet']);
  });

  it('offers registered icons and clears the persisted override to inherit the template', async () => {
    const template = makeTemplate();
    template.defaults.icon = 'template.png';
    const catalog = { ...makeCatalog(template), icon_assets: ['override.png', 'template.png'] };
    render(NodeConfigModalHarness, {
      props: { open: true, mode: 'edit', catalog, node: makeNode({ icon: 'override.png' }) },
    });
    await tick();

    const iconSelect = screen.getByRole<HTMLSelectElement>('combobox', { name: 'Icon' });
    expect(iconSelect.value).toBe('override.png');
    expect(iconSelect).toHaveTextContent('Use template default (template.png)');
    await fireEvent.change(iconSelect, { target: { value: '' } });
    await fireEvent.submit(screen.getByRole('dialog').querySelector('form')!);

    expect(JSON.parse(screen.getByTestId('submit-payload').textContent ?? 'null')).toMatchObject({
      mode: 'edit',
      payload: { icon: '' },
    });
  });

  it('dispatches interface_naming_scheme in create payloads', async () => {
    const catalog = makeCatalog(makeTemplate());
    render(NodeConfigModalHarness, {
      props: { open: true, mode: 'create', catalog, node: null },
    });

    await tick();
    await fireEvent.input(interfaceNamingInput(), {
      target: { value: 'Port{n}' },
    });
    const createForm = screen.getByRole('dialog').querySelector('form');
    expect(createForm).not.toBeNull();
    await fireEvent.submit(createForm as HTMLFormElement);
    await tick();

    expect(screen.getByTestId('submit-payload').textContent).toBeTruthy();
    expect(JSON.parse(screen.getByTestId('submit-payload').textContent ?? 'null')).toEqual(
      expect.objectContaining({
        mode: 'create',
        payload: expect.objectContaining({
          interface_naming_scheme: 'Port{n}',
        }),
      }),
    );
  });

  it('dispatches interface_naming_scheme in edit payloads', async () => {
    const catalog = makeCatalog(makeTemplate());
    const node = makeNode({ interface_naming_scheme: 'eth{n}' });
    render(NodeConfigModalHarness, {
      props: { open: true, mode: 'edit', catalog, node },
    });

    await tick();
    await fireEvent.input(interfaceNamingInput(), {
      target: { value: '' },
    });
    const editForm = screen.getByRole('dialog').querySelector('form');
    expect(editForm).not.toBeNull();
    await fireEvent.submit(editForm as HTMLFormElement);
    await tick();

    expect(screen.getByTestId('submit-payload').textContent).toBeTruthy();
    expect(JSON.parse(screen.getByTestId('submit-payload').textContent ?? 'null')).toEqual(
      expect.objectContaining({
        mode: 'edit',
        payload: expect.objectContaining({
          interface_naming_scheme: null,
        }),
      }),
    );
  });

  it('blocks submit when interface naming input is invalid', async () => {
    const catalog = makeCatalog(makeTemplate());
    const node = makeNode({ interface_naming_scheme: 'eth{n}' });
    render(NodeConfigModalHarness, {
      props: { open: true, mode: 'edit', catalog, node },
    });

    await tick();
    await fireEvent.input(interfaceNamingInput(), {
      target: { value: 'mgmt{n},Gi{port}' },
    });
    await tick();
    const editForm = screen.getByRole('dialog').querySelector('form');
    await fireEvent.submit(editForm as HTMLFormElement);
    await tick();

    expect(screen.getByTestId('submit-payload').textContent).toBe('');
  });

  it('accepts a comma-separated list with placeholder on the last entry', async () => {
    const catalog = makeCatalog(makeTemplate());
    const node = makeNode({ interface_naming_scheme: 'eth{n}' });
    render(NodeConfigModalHarness, {
      props: { open: true, mode: 'edit', catalog, node },
    });

    await tick();
    await fireEvent.input(interfaceNamingInput(), {
      target: { value: 'mgmt0,eth{n}' },
    });
    await tick();
    const editForm = screen.getByRole('dialog').querySelector('form');
    await fireEvent.submit(editForm as HTMLFormElement);
    await tick();

    expect(JSON.parse(screen.getByTestId('submit-payload').textContent ?? 'null')).toEqual(
      expect.objectContaining({
        mode: 'edit',
        payload: expect.objectContaining({
          interface_naming_scheme: 'mgmt0,eth{n}',
        }),
      }),
    );
  });

  it('offers only naming presets whose labels keep to four leading letters', async () => {
    const catalog = makeCatalog(makeTemplate());
    render(NodeConfigModalHarness, {
      props: { open: true, mode: 'create', catalog, node: null },
    });
    await tick();

    const select = screen.getByLabelText('Interface naming preset') as HTMLSelectElement;
    const values = [...select.options].map((option) => option.value);
    const offences: string[] = [];
    for (const value of values) {
      // '' follows the template; '__custom__' is the free-text entry.
      if (!value || value === '__custom__') continue;
      const fixed = value.split(',').length - 1;
      for (let index = 0; index <= fixed + 2; index += 1) {
        const label = formatInterfaceName(value, index);
        // Text before the first digit, slash or dot is the part that must stay short.
        const head = label.split(/[\d/.]/)[0];
        if (head === label) continue;
        if (/\s/.test(head) || head.replace(/[^A-Za-z]/g, '').length > 4) offences.push(label);
      }
    }

    expect(offences).toEqual([]);
    expect(values).toEqual(
      expect.arrayContaining(['Gi0/0/{n}', 'Et{port}', 'Ma1,Et{port}', 'Ma0/0,Gi0/{n}'])
    );
  });

  it('uses the selected Docker image VNC port as the default extra', async () => {
    const catalog = makeCatalog(makeTemplate(), makeDockerTemplate());
    render(NodeConfigModalHarness, {
      props: { open: true, mode: 'create', catalog, node: null },
    });

    await tick();
    await fireEvent.click(screen.getByRole('button', { name: /docker/i }));
    await tick();
    const createForm = screen.getByRole('dialog').querySelector('form');
    expect(createForm).not.toBeNull();
    await fireEvent.submit(createForm as HTMLFormElement);
    await tick();

    expect(JSON.parse(screen.getByTestId('submit-payload').textContent ?? 'null')).toEqual(
      expect.objectContaining({
        mode: 'create',
        payload: expect.objectContaining({
          image: 'accetto/ubuntu-vnc-xfce-firefox-g3:latest',
          console: 'vnc',
          extras: expect.objectContaining({ vnc_port: 5901 }),
        }),
      }),
    );
  });

  it('lets the operator override the Docker VNC port in the modal', async () => {
    const catalog = makeCatalog(makeTemplate(), makeDockerTemplate());
    render(NodeConfigModalHarness, {
      props: { open: true, mode: 'create', catalog, node: null },
    });

    await tick();
    await fireEvent.click(screen.getByRole('button', { name: /docker/i }));
    await tick();
    await tick();
    const vncPortInput = screen.getByDisplayValue('5901') as HTMLInputElement;
    await fireEvent.input(vncPortInput, { target: { value: '5902' } });
    const createForm = screen.getByRole('dialog').querySelector('form');
    expect(createForm).not.toBeNull();
    await fireEvent.submit(createForm as HTMLFormElement);
    await tick();

    expect(JSON.parse(screen.getByTestId('submit-payload').textContent ?? 'null')).toEqual(
      expect.objectContaining({
        mode: 'create',
        payload: expect.objectContaining({
          extras: expect.objectContaining({ vnc_port: 5902 }),
        }),
      }),
    );
  });
});


it.each([false, true])('refreshes catalog choices without resetting an open draft (added template: %s)', async (added) => {
  const template = makeTemplate();
  const { rerender } = render(NodeConfigModalHarness, {
    props: { open: true, mode: 'create', catalog: makeCatalog(template) },
  });
  await tick();
  await fireEvent.input(screen.getByLabelText('Name prefix'), { target: { value: 'Custom router' } });
  const refreshed = { ...template, images: [...template.images, { image: 'vyos-next' }] };
  await rerender({
    catalog: makeCatalog(refreshed, ...(added ? [{ ...template, key: 'extra', name: 'Extra' }] : [])),
  });
  await tick();

  expect(screen.getByLabelText('Name prefix')).toHaveValue('Custom router');
  expect(screen.getByLabelText('Image')).toHaveTextContent('vyos-next');
});

it('keeps paired name overrides and blocks submission if the selected pair disappears', async () => {
  const catalog = makeCatalog(makeTemplate());
  const pair = {
    key: 'paired', name: 'Paired fixture', vendor: 'fixture', child_count: 1, link_count: 0,
    children: [{ id: 'first', kind: 'qemu', name: 'Guest', image: 'vyos-1.4', cpu: 1, ram: 1024, ethernet: 4 }],
    links: [],
  };
  const fullCatalog = { ...catalog, paired_templates: [pair] };
  const { rerender } = render(NodeConfigModalHarness, {
    props: { open: true, mode: 'create', catalog: fullCatalog },
  });
  await tick();
  const picker = screen.getByRole('option', { name: /Paired fixture/ }).closest('select')!;
  await fireEvent.change(picker, { target: { value: 'paired' } });
  await fireEvent.input(screen.getByLabelText('Name · first'), { target: { value: 'Custom guest' } });

  await rerender({ catalog });
  const form = screen.getByRole('dialog').querySelector('form')!;
  expect(form.querySelector('[type=submit]')).toBeDisabled();
  await fireEvent.submit(form);
  expect(screen.getByTestId('submit-payload').textContent).toBe('');

  await rerender({ catalog: fullCatalog });
  expect(screen.getByLabelText('Name · first')).toHaveValue('Custom guest');
});
