<script lang="ts">
  // Spread-through so canvas event handlers (on:nodedragstop etc.) bind to
  // the mock root and component-event dispatches reach them in tests.
  let { ...rest } = $props();
</script>

<div data-testid="svelte-flow-mock" {...rest}>
  <slot />
</div>
