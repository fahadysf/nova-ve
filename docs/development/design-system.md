# nova-ve Design System Adoption Architecture

## Purpose

This document turns the handoff bundle in `./tmp/nova-ve Design System-handoff.zip`
into an implementation-facing architecture for aligning the current frontend with the
handed-off design system while preserving later accepted UI decisions already merged
after the handoff was exported.

## Source Bundle

Primary source files:
- `tmp/nova-ve-design-system/project/README.md`
- `tmp/nova-ve-design-system/project/colors_and_type.css`
- `tmp/nova-ve-design-system/project/ui_kits/topology-editor/*`
- `tmp/nova-ve-design-system/project/preview/*`

The handoff is authoritative for:
- visual tokens
- typography rules
- spacing/radius/shadow vocabulary
- component styling direction
- route-level styling targets for login, labs index, lab editor, canvas, inventory, menu, console, and toasts

## Authority And Conflict Ledger

Use this ledger to resolve conflicts without re-litigating the handoff during execution.

| Topic | Winning source | Decision |
|---|---|---|
| Color, spacing, radii, shadow, typography direction | Handoff bundle | Adopt from `colors_and_type.css` and related specimens. |
| Login / labs-browser visual chrome | Handoff bundle | Align current routes to the handed-off visual system. |
| Canvas node, menu, toast, and console visual chrome | Handoff bundle | Adopt visual treatment while preserving current behavior. |
| Lab editor overall shell structure | Live product | Preserve the merged single rail rather than restoring the older three-pane shell. |
| Add launcher position and affordance | Live product | Preserve the separate large circular `+` beside the bottom-left controls. |
| Canvas lock semantics | Live product | Preserve the explicit lock behavior that disables pan, zoom, dragging, and selection/connect interactions. |
| Light mode scope | Live product planning decision | Defer light mode; this adoption pass is dark-mode only. |
| Add launcher wording conflict | Live product planning decision | Any older bottom-right wording in legacy issue artifacts is superseded by the current bottom-left launcher requirement. |

## Token And Font Mapping

### Fonts
- UI sans: `Inter Tight`
- UI mono: `JetBrains Mono`
- Keep existing fallback stacks behind the self-hosted variable fonts.
- Planned asset destination for execution:
  - `frontend/static/fonts/design-system/InterTight-Variable.woff2`
  - `frontend/static/fonts/design-system/JetBrainsMono-Variable.woff2`

### Semantic token mapping
- app background -> `gray-900`
- chrome/card surface -> `gray-800`
- canvas/iframe surface -> `gray-950`
- border default -> `gray-700`
- border subtle -> `gray-800`
- accent/focus/selection -> `blue-500`
- running/saved -> emerald
- stopped/inactive -> gray
- network -> blue
- destructive/error -> red

### Typography mapping
- eyebrows / section labels -> uppercase, wide tracking, 9px to 10px
- body copy -> dense 13px to 14px
- mono metadata -> JetBrains Mono for paths, IDs, console text, and other operator identifiers

## Canonical Design Rules Extracted From The Handoff

### Brand posture
- Operator-grade technical surface for network engineers.
- Dense, utilitarian, dark-by-default.
- Decoration near-zero; semantic weight is carried by border, pill, eyebrow, and typography.

### Typography
- `Inter Tight` for UI copy.
- `JetBrains Mono` for identifiers, paths, IDs, and console output.
- Uppercase eyebrows with wide tracking are the primary section-navigation device.

### Color and surfaces
- `gray-900` app background, `gray-800` chrome, `gray-950` canvas/terminal surface.
- `blue-500` is the single accent for focus/selection/primary outline.
- Semantic states:
  - emerald = running/saved
  - gray = stopped/inactive
  - blue = network/selection
  - red = destructive/error

### Layout and density
- Tight spacing on a 4px grid.
- Border-first visual hierarchy, almost no shadow.
- Compact button sizing and compact nested cards.
- Dense canvas nodes and inventory rows.

### Component direction
- Buttons: 9px uppercase tracked labels, compact padding, variant-driven border/fill.
- Canvas nodes: larger, more legible labels than the current 7px state; explicit icon/status treatment.
- Inventory items: compact nested card rows with denser actions and richer metadata.
- Console: restrained chrome with traffic lights, tabs, and tighter control bar.
- Toasts: translucent semantic panels with uppercase close control.

## Current Frontend Touchpoints

Token and base layer:
- `frontend/src/app.css`

Route surfaces:
- `frontend/src/routes/login/+page.svelte`
- `frontend/src/routes/labs/+page.svelte`
- `frontend/src/routes/labs/[...id]/+page.svelte`

Canvas and component surfaces:
- `frontend/src/lib/components/canvas/TopologyCanvas.svelte`
- `frontend/src/lib/components/canvas/CustomNode.svelte`
- `frontend/src/lib/components/canvas/NetworkNode.svelte`
- `frontend/src/lib/components/ToastStack.svelte`

## Key Divergences To Preserve

The handoff’s lab editor reflects an older three-pane editor with a persistent top-left
palette. The live product has already moved beyond that in accepted work merged after
the handoff was produced.

Preserve these later decisions:
- the lab editor uses a single compact left rail instead of separate summary and inventory rails
- the add launcher is a separate large circular `+` button near the bottom-left canvas controls
- the canvas lock explicitly disables pan, zoom, dragging, and selection/connect interactions
- dark mode remains the only in-scope product theme for this adoption pass
- any older bottom-right add-launcher wording is obsolete and must not reopen the decision

This means adoption is not a literal copy of the handoff’s topology-editor prototype.
It is a selective application of the handoff’s visual system and component styling to
the current product behavior.

## Adoption Strategy

### 1. Establish design-system foundations
- import/self-host the handed-off fonts into the frontend
- translate token vocabulary from `colors_and_type.css` into the repo’s base styling layer
- define a consistent semantic mapping for borders, surfaces, text, accent, and status colors

### 2. Align route-level chrome
- restyle login and labs index surfaces to match the handed-off density, button treatment, and typography
- standardize eyebrow usage, card density, and action hierarchy

### 3. Align lab-editor chrome without regressing newer behavior
- preserve the merged single rail and bottom-left add launcher
- restyle rail sections, inventory cards, console workspace chrome, and header chrome to match the handoff

### 4. Align canvas components
- restyle device nodes, network nodes, context menu, and canvas overlays to match the handed-off component specimens
- keep current functional behavior intact

### 5. Align feedback and auxiliary components
- restyle toasts and any remaining utility controls to match the same token system

## Proposed Work Breakdown

1. Design tokens and typography foundations
2. Login and labs index design alignment
3. Lab editor rail, inventory, and console chrome alignment
4. Canvas nodes, menus, and overlays alignment
5. Toasts and remaining shared controls alignment

## Verification Plan

Required checks for the implementation phase:
- `npm run check`
- `npm run build`
- route-level browser validation for:
  - `/login`
  - `/labs`
  - `/labs/ui-reactivity-check.json`
- visual verification for:
  - left rail
  - inventory items
  - add launcher and canvas controls
  - context menu
  - console window
  - toasts

## Acceptance Standard For Execution

Execution should be considered complete when:
- the current UI visibly matches the handed-off design language
- later accepted interaction changes remain intact
- tokens and typography are consistent across the touched surfaces
- no route or canvas workflow regresses
