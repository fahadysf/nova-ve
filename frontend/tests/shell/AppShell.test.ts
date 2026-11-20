// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import AppShell from '$lib/components/shell/AppShell.svelte';
import { isNavActive } from '$lib/components/shell/nav';
import { authStore } from '$lib/stores/session';
import type { UserRead } from '$lib/types';


function setUser(role: string): void {
  authStore.setUser({
    username: role === 'admin' ? 'admin' : 'uicheck',
    name: role === 'admin' ? 'Administrator' : 'UI Check',
    role,
    email: `${role}@nova-ve.local`,
  } as unknown as UserRead);
}

afterEach(() => {
  authStore.clear();
});

describe('AppShell (#260)', () => {
  it('renders the compact top bar with user + role and the labs nav', () => {
    setUser('admin');
    render(AppShell, { props: { pathname: '/labs' } });

    expect(screen.getByTestId('app-topbar')).toBeInTheDocument();
    expect(screen.getByTestId('app-logout')).toBeInTheDocument();
    expect(screen.getByText('admin')).toBeInTheDocument();
    expect(screen.getByTestId('nav-labs')).toBeInTheDocument();
  });

  it('shows management, library, and system sections to admins', () => {
    setUser('admin');
    render(AppShell, { props: { pathname: '/labs' } });

    expect(screen.getByTestId('nav-users')).toBeInTheDocument();
    expect(screen.getByTestId('nav-running-labs')).toBeInTheDocument();
    expect(screen.getByTestId('nav-running-nodes')).toBeInTheDocument();
    expect(screen.getByTestId('nav-images-templates')).toBeInTheDocument();
    expect(screen.getByTestId('nav-status')).toBeInTheDocument();
    expect(screen.getByTestId('nav-logs')).toBeInTheDocument();
    expect(screen.getByTestId('nav-settings')).toBeInTheDocument();
  });

  it('hides admin-only sections from regular users', () => {
    setUser('user');
    render(AppShell, { props: { pathname: '/labs' } });

    expect(screen.getByTestId('nav-labs')).toBeInTheDocument();
    expect(screen.queryByTestId('nav-users')).toBeNull();
    expect(screen.queryByTestId('nav-images-templates')).toBeNull();
    expect(screen.queryByTestId('nav-status')).toBeNull();
  });

  it('marks the current route active and planned entries with badges', () => {
    setUser('admin');
    render(AppShell, { props: { pathname: '/manage/users' } });

    const active = screen.getByTestId('nav-users');
    expect(active).toHaveAttribute('aria-current', 'page');
    expect(screen.getByTestId('nav-labs')).not.toHaveAttribute('aria-current');
    expect(screen.getAllByText('planned').length).toBeGreaterThan(0);
  });
});

describe('isNavActive (#260)', () => {
  it('keeps Labs lit for editor-deep routes', () => {
    expect(isNavActive('/labs/st-demo/ST-Demo.json', '/labs')).toBe(true);
    expect(isNavActive('/labs', '/labs')).toBe(true);
  });

  it('matches exact and nested paths, not partial prefixes', () => {
    expect(isNavActive('/manage/users', '/manage/users')).toBe(true);
    expect(isNavActive('/manage/users/x', '/manage/users')).toBe(true);
    expect(isNavActive('/manage/users-x', '/manage/users')).toBe(false);
    expect(isNavActive('/system/logs', '/system/status')).toBe(false);
  });
});
