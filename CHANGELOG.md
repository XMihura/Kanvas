# Changelog
## v0.3.1 — 2026-03-19
- Fix `add-dep` incorrectly rejecting valid edges when a pre-existing unrelated cycle existed elsewhere in the graph
## v0.3.0 — 2026-03-19
- Add DAG auto-layout engine to the Canvas Watcher plugin
- Add vertical and horizontal layout buttons to the Obsidian canvas toolbar
- Add layer-gap slider to control spacing between dependency levels, persisted across sessions
- Layout respects group membership, resolves depth conflicts via graph coloring, and applies transitive reduction
- Add `.gitattributes` to normalize line endings

## v0.2.0 — 2026-03-16
- Add `init` command to set up Kanvas in a project directory
- Add `--no-plugin` flag to skip Obsidian plugin install during init
- Improve README setup instructions

## v0.1.0 — 2026-03-13
- Initial release
- CLI tool (`canvas-tool.py`) with workflow enforcement
- Workflow rules (`RULES.md`) and agent instructions (`CLAUDE.md`, `AGENTS.md`)
- Canvas Watcher Obsidian plugin
- Standalone canvas watcher (`canvas-watcher.js`)
- Example boards (blank template, sample project)
