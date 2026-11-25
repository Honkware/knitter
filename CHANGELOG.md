# Changelog

## 0.1.0 - 2026-11-24

- Initial release with polite Nitter RSS fetching (`robots.txt`, `ETag`/`If-Modified-Since`, 15-minute feed cache, 1-hour host cooldowns).
- Automatic healthy RSS instance discovery via `status.d420.de` (`0xpr03/nitter-status`) plus custom `-i` instance override.
- Single-file SQLite WAL archive (`--new` incremental scans and `--local` offline queries).
- Client-side boolean and phrase query planner (`AND`, `OR`, `NOT`, `-term`, `"phrase"`).
