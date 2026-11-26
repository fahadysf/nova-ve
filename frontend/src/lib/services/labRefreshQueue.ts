type RefreshOptions = {
  blocking?: boolean;
  reconcileRuntime?: boolean;
};

export function createLabRefreshQueue(refresh: (options: RefreshOptions) => Promise<void>) {
  let pending: RefreshOptions | undefined;
  let active: Promise<void> | undefined;
  let closed = false;

  async function run() {
    try {
      while (pending) {
        const options = pending;
        pending = undefined;
        await refresh(options);
      }
    } finally {
      active = undefined;
    }
  }

  return {
    request(options: RefreshOptions) {
      if (closed) return Promise.resolve();
      pending = {
        blocking: pending?.blocking || options.blocking,
        reconcileRuntime: pending?.reconcileRuntime || options.reconcileRuntime,
      };
      active ??= Promise.resolve().then(run);
      return active;
    },

    close() {
      closed = true;
      pending = undefined;
    },
  };
}
