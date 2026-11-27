// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen, fireEvent, waitFor, within } from '@testing-library/svelte';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import LibraryPage from '../../src/routes/(shell)/library/images/+page.svelte';
import type { LibraryImage, LibraryTemplate } from '../../src/lib/types';

const apiRequest = vi.fn();

vi.mock('$lib/api', () => ({
  apiRequest: (...args: unknown[]) => apiRequest(...args),
}));

const TEMPLATES: LibraryTemplate[] = [
  {
    type: 'qemu',
    key: 'veos',
    source: 'builtin',
    name: 'vEOS-QEMU',
    description: 'Arista vEOS',
    icon_type: 'router',
    cpu: 1,
    ram: 2048,
    ethernet: 4,
    console_type: 'telnet',
    cpulimit: 1,
    image_aliases: ['veos', 'arista-lab-*'],
    interface_naming: { format: 'Ethernet{port}' },
    capabilities: { machine: 'q35', hotplug: true, max_nics: 16 },
    extras: { qemu_nic: 'virtio-net-pci', qemu_version: '5.2.0' },
    extras_schema: [
      { key: 'qemu_nic', label: 'NIC model', type: 'select', default: 'e1000', options: [
        { value: 'e1000' }, { value: 'virtio-net-pci' },
      ] },
      { key: 'qemu_version', label: 'QEMU version', type: 'select', default: '', options: [
        { value: '', label: 'Host default' },
      ] },
      { key: 'qemu_options', label: 'Extra QEMU arguments', type: 'textarea', default: '' },
    ],
    images: [
      { name: 'veos', filename: 'veos-4.31.3F.qcow2', state: 'available', size: 1298568192 },
    ],
  },
  {
    type: 'qemu',
    key: 'vqfx',
    source: 'user',
    name: 'vQFX',
    description: '',
    icon_type: 'router',
    cpu: 1,
    ram: 1024,
    ethernet: 2,
    console_type: 'telnet',
    cpulimit: 1,
    image_aliases: ['vqfx'],
    images: [],
  },
];

const IMAGES: LibraryImage[] = [
  {
    name: 'veos',
    filename: 'veos-4.31.3F.qcow2',
    type: 'qemu',
    path: '/var/lib/nova-ve/images/qemu/veos',
    files: ['veos-4.31.3F.qcow2'],
    size: 1298568192,
    mtime: 1735000000,
    mtime_iso: '2024-12-24 03:46',
    sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    state: 'available',
    referenced_by: [
      { lab: 'arc-lab.json', lab_path: '/arc-lab.json', lab_name: 'Arc Lab', node: 'sw1', template: 'veos' },
      { lab: 'ST-Demo.json', lab_path: '/ST-Demo.json', lab_name: 'ST Demo', node: 'leaf1', template: 'veos' },
    ],
    templates: [{ type: 'qemu', key: 'veos', name: 'vEOS-QEMU' }],
  },
  {
    name: 'vyos',
    filename: 'vyos-1.5-rolling.qcow2',
    type: 'qemu',
    path: '/var/lib/nova-ve/images/qemu/vyos',
    files: ['vyos-1.5-rolling.qcow2'],
    size: 1288490188,
    mtime: 1735000000,
    mtime_iso: '2024-12-24 03:46',
    sha256: null,
    state: 'available',
    referenced_by: [],
    templates: [],
  },
];

