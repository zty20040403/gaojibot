# Theme artwork 0.1.31

- TypeScript/Vite build passed.
- Focused `verify-workspace-ui.cjs` passed with isolated macOS Electron fixtures; no real servers connected.
- Eight local SVG decorations load through the application protocol. No external asset host or new runtime dependency.
- All four themes expose different button geometry, outline and shadow, their matching edge artwork and watermark.
- Decorative pseudo-elements have `pointer-events: none`; background opacity is at most 0.1.
- Fixed rotated-watermark horizontal overflow by reserving space for its rotated bounds, not by clipping interactive content.
- Verified desktop and 390px layouts, light/dark modes, system appearance changes and saved preferences.
- Visually reviewed blossom/light workspace, ocean/light workspace, graphite/dark workspace and narrow dark theme chooser. Text remains legible and controls do not overlap.
- Package verifier now compares each bundled theme artwork file against the source asset.
- Generated screenshot fixtures may be deleted after successful publication; this written record is retained.

Scope: desktop appearance only. Deployment runtime unchanged. Windows runtime acceptance not claimed.
