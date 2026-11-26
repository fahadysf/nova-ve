<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import DeleteConfirmDialog from '$lib/components/DeleteConfirmDialog.svelte';

  export let title = 'Delete node "r1"?';
  export let message = 'Its disks and data will be deleted immediately and may not be recoverable.';

  let lastAction = 'none';

  function handleConfirm() {
    lastAction = 'confirm';
  }

  function handleCancel() {
    lastAction = 'cancel';
  }
</script>

<DeleteConfirmDialog {title} {message} confirmLabel="Delete node" testId="node-delete-confirm" on:confirm={handleConfirm} on:cancel={handleCancel} />
<output data-testid="last-action">{lastAction}</output>
