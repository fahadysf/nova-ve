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
</script>

<div data-testid="svelte-flow-mock" {...rest} use:bridgeFlowEvents>
  <slot />
</div>
