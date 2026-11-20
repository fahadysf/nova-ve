<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import TemplateDialog from '$lib/components/manage/TemplateDialog.svelte';
  import type { LibraryTemplate, LibraryTemplateEdit } from '$lib/types';

  export let template: LibraryTemplate;

  let savedEdit = '';

  function handleSave(event: CustomEvent<{ template: LibraryTemplate; edit: LibraryTemplateEdit }>) {
    savedEdit = JSON.stringify(event.detail.edit);
  }
</script>

<TemplateDialog {template} on:save={handleSave} />
<output data-testid="saved-edit">{savedEdit}</output>
