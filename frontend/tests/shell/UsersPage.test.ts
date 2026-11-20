// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import UsersPage from '../../src/routes/(shell)/manage/users/+page.svelte';

const apiRequest = vi.fn();

vi.mock('$lib/api', () => ({
  apiRequest: (...args: unknown[]) => apiRequest(...args),
}));

const USERS: Record<string, unknown> = {
  admin: {
    username: 'admin',
    email: 'admin@example.com',
    name: 'Administrator',
    role: 'admin',
    extauth: 'internal',
    online: 1,
    pod: 0,
    cpu: -1,
    ram: -1,
    folder: '/',
    datestart: 1735689600,
    expiration: -1,
  },
  trainee: {
    username: 'trainee',
    email: 'trainee@example.com',
    name: 'Trainee',
    role: 'user',
    extauth: 'internal',
    online: 0,
    pod: 3,
    cpu: 4,
    ram: 8192,
    folder: '/trainee',
    datestart: -1,
    expiration: 1798761600,
  },
};

describe('User management page (#261)', () => {
  beforeEach(() => {
    apiRequest.mockReset();
    apiRequest.mockImplementation(async (path: string) => {
      if (path === '/users/') {
        return { code: 200, status: 'success', message: '', data: USERS };
      }
      return { code: 200, status: 'success', message: '', data: {} };
    });
  });

  it('lists accounts with role, tenant, quotas, and lifecycle dates', async () => {
    render(UsersPage);

    await waitFor(() => {
      expect(screen.getByTestId('users-row-trainee')).toBeInTheDocument();
    });

    expect(screen.getByText(/Administrator · admin@example\.com/)).toBeInTheDocument();
    expect(screen.getByText(/8192 MB\s*\/\s*4/)).toBeInTheDocument();
    expect(screen.getByText('/trainee')).toBeInTheDocument();
    expect(screen.getByText('2027-01-01')).toBeInTheDocument();
  });

  it('opens the create dialog and posts the payload', async () => {
    render(UsersPage);
    await waitFor(() => screen.getByTestId('users-add'));

    await fireEvent.click(screen.getByTestId('users-add'));
    expect(screen.getByTestId('user-dialog')).toBeInTheDocument();

    await fireEvent.input(screen.getByTestId('user-dialog-username'), {
      target: { value: 'newuser' },
    });
    await fireEvent.input(screen.getByTestId('user-dialog-password'), {
      target: { value: 'secret123' },
    });
    await fireEvent.click(screen.getByTestId('user-dialog-save'));

    await waitFor(() => {
      const createCall = apiRequest.mock.calls.find(
        ([path, init]) => path === '/users/' && (init as { method?: string })?.method === 'POST'
      );
      expect(createCall).toBeTruthy();
      expect((createCall![1] as { body: Record<string, unknown> }).body.username).toBe('newuser');
    });
  });

  it('opens edit with existing values and PATCHes the row', async () => {
    render(UsersPage);
    await waitFor(() => screen.getByTestId('users-edit-trainee'));

    await fireEvent.click(screen.getByTestId('users-edit-trainee'));
    const usernameInput = screen.getByTestId('user-dialog-username') as HTMLInputElement;
    expect(usernameInput.value).toBe('trainee');
    expect(usernameInput.disabled).toBe(true);

    await fireEvent.click(screen.getByTestId('user-dialog-save'));

    await waitFor(() => {
      const patchCall = apiRequest.mock.calls.find(
        ([path, init]) => path === '/users/trainee' && (init as { method?: string })?.method === 'PATCH'
      );
      expect(patchCall).toBeTruthy();
    });
  });

  it('deletes a user through the confirm dialog', async () => {
    render(UsersPage);
    await waitFor(() => screen.getByTestId('users-delete-trainee'));

    await fireEvent.click(screen.getByTestId('users-delete-trainee'));
    expect(screen.getByTestId('user-delete-confirm')).toBeInTheDocument();
    await fireEvent.click(screen.getByTestId('user-delete-confirm-button'));

    await waitFor(() => {
      const deleteCall = apiRequest.mock.calls.find(
        ([path, init]) => path === '/users/trainee' && (init as { method?: string })?.method === 'DELETE'
      );
      expect(deleteCall).toBeTruthy();
    });
  });
});
