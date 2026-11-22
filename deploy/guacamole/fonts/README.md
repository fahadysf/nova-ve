# Console terminal fonts

These TTFs are installed into the guacd container by
`deploy/scripts/run-guacamole.sh` (`install_guacd_fonts`), making them
available to the server-side terminal renderer for in-browser console
sessions (connection parameter `font-name`). All fonts are licensed
under the SIL Open Font License 1.1 (`OFL-1.1.txt`); the per-project
OFL notice for Roboto Mono ships as `RobotoMono-OFL.txt`.

| Fontconfig family name | Files | Source (Nerd Fonts v3.4.0) |
|---|---|---|
| `JetBrainsMono Nerd Font Mono` | JetBrainsMonoNerdFontMono-{Regular,Bold}.ttf | JetBrains Mono (JetBrains), Nerd Fonts patched |
| `FiraCode Nerd Font Mono` | FiraCodeNerdFontMono-{Regular,Bold}.ttf | Fira Code (The Fira Code Project Authors), Nerd Fonts patched |
| `MesloLGS Nerd Font Mono` | MesloLGSNerdFontMono-{Regular,Bold}.ttf | Meslo LGS (GitHub releases of Meslo LG), Nerd Fonts patched |
| `Roboto Mono` | RobotoMono-{Regular,Bold}.ttf | Roboto Mono (Google), unpatched |

Use the *Fontconfig family name* exactly when setting the default
terminal font (System → Settings → Terminal font) or the per-session
"Font name" field in the Guacamole client menu (Ctrl+Shift+Alt).
