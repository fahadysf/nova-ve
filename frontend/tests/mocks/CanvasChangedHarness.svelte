<script lang="ts">
  // Test harness: records the canvas's ``changed`` events. Legacy ``on:foo``
  // handlers reach a legacy-syntax component only through a template, not
  // through props passed to render().
  import TopologyCanvas from '$lib/components/canvas/TopologyCanvas.svelte';

  export let labId: string;
  export let reasons: string[] = [];
  export let snapshotNeeded: () => void = () => {};
</script>

<TopologyCanvas
  {labId}
  consoleSelectorWindows={[]}
  consoleMinimizedWindows={[]}
  on:changed={(event) => reasons.push(event.detail.reason)}
  on:snapshot-needed={snapshotNeeded}
/>
