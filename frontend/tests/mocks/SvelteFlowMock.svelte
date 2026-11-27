<script lang="ts">
  // Spread-through so canvas props render as attributes in tests.
  let { ...rest } = $props();

  // Legacy-syntax parents pass ``on:foo`` handlers through the component
  // events channel ($$events), never through $props() — so a spread cannot
  // forward them. Tests instead dispatch a ``mock-flow-event`` DOM
  // CustomEvent ({ type, detail }) on the mock root; this bridge
  // re-dispatches it as a real component event the canvas can handle.
  import { createEventDispatcher } from 'svelte';
  const dispatch = createEventDispatcher();

  function bridgeFlowEvents(node: HTMLElement) {
    const handler = (event: Event) => {
      const { type, detail } = (event as CustomEvent<{ type: string; detail: unknown }>).detail;
      dispatch(type, detail);
    };
    node.addEventListener('mock-flow-event', handler);
    return {
      destroy: () => node.removeEventListener('mock-flow-event', handler),
    };
  }

  // Test summaries of the nodes/edges props: id + class only (node data
  // carries DOM refs, so a full JSON dump would throw on circular refs).
  // The canvas passes STORES — a plain attribute expression would compute
  // once from the stable store object, so subscribe to keep the summary
  // live as the store republishes.
  function summarize(items: unknown) {
    const list = Array.isArray(items) ? items : [];
    return JSON.stringify(list.map((item: { id: string; class?: string }) => ({ id: item.id, class: item.class ?? null })));
  }

  // $state: the mock is runes mode — plain ``let`` assignments in
  // the subscribe callbacks below would not invalidate the template.
  let nodeSummary = $state('[]');
  let edgeSummary = $state('[]');
  let nodePublications = $state(0);
  let edgePublications = $state(0);
  if (typeof (rest.nodes as { subscribe?: unknown } | undefined)?.subscribe === 'function') {
    (rest.nodes as { subscribe: (run: (value: unknown) => void) => void }).subscribe((items) => {
      nodeSummary = summarize(items);
      nodePublications += 1;
    });
  } else {
    nodeSummary = summarize(rest.nodes);
  }
  if (typeof (rest.edges as { subscribe?: unknown } | undefined)?.subscribe === 'function') {
    (rest.edges as { subscribe: (run: (value: unknown) => void) => void }).subscribe((items) => {
      edgeSummary = summarize(items);
      edgePublications += 1;
    });
  } else {
    edgeSummary = summarize(rest.edges);
  }
</script>

<div
  data-testid="svelte-flow-mock"
  data-flow-nodes={nodeSummary}
  data-flow-edges={edgeSummary}
  data-node-publications={nodePublications}
  data-edge-publications={edgePublications}
  {...rest}
  use:bridgeFlowEvents
>
  <slot />
</div>
