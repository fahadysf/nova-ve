import { apiRequest } from '$lib/api';
import { authStore } from '$lib/stores/session';
import type { AuthLoginResponse, UserRead } from '$lib/types';

export { authStore } from '$lib/stores/session';

export async function checkAuth() {
  try {
    const response = await apiRequest<UserRead>('/auth', { suppressToast: true });
    if (response.data) {
      authStore.setUser(response.data);
    } else {
      authStore.clear();
    }
  } catch (_error) {
    authStore.clear();
  }
}

export async function login(username: string, password: string) {
  try {
    const response = await apiRequest<AuthLoginResponse>('/auth/login', {
      method: 'POST',
      body: { username, password },
      suppressToast: true
    });
    authStore.setUser(response.data);
    return { success: true };
  } catch (error) {
    return {
      success: false,
      message: error instanceof Error ? error.message : 'Login failed'
    };
  }
}

export async function logout() {
  try {
    await apiRequest('/auth/logout', { method: 'POST', suppressToast: true });
  } finally {
    authStore.clear();
  }
}
