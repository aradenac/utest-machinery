# uTest Machinery — agent compatibility

**Claude Code automatically loads [CLAUDE.md](CLAUDE.md), which is the authoritative development-agent entrypoint.** Other coding agents should read that file before acting.

Development proceeds **one product-feature PR at a time**, with local tests first and explicit human review/merge. **Stop after each PR**; never continue to the next milestone automatically.

Backlog: **Beads (`bd`)**, synchronized and made reviewable in Git; requirements and feature gates remain in [the implementation plan](docs/plan/implementation.md). Do not let automatic Beads setup overwrite these instructions.
