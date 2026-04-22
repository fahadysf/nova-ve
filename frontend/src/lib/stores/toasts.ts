import { writable } from 'svelte/store';

export interface ToastMessage {
  id: number;
  kind: 'error' | 'success' | 'info';
  message: string;
}

function createToastStore() {
  const { subscribe, update } = writable<ToastMessage[]>([]);

  return {
    subscribe,
    push(message: string, kind: ToastMessage['kind'] = 'error', timeout = 4000) {
      const id = Date.now() + Math.floor(Math.random() * 1000);
      update((items) => [...items, { id, kind, message }]);
      if (timeout > 0) {
        setTimeout(() => {
          update((items) => items.filter((item) => item.id !== id));
        }, timeout);
      }
    },
    remove(id: number) {
      update((items) => items.filter((item) => item.id !== id));
    }
  };
}

export const toastStore = createToastStore();
