# Documentation

Start with the path that matches what you want to do. The technical references
are detailed, but you do not need a tour of every experimental index just to
remember a command you ran yesterday.

| Goal | Start here |
| --- | --- |
| Install on macOS, save and find your first event, or enable capture | [Getting started](GETTING_STARTED.md) |
| Learn what is implemented, experimental, or unavailable | [Capabilities and limits](CAPABILITIES.md) |
| Understand the packages, storage, and trust boundaries | [Architecture](ARCHITECTURE.md) |
| Choose a contribution or see acceptance criteria | [Improvement sprint](SPRINT.md) |
| Use the macOS client | [CLI package guide](../packages/cli/README.md) |
| Use the DB on its own | [DB package guide](../packages/db/README.md) |

## Deeper references

- [CLI internals](../packages/cli/docs/TOME.md) and [DB internals and math](../packages/db/docs/TOME.md)
  document code paths and research experiments. They are reference material,
  not a promise that every subsystem is production-ready.
- [Native companion](../packages/cli/native/README.md) describes the optional
  Swift bridge.
- [Selected benchmark reports](benchmarks/README.md) show measured results for
  specific synthetic workloads. They are not general comparisons.
- [Future directions](../packages/cli/docs/FUTURE.md) is exploratory, while the
  [improvement sprint](SPRINT.md) is the near-term work list.

For contributing and private vulnerability reports, see
[CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md).
