# Desktop themes 0.1.30

## Scope

Renderer-only visual changes. Runtime bundle and deployment actions unchanged.
No connections to h310, h610, tank, or user servers during this check.

## Local evidence

- `npm run build`: TypeScript and Vite passed.
- `tests/verify-workspace-ui.cjs`: passed in isolated macOS Electron user data.
- Four palettes in light and dark: sidebar, primary command, search input, and theme control each have four distinct computed backgrounds.
- Theme choice and dark mode persist through reload. System mode follows emulated OS appearance changes.
- 1280px desktop and 390px narrow view have no page horizontal overflow; narrow theme dialog has no content horizontal overflow.
- Completed theme dialog closes via its command. Search, pin persistence, filters, secret masking, and redacted diagnostics still pass.
- Screenshots visually reviewed: forest/light workspace, blossom/light theme chooser, blossom/dark workspace, narrow dark theme chooser. No text overlap seen.

Screenshots are generated under `desktop/release/qa/workspace-0.1.30/` and can be cleaned after publication. They contain only synthetic `.invalid` server fixtures.

## Limits

This is a focused UI check, not new end-to-end deployment or Windows runtime acceptance.
