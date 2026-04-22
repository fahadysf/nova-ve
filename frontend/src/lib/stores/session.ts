import { writable } from 'svelte/store';
import type { UserRead } from '$lib/types';

export interface AuthState {
  user: UserRead | null;
  loading: boolean;
  authenticated: boolean;
}

function createAuthStore() {
  const { subscribe, set, update } = writable<AuthState>({
    user: null,
    loading: true,
    authenticated: false
  });

  return {
    subscribe,
    setUser: (user: UserRead | null) =>
      update((state) => ({
        ...state,
        user,
        authenticated: !!user,
        loading: false
      })),
    clear: () => set({ user: null, loading: false, authenticated: false }),
    setLoading: (loading: boolean) => update((state) => ({ ...state, loading }))
  };
}

export const authStore = createAuthStore();
