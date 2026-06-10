# CHANGELOG

<!-- version list -->

## v0.2.2 (2026-06-10)

### Bug Fixes

- **auth**: Read provisioned secrets in raw mode to avoid MAX_CANON hang
  ([`382ce5b`](https://github.com/igrybkov/mcp-hub/commit/382ce5b3332c19f38724583eaded06a69afa023b))

### Refactoring

- **auth**: Compact provision output and show create_url in all cases
  ([`4b03144`](https://github.com/igrybkov/mcp-hub/commit/4b031443df6a3de657e700418ad3638f34633634))


## v0.2.1 (2026-06-09)

### Bug Fixes

- **auth**: Clarify empty input keeps current value on --force provision
  ([`5bf268a`](https://github.com/igrybkov/mcp-hub/commit/5bf268ae7513fec0cc4788c9dc81167e89909037))


## v0.2.0 (2026-06-08)

### Continuous Integration

- Drop unsupported GitHub Packages publish step, block PyPI uploads
  ([`728e9d0`](https://github.com/igrybkov/mcp-hub/commit/728e9d0894d02987acb39f60fb218a1824df3464))

### Documentation

- Add CLAUDE.md guide for Claude Code
  ([`d6de823`](https://github.com/igrybkov/mcp-hub/commit/d6de8231c230c19fc0aa775ccf5559669b6b4c6f))

- Lead with quick start and lift the table of contents to the top
  ([`f91266f`](https://github.com/igrybkov/mcp-hub/commit/f91266f2ab0e41505c8fa89f0266d5a8c8198cca))

### Features

- Add server-management agent skill and config CLI helpers
  ([`04e6fb0`](https://github.com/igrybkov/mcp-hub/commit/04e6fb074f50b09646d74955325b930fe73c3170))


## v0.1.0 (2026-06-08)

### Features

- Implement mcp-hub meta-MCP server with lazy proxying, discovery, auth, and CLI
  ([`16f0ad4`](https://github.com/igrybkov/mcp-hub/commit/16f0ad43eae8e51aa2d551cf98c9907bd318389a))
