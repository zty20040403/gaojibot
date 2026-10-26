# App Releases

The desktop App and the server runtime have separate update flows. App updates do
not deploy, stop, restart, or migrate a bot instance. Connection records and
credentials stay in the existing Electron userData directory.

## User Flow

- Open the download icon in the top bar or Gaoji > Check App Updates.
- Check, download, then open the installer (macOS) or restart and update (Windows).
- macOS preview builds are unsigned. They intentionally use a verified DMG and
  require replacing the application in Applications. No Gatekeeper or signature
  checks are disabled. The first updater-enabled version must be installed manually.
- Windows uses electron-updater/NSIS. Automatic download and install-on-quit are
  disabled. The explicit restart asks users to save pending form edits.
- Existing installers in Downloads and duplicate Apps outside the installation
  directory are not deleted. The App-owned Mac update cache deletes partial files,
  packages at or below the running version, and packages older than seven days.
- Update errors leave the installed application intact. No release and a failed
  network check are distinct states, not an assertion that the App is current.

## Publishing

Installers and update manifests are published to `zty20040403/gaoji-app`.
This distribution-only repository contains no workspace source or user data.
Versions through 0.1.24 use the old repository; install 0.1.25 manually once
to move to the new update channel. App identity and saved settings stay unchanged.

Only published, non-prerelease tags matching `app-vX.Y.Z` enter the update channel.
Bot tags are ignored. `app-update.json` has schema 1, appId `com.kenneth.gaoji`, a
semver version, and darwin-arm64/win32-x64 assets with name, byte count and SHA-256.
GitHub HTTPS is the distribution trust boundary; checksums detect corruption,
not compromise of the GitHub publisher account. No signing key is bundled.

1. Increment desktop/package.json and package-lock.json with npm version.
2. Build the UI. Reuse the verified runtime bundle for App-only edits; rebuild the
   runtime only when its source changes.
3. Package macOS arm64 and Windows x64 using electron-builder `--publish never`
   into the same versioned release directory. Keep generated `latest.yml`.
4. Run `node scripts/release-manifest.cjs release/VERSION` after targeted QA.
5. With explicit release approval, run
   `node scripts/publish-app.cjs --publish release/VERSION`.

The publisher uploads an explicit installer/manifest allowlist, verifies GitHub
asset SHA-256 values, then publishes the draft. It does not commit or push source.
It uses the configured Git credential helper in memory and never includes user
data, keys, test fixtures, or server state. It refuses to overwrite public assets.
Interrupted uploads stay in a draft; verified assets are reusable on retry.
After confirming the previous publisher has exited, `--resume-incomplete` can
discard its unfinished `starter` upload without waiting for the two-minute guard.

If a network path repeatedly interrupts large uploads, run
`node scripts/stage-release.cjs --stage release/VERSION` against the existing
draft. It uploads verified 32 MiB parts and dispatches the distribution repo's
`assemble-release.yml` workflow. The workflow validates the pinned manifest and
every complete installer, removes parts, and leaves the release unpublished.
After that workflow succeeds, rerun the normal publisher to verify and publish.
The workflow uses GitHub's short-lived built-in token, not a user credential.
Initial workflow installation requires GitHub workflow-write permission or an
authorized SSH commit to the distribution repo; App source is never pushed.

Windows update plumbing is unit-tested with an adapter fixture. A macOS cross
build is not proof of Windows execution: real NSIS upgrade acceptance on Windows
and signed/notarized Mac auto-replacement remain release follow-ups.
