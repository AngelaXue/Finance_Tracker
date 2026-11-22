# Instructions for AI agents

This is a private personal-finance app that runs only on the user's computer. **The full spec is
[`CLAUDE.md`](CLAUDE.md)** — read it before changing anything; §0 has a playbook for adapting the app to a user, and
§6 explains every business rule and why it exists. [`README.md`](README.md) is the user guide.

## Hard rules (these protect the user's financial data)

You are likely a cloud service, so the user's financial data must never reach you:

- **Never read, search, open, print or summarize** anything in `data/` (the SQLite database, the trained
  classifier), `incoming_statements/` (bank statements), `import_history/`, `legacy_data/` or
  `config/account_map.json` — not directly, and not indirectly through Python, `sqlite3`, pandas or app code that
  prints rows. (Claude Code enforces part of this through `.claude/settings.json`; other agents must follow it on
  their own.)
- To debug a data problem, ask the user for the output of the `--audit` commands or for error messages — those
  never contain amounts — or for a CSV **header row** only. Reproduce issues with fake data in a test.
- Never bind the server to `0.0.0.0`, never send transaction data to any network service, never hardcode an absolute
  path, and never log or print an amount or balance.

## Working here

- Schema changes: `SCHEMA` and an idempotent step in `_migrate()` in `src/db.py`.
- Verify: `python -m pytest -q` (temp databases only) and `node --check static/app.js`.
- When a rule changes, update `CLAUDE.md`, `README.md` and `templates/help.html` together.
- Optional features that aren't built in (e.g. a work card / reimbursement tracker) are written up as designs in
  `docs/recipes/` — build from those when the user needs one.
