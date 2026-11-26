// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import DeleteConfirmDialogHarness from './DeleteConfirmDialogHarness.svelte';

describe('DeleteConfirmDialog (#338)', () => {
  it('warns that disks and data are deleted immediately and may not be recoverable', () => {
    render(DeleteConfirmDialogHarness);

    expect(screen.getByTestId('node-delete-confirm-dialog')).toBeTruthy();
    expect(screen.getByText('Delete node "r1"?')).toBeTruthy();
    expect(
      screen.getByText('Its disks and data will be deleted immediately and may not be recoverable.')
    ).toBeTruthy();
  });

  it('cancel emits cancel and never confirm', async () => {
    render(DeleteConfirmDialogHarness);

    await fireEvent.click(screen.getByTestId('node-delete-confirm-cancel'));

    expect(screen.getByTestId('last-action').textContent).toBe('cancel');
  });

  it('delete emits confirm', async () => {
    render(DeleteConfirmDialogHarness);

    await fireEvent.click(screen.getByTestId('node-delete-confirm-confirm'));

    expect(screen.getByTestId('last-action').textContent).toBe('confirm');
  });

  it('uses a reddish outline for Delete and the normal theme for Cancel', () => {
    render(DeleteConfirmDialogHarness);

    const deleteButton = screen.getByTestId('node-delete-confirm-confirm');
    const cancelButton = screen.getByTestId('node-delete-confirm-cancel');
    expect(deleteButton.className).toContain('border-red-500/60');
    expect(deleteButton.className).toContain('text-red-200');
    expect(deleteButton.className).not.toContain('bg-red-600');
    expect(cancelButton.className).toContain('border-gray-700');
    expect(cancelButton.className).toContain('text-gray-300');
  });
});
