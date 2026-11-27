import type { NodeCatalog } from '$lib/types';

export type NodeCatalogLoaderOptions = {
  load: () => Promise<NodeCatalog>;
  onCatalog: (catalog: NodeCatalog) => void;
  onError?: (error: unknown) => void;
  retryDelayMs?: number;
};

export type NodeCatalogLoader = {
  refresh: () => void;
  close: () => void;
};

export function createNodeCatalogLoader(
  options: NodeCatalogLoaderOptions
): NodeCatalogLoader {
  const retryDelayMs = options.retryDelayMs ?? 2000;
  const fallbackDelayMs = 60_000;
  let closed = false;
  let inFlight = false;
  let queued = false;
  let requestedVersion = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  function clearRetry() {
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
  }

  async function run(version: number): Promise<void> {
    inFlight = true;
    queued = false;
    let nextRefreshDelayMs = fallbackDelayMs;
    try {
      const catalog = await options.load();
      if (catalog.pending === true) {
        nextRefreshDelayMs = retryDelayMs;
      }
      if (!closed && version === requestedVersion) {
        options.onCatalog(catalog);
      }
    } catch (error) {
      nextRefreshDelayMs = retryDelayMs;
      if (!closed && version === requestedVersion) {
        options.onError?.(error);
      }
    } finally {
      inFlight = false;
      if (closed) return;
      if (queued || version !== requestedVersion) {
        queued = false;
        void run(requestedVersion);
      } else {
        clearRetry();
        retryTimer = setTimeout(() => {
          retryTimer = null;
          refresh();
        }, nextRefreshDelayMs);
      }
    }
  }

  function refresh(): void {
    if (closed) return;
    clearRetry();
    requestedVersion += 1;
    if (inFlight) {
      queued = true;
      return;
    }
    void run(requestedVersion);
  }

  return {
    refresh,
    close() {
      closed = true;
      queued = false;
      requestedVersion += 1;
      clearRetry();
    },
  };
}
