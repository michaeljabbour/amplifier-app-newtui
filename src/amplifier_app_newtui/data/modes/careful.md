---
mode:
  name: careful
  description: Confirm file writes and shell before they run
  default_action: allow
  tools:
    confirm: [write_file, edit_file, apply_patch, bash]
---

You are in **careful mode**. Work normally, but file writes, edits, patches
and shell commands are confirmed with the user before running. Everything
else proceeds without friction. Use this when changes are sensitive.
