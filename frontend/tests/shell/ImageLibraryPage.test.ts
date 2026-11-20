// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from '@testing-library/svelte';
import { fireEvent } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import ImagesPage from '../../src/routes/(shell)/library/images/+page.svelte';
import { imageMatchesPattern } from '$lib/services/imageLibraryPreview';

describe('Images & Templates library page (#264, preview)', () => {
  it('renders template cards grouped by type with their matching on-box images', () => {
    render(ImagesPage);

    expect(screen.getByText('vEOS-QEMU')).toBeInTheDocument();
    expect(screen.getByText('veos-4.31.3F.qcow2')).toBeInTheDocument();
    // Missing-image template state is explicit.
    expect(screen.getByText(/No image matches/i)).toBeInTheDocument();
  });

  it('switches to the images tab and lists size, checksum, and lab references', async () => {
    render(ImagesPage);
    await fireEvent.click(screen.getByTestId('images-tab-images'));

    expect(screen.getByTestId('images-panel')).toBeInTheDocument();
    expect(screen.getByText('vyos-1.5-rolling.qcow2')).toBeInTheDocument();
    expect(screen.getByText('1.2 GB')).toBeInTheDocument();
    // Reference chips render lab file names, not raw paths.
    expect(screen.getAllByText('arc-lab.json').length).toBeGreaterThanOrEqual(1);
    // Multiple images reference ST-Demo — chips repeat per image row.
    expect(screen.getAllByText('ST-Demo.json').length).toBeGreaterThanOrEqual(2);
    // Missing image state surfaces.
    expect(screen.getByText('missing')).toBeInTheDocument();
  });

  it('filters images by search query', async () => {
    render(ImagesPage);
    await fireEvent.click(screen.getByTestId('images-tab-images'));

    await fireEvent.input(screen.getByTestId('images-search'), {
      target: { value: 'vyos' },
    });
    expect(screen.getByText('vyos-1.5-rolling.qcow2')).toBeInTheDocument();
    expect(screen.queryByText('veos-4.31.3F.qcow2')).toBeNull();
  });
});

describe('imageMatchesPattern (#264)', () => {
  it('matches prefix, suffix, and exact patterns', () => {
    expect(imageMatchesPattern('veos-4.31.3F.qcow2', 'veos-*.qcow2')).toBe(true);
    expect(imageMatchesPattern('veos-4.31.3F.qcow2', 'alpine:*')).toBe(false);
    expect(imageMatchesPattern('alpine:3.20', 'alpine:*')).toBe(true);
    expect(imageMatchesPattern('exact.bin', 'exact.bin')).toBe(true);
  });
});
