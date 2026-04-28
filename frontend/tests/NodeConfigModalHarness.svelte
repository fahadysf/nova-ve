<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import NodeConfigModal from '$lib/components/canvas/NodeConfigModal.svelte';
  import type { NodeCatalog, NodeData } from '$lib/types';

  export let open = true;
  export let mode: 'create' | 'edit' = 'create';
  export let catalog: NodeCatalog;
  export let node: NodeData | null = null;

  let submitPayload = '';

  function handleSubmit(event: CustomEvent) {
    submitPayload = JSON.stringify(event.detail);
  }
</script>

<NodeConfigModal {open} {mode} {catalog} {node} on:submit={handleSubmit} />
<output data-testid="submit-payload">{submitPayload}</output>
