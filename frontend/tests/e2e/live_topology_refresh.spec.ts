import { expect, test, type WebSocketRoute } from '@playwright/test';

test.use({ baseURL: process.env.FRONTEND_URL ?? 'http://localhost:5173' });

test('an open lab refreshes remote topology and preserves a pending layout on resnapshot', async ({ page }) => {
  const lab = 'team/refresh.json';
  const labApi = `/api/labs/${lab}`;
  let revision = 1;
  let socket: WebSocketRoute | undefined;
  let reconnect = false;
  let reconcileRequests = 0;
  let layoutRequests = 0;
  let nodeReads = 0;
  let links: unknown[] = [];
  let nodesReadGate: Promise<void> | undefined;
  let releaseLayout!: () => void;
  const heldLayout = new Promise<void>((resolve) => { releaseLayout = resolve; });

  function node(id: number, name: string, left: number, top: number) {
    return {
      id, name, left, top, type: 'qemu', template: 'router', image: 'router',
      status: 0, console: 'telnet', icon: 'Router.png', ethernet: 1,
      interfaces: [{ index: 0, name: 'eth0' }],
    };
  }
  const nodes = { '1': node(1, 'Local router', 110, 110) } as Record<string, ReturnType<typeof node>>;
  let networks = {};

  await page.routeWebSocket('**/ws/labs/**', (connection) => {
    socket = connection;
    connection.send(JSON.stringify({ type: 'hello', seq: reconnect ? 0 : 1 }));
    if (reconnect) {
      connection.send(JSON.stringify({ type: 'force_resnapshot' }));
      connection.send(JSON.stringify({ type: 'lab_topology', seq: 0, payload: { id: 'refresh-fixture' } }));
    }
  });

  await page.route('**/api/**', async (route) => {
    const path = decodeURIComponent(new URL(route.request().url()).pathname);
    let data: unknown;
    if (path === '/api/auth') {
      data = { id: 1, username: 'fixture', name: 'Fixture', role: 'admin', html5: true, folder: '/' };
    } else if (path === labApi) {
      data = { id: 'refresh-fixture', name: 'Refresh lab', filename: lab, path: `/${lab}`, revision };
    } else if (path === `${labApi}/nodes`) {
      nodeReads += 1;
      data = structuredClone(nodes);
      await nodesReadGate;
    } else if (path === `${labApi}/networks`) {
      data = networks;
    } else if (path === `${labApi}/links`) {
      data = links;
    } else if (path === `${labApi}/runtime/reconcile`) {
      reconcileRequests += 1;
      data = {};
    } else if (path === `${labApi}/layout` && route.request().method() === 'PUT') {
      layoutRequests += 1;
      await heldLayout;
      await route.fulfill({ json: { code: 200, status: 'success', revision: ++revision } });
      return;
    } else {
      data = [];
    }
    await route.fulfill({ json: { code: 200, status: 'success', data } });
  });

  const flowNode = (name: string) => page.locator('.svelte-flow__node').filter({ hasText: name });
  const transform = (name: string) => flowNode(name).evaluate((element) => element.style.transform);
  function publish(type: string, seq: number, payload = {}) {
    socket?.send(JSON.stringify({ type, seq, payload }));
  }

  try {
    await page.goto(`/labs/${lab}`);
    await expect(flowNode('Local router')).toBeVisible();
    await expect.poll(() => !!socket).toBe(true);

    revision += 1;
    nodes['2'] = node(2, 'Remote router', 340, 190);
    publish('node_created', 2);
    await expect(flowNode('Remote router')).toBeVisible();

    revision += 1;
    networks = { '1': { id: 1, name: 'Remote network', type: 'bridge', left: 570, top: 220, visibility: true } };
    publish('network_created', 3);
    await expect(flowNode('Remote network')).toBeVisible();

    const link = {
      id: 'lnk_1', from: { node_id: 2, interface_index: 0 }, to: { network_id: 1 },
      label: '', color: '', width: '1'
    };
    const edge = page.locator('.svelte-flow__edge');
    revision += 1;
    links = [link];
    publish('link_created', 4, { link });
    await expect(edge).toHaveCount(1);

    let releaseNodes!: () => void;
    nodesReadGate = new Promise<void>((resolve) => { releaseNodes = resolve; });
    const readsBeforeDelete = nodeReads;
    try {
      const oldLinksRead = page.waitForResponse((response) =>
        new URL(response.url()).pathname === `${labApi}/links`
      );
      publish('topology_updated', 5);
      await oldLinksRead;
      await expect.poll(() => nodeReads).toBe(readsBeforeDelete + 1);
      revision += 1;
      links = [];
      publish('link_deleted', 6, { link_id: link.id });
      await expect(edge).toHaveCount(0);
    } finally {
      nodesReadGate = undefined;
      releaseNodes();
    }
    await expect.poll(() => nodeReads).toBeGreaterThan(readsBeforeDelete + 1);
    await expect(edge).toHaveCount(0);

    const remoteBefore = await transform('Remote router');
    revision += 1;
    nodes['2'].left = 470;
    publish('layout_updated', 7);
    await expect.poll(() => transform('Remote router')).not.toBe(remoteBefore);

    const initialPosition = await transform('Local router');
    const box = await flowNode('Local router').getByTestId('custom-node-card').boundingBox();
    expect(box).not.toBeNull();
    const x = box!.x + box!.width / 2;
    const y = box!.y + box!.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + 90, y + 70, { steps: 5 });
    const draggingPosition = await transform('Local router');
    expect(draggingPosition).not.toBe(initialPosition);
    revision += 1;
    nodes['2'].name = 'Renamed during drag';
    const remoteRead = page.waitForResponse((response) =>
      new URL(response.url()).pathname === `${labApi}/nodes`
    );
    publish('node_updated', 8);
    await remoteRead;
    expect(await transform('Local router')).toBe(draggingPosition);
    await page.mouse.up();
    await expect(flowNode('Renamed during drag')).toBeVisible();
    await expect.poll(() => layoutRequests).toBe(1);
    const pendingPosition = await transform('Local router');
    expect(pendingPosition).not.toBe(initialPosition);

    reconnect = true;
    revision += 1;
    nodes['3'] = node(3, 'Added while disconnected', 240, 330);
    socket?.close();
    await expect(flowNode('Added while disconnected')).toBeVisible();
    expect(await transform('Local router')).toBe(pendingPosition);
    expect(reconcileRequests).toBe(1);
  } finally {
    releaseLayout();
  }
});
