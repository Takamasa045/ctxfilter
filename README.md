# ctxfilter

Read-only, bounded local file reading for Codex through CLI and stdio MCP. No network calls or Jev inference.

`read` / `read_file` automatically returns compact full UTF-8 for small files that fit the output budget, otherwise a bounded preview. A literal query selects search. Existing preview, search and fetch APIs remain available.

```bash
python3 outputs/bin/ctxfilter read --path FILE
python3 outputs/bin/ctxfilter read --path FILE --query TEXT
python3 outputs/bin/ctxfilter mcp
PYTHONPATH=outputs/src python3 -m unittest discover -s outputs/tests -v
```

Defaults: full-text candidate 4096 bytes, content JSON budget 4096 bytes. MCP envelopes are counted separately. Preview/search retain identity and continuation cursors.

See [usage and contracts](outputs/README.md), [auto-read verification](outputs/auto-read/README.md) and [measurements](outputs/auto-read/measurement.json).

Local backups, credentials, generated fixtures and runtime logs are excluded. The fixture generator is included for tests. Registration examples and recorded evidence refer to the original local installation paths; adjust them for another machine. New `read_file` visibility in an already-running Codex session requires MCP reload and was not verified in the original implementation session.
