import { describe, expect, it } from 'vitest';
import { frameFits, frameRowPosition, networkFrameHeight, networkFrameRows } from '../src/lib/services/networkFrame';
import type { Link, NodeData } from '../src/lib/types';

describe('network frame projection', () => {
  const nodes = { '1': { id: 1, name: 'router', left: 10, interfaces: [{ index: 1, name: 'ge-0/0/1' }] } as NodeData };
  it('sorts rendered links and does not invent rows for unsupported endpoints or mutate nodes', () => {
    const links = [
      { id: '10', from: { node_id: 1, interface_index: 1 }, to: { network_id: 2 } },
      { id: '2', from: { node_id: 1, interface_index: 1 }, to: { network_id: 2 } },
      { id: '4', from: { network_id: 2 }, to: { node_id: 1, interface_index: 1 } },
      { id: '3', from: { node_id: 1 }, to: { network_id: 3 } },
    ] as Link[];
    expect(networkFrameRows(2, links, nodes)).toEqual([
      { linkId: '2', label: 'router · ge-0/0/1', peerX: 10 },
      { linkId: '10', label: 'router · ge-0/0/1', peerX: 10 },
    ]);
    expect(nodes['1'].left).toBe(10);
  });
  it('bounds height and aligns row anchors within the frame', () => {
    expect(networkFrameHeight(20)).toBe(57 + 7 * 28);
    expect(frameRowPosition(0, networkFrameHeight(2), 'left')).toEqual({ side: 'left', offset: 70 / 113 });
  });
  it('rejects overlap without changing stored positions', () => {
    const frame = { x: 100, y: 100, w: 210, h: 140 };
    expect(frameFits(frame, [{ x: 305, y: 200, w: 30, h: 30 }])).toBe(false);
    expect(frameFits(frame, [{ x: 310, y: 100, w: 30, h: 30 }])).toBe(true);
  });
});
