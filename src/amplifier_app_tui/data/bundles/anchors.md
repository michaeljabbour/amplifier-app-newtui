---
name: anchors
version: 0.1.0
description: >-
  Packaged pointer to foundation's anchors bundle at the same locked ref the
  tui wrapper composes — so `bundle.active: anchors` (a valid
  amplifier-app-cli default carried in shared settings) resolves here too.
  No app overlays: raw anchors; providers come from settings
  `config.providers` / keys.env.
includes:
  # Keep this full SHA in lockstep with tui.md and anchors-source-lock.json.
  # The pinned Foundation resolver cold-loads non-tip commits correctly.
  - bundle: git+https://github.com/microsoft/amplifier-foundation@dea5bd8fe11a7617dbcfc61c47f9f4f2fdc0b134#subdirectory=bundles/anchors/bundle.md
---

# anchors (packaged pointer)

Cross-app parity shim. amplifier-app-cli users carry `bundle.active: anchors`
in `~/.amplifier/settings.yaml`; without this pointer tui refused to boot
("Bundle 'anchors' not found in project, user, or packaged bundle paths").

The TUI-specific overlays (default provider, tool-mcp, team-pulse,
notify-push, the user skills dir) live in the `tui` wrapper bundle —
booting raw `anchors` skips them by explicit choice. The app kernel still
suppresses the printing/notify hooks at mount time regardless of bundle, so
the screen stays clean either way.
