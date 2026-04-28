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
import { describe, expect, it } from 'vitest';
import { tick } from 'svelte';
import NodeConfigModal from '$lib/components/canvas/NodeConfigModal.svelte';
import NodeConfigModalHarness from './NodeConfigModalHarness.svelte';
import type { NodeCatalog, NodeCatalogTemplate, NodeData, TemplateCapabilities } from '$lib/types';

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

function makeCatalog(template: NodeCatalogTemplate): NodeCatalog {
  return {
    templates: [template],
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

  it('TemplateCapabilities type accepts all three variants (type-level smoke)', () => {
    const qemu: TemplateCapabilities = { hotplug: true, max_nics: 8, machine: 'q35' };
    const docker: TemplateCapabilities = { hotplug: true, max_nics: 99, machine: null };
    const legacy: TemplateCapabilities = { hotplug: false, max_nics: 8, machine: 'pc' };

    expect(qemu.machine).toBe('q35');
    expect(docker.machine).toBeNull();
    expect(legacy.hotplug).toBe(false);
  });

  it('dispatches interface_naming_scheme in create payloads', async () => {
    const catalog = makeCatalog(makeTemplate());
    render(NodeConfigModalHarness, {
      props: { open: true, mode: 'create', catalog, node: null },
    });

    await tick();
    await fireEvent.change(screen.getByRole('combobox', { name: /interface naming/i }), {
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
    await fireEvent.change(screen.getByRole('combobox', { name: /interface naming/i }), {
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
});
