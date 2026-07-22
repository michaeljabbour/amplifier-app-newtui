---
name: anchors
version: 0.1.0
description: >-
  Packaged pointer to foundation's anchors bundle at the same pinned SHA the
  newtui wrapper composes — so `bundle.active: anchors` (a valid
  amplifier-app-cli default carried in shared settings) resolves here too.
  No app overlays: raw anchors; providers come from settings
  `config.providers` / keys.env.
includes:
  # Keep this SHA in lockstep with the include in newtui.md — a test pins
  # the two together (test_kernel_session_config.py).
  - bundle: git+https://github.com/microsoft/amplifier-foundation@93615d9847ce40313cc0d60583cb886de4337f9e#subdirectory=bundles/anchors/bundle.md
---

# anchors (packaged pointer)

Cross-app parity shim. amplifier-app-cli users carry `bundle.active: anchors`
in `~/.amplifier/settings.yaml`; without this pointer newtui refused to boot
("Bundle 'anchors' not found in project, user, or packaged bundle paths").

The TUI-specific overlays (default provider, tool-mcp, team-pulse,
notify-push, the user skills dir) live in the `newtui` wrapper bundle —
booting raw `anchors` skips them by explicit choice. The app kernel still
suppresses the printing/notify hooks at mount time regardless of bundle, so
the screen stays clean either way.