function mockApi(templates = TEMPLATES) {
  apiRequest.mockImplementation(async (path: string) => {
    if (path === '/library/templates') {
      return { code: 200, status: 'success', message: '', data: { templates } };
    }
    if (path === '/library/images') {
      return { code: 200, status: 'success', message: '', data: { images: IMAGES } };
    }
    if (path === '/library/images/qemu/vyos/verify') {
      return {
        code: 200,
        status: 'success',
        message: '',
        data: { sha256: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', size: 1288490188 },
      };
    }
    return { code: 200, status: 'success', message: '', data: {} };
  });
}

describe('Images & Templates library page (#264, live)', () => {
  beforeEach(() => {
    apiRequest.mockReset();
    mockApi();
  });

  it('renders template cards grouped by type with their matching on-box images', async () => {
    render(LibraryPage);

    await waitFor(() => {
      expect(screen.getByTestId('template-card-veos')).toBeInTheDocument();
    });

    expect(screen.getByText('vEOS-QEMU')).toBeInTheDocument();
    expect(screen.getByText('veos-4.31.3F.qcow2')).toBeInTheDocument();
    // Missing-image template state is explicit.
    expect(screen.getByText(/No image matches/i)).toBeInTheDocument();
    expect(screen.getByText('1.2 GB')).toBeInTheDocument();
  });

  it('renders both template images when their display names match', async () => {
    const template = TEMPLATES[0];
    mockApi([{
      ...template,
      images: [
        { ...template.images[0], filename: 'veos.qcow2' },
        { ...template.images[0], filename: 'veos.iso' },
      ],
    }]);
    render(LibraryPage);

    const card = within(await screen.findByTestId('template-card-veos'));
    expect(card.getByText('veos.qcow2')).toBeInTheDocument();
    expect(card.getByText('veos.iso')).toBeInTheDocument();
  });

  it('switches to the images tab and lists size, checksum, and lab references', async () => {
    render(LibraryPage);
    await waitFor(() => screen.getByTestId('template-card-veos'));

    await fireEvent.click(screen.getByTestId('images-tab-images'));

    expect(screen.getByTestId('images-panel')).toBeInTheDocument();
    expect(screen.getByText('vyos-1.5-rolling.qcow2')).toBeInTheDocument();
    // Reference chips render lab file names, not raw paths.
    expect(screen.getByText('arc-lab.json')).toBeInTheDocument();
    expect(screen.getAllByText('ST-Demo.json').length).toBeGreaterThanOrEqual(1);
    // Referenced images cannot be deleted — blocked marker replaces the action.
    expect(screen.getByTestId('image-delete-blocked-veos')).toBeInTheDocument();
    // Unreferenced images expose the delete action.
    expect(screen.getByTestId('image-delete-vyos')).toBeInTheDocument();
  });

  it('verify computes the checksum through the API and shows the cached value', async () => {
    render(LibraryPage);
    await waitFor(() => screen.getByTestId('template-card-veos'));
    await fireEvent.click(screen.getByTestId('images-tab-images'));

    await fireEvent.click(screen.getByTestId('image-verify-vyos'));

    await waitFor(() => {
      const verifyCall = apiRequest.mock.calls.find(
        ([path, init]) =>
          path === '/library/images/qemu/vyos/verify' &&
          (init as { method?: string })?.method === 'POST'
      );
      expect(verifyCall).toBeTruthy();
    });
    expect(screen.getByText('bbbbbbbbbbbb…bbbbbb')).toBeInTheDocument();
  });

  it('filters images by search query', async () => {
    render(LibraryPage);
    await waitFor(() => screen.getByTestId('template-card-veos'));
    await fireEvent.click(screen.getByTestId('images-tab-images'));

    await fireEvent.input(screen.getByTestId('images-search'), {
      target: { value: 'vyos' },
    });
    expect(screen.getByText('vyos-1.5-rolling.qcow2')).toBeInTheDocument();
    expect(screen.queryByText('veos-4.31.3F.qcow2')).toBeNull();
  });

  it('filters templates by an image alias', async () => {
    render(LibraryPage);
    await waitFor(() => screen.getByTestId('template-card-vqfx'));

    await fireEvent.input(screen.getByTestId('images-search'), {
      target: { value: 'ARISTA-LAB' },
    });
    expect(screen.getByTestId('template-card-veos')).toBeInTheDocument();
    expect(screen.queryByTestId('template-card-vqfx')).not.toBeInTheDocument();
  });

  it('combines image availability with template search and type', async () => {
    const [veos, vqfx] = TEMPLATES;
    mockApi([
      veos,
      vqfx,
      { ...veos, type: 'docker', key: 'docker-router' },
      {
        ...vqfx,
        key: 'vqfx-missing',
        images: [{ ...veos.images[0], state: 'missing' }],
      },
    ]);
    render(LibraryPage);
    await screen.findByTestId('template-card-vqfx');

    const availability = screen.getByRole('combobox', { name: 'Filter templates by image availability' });
    const search = screen.getByTestId('images-search');
    const type = screen.getByTestId('images-type-filter');
    expect(search.nextElementSibling).toBe(availability);
    expect(availability.nextElementSibling).toBe(type);
    expect(availability).toHaveValue('all');
    expect(screen.getByTestId('template-card-vqfx-missing')).toBeInTheDocument();

    await fireEvent.change(availability, { target: { value: 'with-images' } });
    expect(screen.getByTestId('template-card-veos')).toBeInTheDocument();
    expect(screen.getByTestId('template-card-docker-router')).toBeInTheDocument();
    expect(screen.queryByTestId('template-card-vqfx')).not.toBeInTheDocument();
    expect(screen.queryByTestId('template-card-vqfx-missing')).not.toBeInTheDocument();

    await fireEvent.change(type, { target: { value: 'qemu' } });
    expect(screen.queryByTestId('template-card-docker-router')).not.toBeInTheDocument();
    expect(screen.getByTestId('template-card-veos')).toBeInTheDocument();

    await fireEvent.input(search, { target: { value: 'vqfx' } });
    expect(screen.getByText('No templates match your filters.')).toBeInTheDocument();
    await fireEvent.change(availability, { target: { value: 'all' } });
    expect(screen.getByTestId('template-card-vqfx')).toBeInTheDocument();
    expect(screen.queryByTestId('template-card-veos')).not.toBeInTheDocument();

    await fireEvent.click(screen.getByTestId('images-tab-images'));
    expect(screen.queryByRole('combobox', { name: 'Filter templates by image availability' })).not.toBeInTheDocument();
  });

  it('creates a template and refreshes templates and image associations', async () => {
    const templates = [...TEMPLATES];
    mockApi(templates);
    const readApi = apiRequest.getMockImplementation()!;
    apiRequest.mockImplementation(async (path, options) => {
      if (path === '/library/templates' && options?.method === 'POST') {
        templates.push({
          ...TEMPLATES[1],
          ...options.body,
          images: [],
        });
      }
      return readApi(path, options);
    });

    render(LibraryPage);
    await screen.findByTestId('template-card-veos');
    await fireEvent.click(screen.getByRole('button', { name: 'Add template' }));
    const dialog = within(screen.getByRole('dialog', { name: 'Create template' }));
    const create = dialog.getByRole('button', { name: 'Create template' });

    await fireEvent.change(dialog.getByLabelText('Template type'), { target: { value: 'iol' } });
    await fireEvent.input(dialog.getByLabelText('Display name'), { target: { value: 'Custom IOL' } });
    await fireEvent.input(dialog.getByLabelText('Template key'), { target: { value: '../custom' } });
    expect(create).toBeDisabled();
    await fireEvent.input(dialog.getByLabelText('Template key'), { target: { value: 'custom-iol' } });
    await fireEvent.input(dialog.getByLabelText('Description'), { target: { value: 'Custom router' } });
    await fireEvent.input(dialog.getByLabelText('Image aliases'), { target: { value: 'custom-iol-*\n alternate-* ' } });
    await fireEvent.click(create);

    expect(await screen.findByTestId('template-card-custom-iol')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(apiRequest).toHaveBeenCalledWith('/library/templates', {
      method: 'POST',
      body: {
        type: 'iol',
        key: 'custom-iol',
        name: 'Custom IOL',
        description: 'Custom router',
        image_aliases: ['custom-iol-*', 'alternate-*'],
      },
      suppressToast: true,
    });
    expect(apiRequest.mock.calls.filter(([path]) => path === '/library/images')).toHaveLength(2);
  });

  it('uploads a YAML file as multipart and keeps the file selected after a validation error', async () => {
    const readApi = apiRequest.getMockImplementation()!;
    const upload = vi.fn()
      .mockRejectedValueOnce(new Error('YAML must define a template type.'))
      .mockResolvedValueOnce({ code: 200, data: {} });
    apiRequest.mockImplementation((path, options) =>
      path === '/library/templates/upload' ? upload(options.body) : readApi(path, options)
    );

    render(LibraryPage);
    await screen.findByTestId('template-card-veos');
    await fireEvent.click(screen.getByRole('button', { name: 'Upload YAML' }));
    const dialog = within(screen.getByRole('dialog', { name: 'Upload YAML' }));
    const confirm = dialog.getByRole('button', { name: 'Upload YAML' });
    expect(confirm).toBeDisabled();
    const file = new File(['type: qemu\nname: Example\n'], 'example.yaml', { type: 'application/yaml' });
    await fireEvent.change(dialog.getByLabelText('Template YAML file'), { target: { files: [file] } });
    await fireEvent.click(confirm);

    expect(await dialog.findByRole('alert')).toHaveTextContent('YAML must define a template type.');
    expect((upload.mock.calls[0][0] as FormData).get('file')).toBe(file);
    expect(confirm).toBeEnabled();
    await fireEvent.click(confirm);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect((upload.mock.calls[1][0] as FormData).get('file')).toBe(file);
  });

  it('confirms removal of custom templates and respects a blocking lab reference', async () => {
    const templates = [...TEMPLATES];
    mockApi(templates);
    const readApi = apiRequest.getMockImplementation()!;
    const deletion = vi.fn()
      .mockRejectedValueOnce(new Error('Template is referenced by a lab node.'))
      .mockImplementationOnce(async () => {
        templates.pop();
        return { code: 200, data: {} };
      });
    apiRequest.mockImplementation((path, options) =>
      path === '/library/templates/qemu/vqfx' && options?.method === 'DELETE'
        ? deletion()
        : readApi(path, options)
    );

    render(LibraryPage);
    await screen.findByTestId('template-card-veos');
    expect(screen.queryByRole('button', { name: 'Delete vEOS-QEMU' })).not.toBeInTheDocument();

    const remove = screen.getByRole('button', { name: 'Delete vQFX' });
    await fireEvent.click(remove);
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(deletion).not.toHaveBeenCalled();

    await fireEvent.click(remove);
    const dialog = within(screen.getByRole('dialog', { name: 'Delete template for vQFX?' }));
    await fireEvent.click(dialog.getByRole('button', { name: 'Delete template' }));
    expect(await dialog.findByRole('alert')).toHaveTextContent('Template is referenced by a lab node.');
    expect(screen.getByTestId('template-card-vqfx')).toBeInTheDocument();

    await fireEvent.click(dialog.getByRole('button', { name: 'Delete template' }));
    await waitFor(() => expect(screen.queryByTestId('template-card-vqfx')).not.toBeInTheDocument());
    expect(apiRequest).toHaveBeenCalledWith('/library/templates/qemu/vqfx', {
      method: 'DELETE',
      suppressToast: true,
    });
  });

  it('reverts a built-in override and leaves the shipped template in the library', async () => {
    const templates: LibraryTemplate[] = [{ ...TEMPLATES[0], source: 'override' }];
    mockApi(templates);
    const readApi = apiRequest.getMockImplementation()!;
    apiRequest.mockImplementation((path, options) => {
      if (path === '/library/templates/qemu/veos' && options?.method === 'DELETE') {
        templates[0] = TEMPLATES[0];
      }
      return readApi(path, options);
    });

    render(LibraryPage);
    await fireEvent.click(await screen.findByRole('button', { name: 'Revert vEOS-QEMU' }));
    const dialog = within(screen.getByRole('dialog', { name: 'Revert override for vEOS-QEMU?' }));
    expect(dialog.getByText(/restore the shipped template settings/i)).toBeInTheDocument();
    await fireEvent.click(dialog.getByRole('button', { name: 'Revert override' }));

    await waitFor(() => expect(screen.queryByRole('button', { name: 'Revert vEOS-QEMU' })).not.toBeInTheDocument());
    expect(screen.getByTestId('template-card-veos')).toBeInTheDocument();
  });

  it('edits a template through the dialog and PATCHes the backend', async () => {
    render(LibraryPage);
    await waitFor(() => screen.getByTestId('template-card-veos'));

    await fireEvent.click(screen.getByTestId('template-edit-veos'));
    expect(screen.getByTestId('template-dialog')).toBeInTheDocument();

    await fireEvent.input(screen.getByTestId('template-dialog-ram'), {
      target: { value: '4096' },
    });
    await fireEvent.click(screen.getByTestId('template-dialog-save'));

    await waitFor(() => {
      const patchCall = apiRequest.mock.calls.find(
        ([path, init]) =>
          path === '/library/templates/qemu/veos' &&
          (init as { method?: string })?.method === 'PATCH'
      );
      expect(patchCall).toBeTruthy();
      const body = (patchCall![1] as { body: Record<string, unknown> }).body;
      expect(body).toEqual({ ram: 4096 });
    });
  });

  it.each([
    { value: '  veos-*\n\n arista-lab-*, custom-veos  ', aliases: ['veos-*', 'arista-lab-*', 'custom-veos'] },
    { value: '', aliases: [] },
  ])('saves the edited image alias list $aliases', async ({ value, aliases }) => {
    render(LibraryPage);
    await fireEvent.click(await screen.findByTestId('template-edit-veos'));

    const editor = screen.getByRole('textbox', { name: 'Image aliases' });
    expect(editor).toHaveValue('veos\narista-lab-*');
    await fireEvent.input(editor, { target: { value } });
    await fireEvent.click(screen.getByTestId('template-dialog-save'));

    await waitFor(() => {
      expect(apiRequest).toHaveBeenCalledWith('/library/templates/qemu/veos', {
        method: 'PATCH',
        body: { image_aliases: aliases },
        suppressToast: true,
      });
    });
  });

  it('loads the configured defaults and sends only the changed template settings', async () => {
    render(LibraryPage);
    await fireEvent.click(await screen.findByTestId('template-edit-veos'));

    const save = screen.getByTestId('template-dialog-save');
    expect(save).toBeDisabled();
    expect(screen.getByLabelText('Interface name format')).toHaveValue('Ethernet{port}');
    expect(screen.getByLabelText('NIC model')).toHaveValue('virtio-net-pci');
    expect(screen.getByLabelText('QEMU version')).toHaveValue('5.2.0');
    expect(screen.getByLabelText('Allow NIC hotplug')).toBeChecked();

    await fireEvent.input(screen.getByLabelText('Description'), { target: { value: '' } });
    await fireEvent.change(screen.getByLabelText('Icon category'), { target: { value: 'switch' } });
    await fireEvent.input(screen.getByLabelText('CPU limit'), { target: { value: '0' } });
    await fireEvent.change(screen.getByLabelText('Primary console'), { target: { value: 'vnc' } });
    await fireEvent.change(screen.getByLabelText('Second console'), { target: { value: 'telnet' } });
    await fireEvent.change(screen.getByLabelText('Interface naming'), { target: { value: 'explicit' } });
    await fireEvent.input(screen.getByRole('textbox', { name: /^Explicit interface names/ }), {
      target: { value: 'Management1\nEthernet1,Ethernet2' },
    });
    await fireEvent.change(screen.getByLabelText('QEMU machine'), { target: { value: 'pc' } });
    expect(screen.getByLabelText('Allow NIC hotplug')).not.toBeChecked();
    expect(screen.getByLabelText('Allow NIC hotplug')).toBeDisabled();
    await fireEvent.input(screen.getByLabelText('Maximum NICs'), { target: { value: '8' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Use default for NIC model' }));
    expect(screen.getByLabelText('NIC model')).toHaveValue('e1000');
    await fireEvent.input(screen.getByLabelText('Extra QEMU arguments'), {
      target: { value: '-nographic' },
    });
    await fireEvent.click(save);

    expect(apiRequest).toHaveBeenCalledWith('/library/templates/qemu/veos', {
      method: 'PATCH',
      suppressToast: true,
      body: {
        description: '',
        icon_type: 'switch',
        cpulimit: 0,
        console_type: 'vnc',
        second_console: 'telnet',
        interface_naming: { explicit: ['Management1', 'Ethernet1', 'Ethernet2'] },
        capabilities: { machine: 'pc', hotplug: false, max_nics: 8 },
        extras: { qemu_nic: null, qemu_options: '-nographic' },
      },
    });
  });

  it('keeps edits and displays the server error when saving fails', async () => {
    render(LibraryPage);
    await fireEvent.click(await screen.findByTestId('template-edit-veos'));

    await fireEvent.input(screen.getByLabelText('Description'), { target: { value: 'Arista switch' } });
    await fireEvent.change(screen.getByLabelText('Interface naming'), { target: { value: 'automatic' } });
    apiRequest.mockRejectedValueOnce(new Error('Template validation failed.'));
    await fireEvent.click(screen.getByTestId('template-dialog-save'));

    expect(await screen.findByRole('alert')).toHaveTextContent('Template validation failed.');
    expect(screen.getByLabelText('Description')).toHaveValue('Arista switch');
    expect(screen.getByLabelText('Interface naming')).toHaveValue('automatic');
    expect(screen.getByTestId('template-dialog-save')).toBeEnabled();

    await fireEvent.click(screen.getByTestId('template-dialog-save'));
    await waitFor(() => expect(screen.queryByTestId('template-dialog')).not.toBeInTheDocument());
    expect(apiRequest).toHaveBeenCalledWith('/library/templates/qemu/veos', {
      method: 'PATCH',
      suppressToast: true,
      body: { description: 'Arista switch', interface_naming: null },
    });
  });

  it('edits Docker environment variables without changing engine defaults', async () => {
    mockApi([{
      ...TEMPLATES[0],
      type: 'docker',
      key: 'docker',
      interface_naming: null,
      capabilities: { machine: null, hotplug: true, max_nics: 16 },
      extras: { environment: [{ key: 'LANG', value: 'C' }] },
      extras_schema: [
        { key: 'environment', label: 'Environment variables', type: 'env', default: [] },
        { key: 'vnc_port', label: 'VNC port', type: 'number', default: 5900 },
      ],
    }]);
    render(LibraryPage);
    await fireEvent.click(await screen.findByTestId('template-edit-docker'));

    expect(screen.queryByLabelText('QEMU machine')).not.toBeInTheDocument();
    expect(screen.getByLabelText('VNC port')).toHaveValue(5900);
    expect(screen.getByLabelText('Variable name 1')).toHaveValue('LANG');
    await fireEvent.click(screen.getByRole('button', { name: 'Add variable' }));
    await fireEvent.input(screen.getByLabelText('Variable name 2'), { target: { value: 'TZ' } });
    await fireEvent.input(screen.getByLabelText('Variable value 2'), { target: { value: 'UTC' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Remove variable 1' }));
    await fireEvent.click(screen.getByTestId('template-dialog-save'));

    expect(apiRequest).toHaveBeenCalledWith('/library/templates/docker/docker', {
      method: 'PATCH',
      suppressToast: true,
      body: { extras: { environment: [{ key: 'TZ', value: 'UTC' }] } },
    });
  });
});
