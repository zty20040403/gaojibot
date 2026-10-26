# App 0.1.22: model discovery and installation

## Changes

- API discovery no longer uses a browser datalist filtered by the current model
  name. Discovered models and saved model profiles have separate counters.
- Search, selection and batch import preserve the default profile and deduplicate
  exact API/protocol/model combinations. GPT image-generation and special review
  endpoints are listed separately, not silently used as ordinary chat models.
- A saved credential can be copied only inside the same instance and exact API
  URL/protocol. No saved keys are returned to the App. Older controllers require
  re-entering the credential before batch import; normal listing still works.
- Remote and local installs prefer a checksum-bound, architecture-specific image
  when the shipped deployment catalog contains one. Image pulls fail closed on
  download or verification errors; they do not silently fall back to source.
- Source installation has an explicit official/Tsinghua Debian HTTPS source
  selector, APT retries/timeouts, and sanitized progress. These settings do not
  change the host's package sources, Nix configuration, Docker registry, or PyPI.
- Existing data and installation job IDs are retained when retrying. Running
  installations are not interrupted or upgraded by simply opening the new App.

## Publishing

Build the runtime once, then use `packaging/publish-runtime-images.mjs` with an
explicit Docker endpoint, approved GHCR namespace, and `--publish`. It builds only
the already verified allowlisted archive, never the repository or a user's home
directory. The script records the registry's immutable digests in deployment.json
after all four architecture/component images have published successfully.

Package Electron after publishing without rerunning build-runtime.mjs, since a
new runtime archive has a new identity. Make GHCR packages anonymously readable
before releasing to novice users; the installer does not request registry tokens.
No prebuilt release is claimed until its catalog is populated and downloads are
verified. An empty catalog permits explicit source fallback in Auto mode.

## Verification

Read-only discovery against the user-provided CLIProxyAPI returned twelve IDs.
No inference request or model charge was performed for that check. Synthetic UI
acceptance showed all twelve IDs after selecting one, imported six chat profiles
with independent records, preserved the default, and checked narrow/desktop
layouts and invalidation after credential edits. Images are in desktop/release/qa.

Focused Python checks cover catalog credentials, installer gates, pinned image
selection, sanitized progress and existing-instance updates. Focused Node checks
cover upload validation, persisted retry identity, source selection and local
Docker endpoint isolation. No h310 installation was restarted or modified.

Prebuilt publication and a full fresh-server install have not been run for this
revision. The Windows installer is packaged, not execution-tested on Windows.

Reference: [Docker multi-stage builds](https://docs.docker.com/build/building/multi-stage/)
and [Tsinghua Debian mirror](https://mirrors.tuna.tsinghua.edu.cn/help/debian/).
