# knitter

A zero-dependency Nitter RSS reader backed by a local SQLite archive with client-side boolean filtering.

It runs on Python 3.11+ stdlib alone and fetches `/rss` feeds while respecting `robots.txt`, `ETag`/`304` revalidation, and host rate limits. Healthy instances are discovered automatically from `status.d420.de` (cached for an hour), or you can pass your own instance URL with `-i`.

## Install

```bash
pip install -e .
```

Or run `python3 knitter.py` directly.

## Usage

```bash
knitter rust
knitter '(python OR rust) AND ("release notes" OR launch) -draft'
knitter -u github --new
knitter --local 'python OR rust'
knitter -i http://localhost:8080 -u github --json
```

Run `knitter --help` for flags. Optional env vars: `KNITTER_DB` (default `~/.cache/knitter/knitter.db`), `KNITTER_DELAY` (default `1.5`s).

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT.
