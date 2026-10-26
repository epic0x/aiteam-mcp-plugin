# aiteam-connector

The AI Teamspace Plugin MCP Connector: one MCP server for Claude and ChatGPT that drives your Hermes Sons,
plus the messaging box sidecar. Design: [../docs/UNIFIED-CONNECTOR.md](../docs/UNIFIED-CONNECTOR.md).

```bash
pip install -e connector            # Python 3.11+
aiteam-box new-caller my-connector --sons my-son      # prints a caller token ONCE + the config entry
SON_API_KEY=... aiteam-box serve --config box.json --port 8710
BOX_TOKEN_SON=... aiteam-connector --config conn.json --port 8720   # MCP at http://127.0.0.1:8720/mcp
python connector/scripts/e2e_live.py http://127.0.0.1:8720/mcp my-son # live check, JSON report
```

`box.json`
```json
{"box_id": "my-son-box", "box_name": "My Son box",
 "sons": [{"name": "my-son", "url": "http://127.0.0.1:8642", "key_env": "SON_API_KEY"}],
 "callers": [{"id": "my-connector", "name": "Claude connector", "token_sha256": "<from new-caller>", "sons": ["my-son"]}]}
```
`conn.json`
```json
{"sons": [{"name": "my-son", "box_url": "http://127.0.0.1:8710", "box_token_env": "BOX_TOKEN_SON",
           "upstream_mcp_url": "http://127.0.0.1:8700/mcp"}],
 "passthrough": "session"}
```
Optional per Son: `"model": "claude-opus-5-5"` pins the box session model (needed on Hermes 0.20.x, where a session
created without one stores the placeholder `hermes-agent` and the provider answers 404).
Coordinator: list several Sons in both files (each Son's own box URL in `conn.json`). Secrets only in env vars.
Tests: `pip install -e "connector[test]" && pytest -q connector`.

## GitHub tools (the Son's own `gh`)

27 `github_*` tools (13 read, 14 write) are on by default. They run the Son's own `gh` CLI with the Son's own
login, so they have the same permissions and return the same output as `gh` on that Son, byte for byte. There is
no GitHub App and no separate OAuth scope. Each call runs `gh` with an argument list (never a shell), is cut off
after 30 s, and its output is capped at 60 000 chars (a cut says so). A Son with no login answers
"Connect GitHub to your Son: follow the guide at <connect_url>." with gh's own message underneath.

Config (`conn.json`, all optional):

    "github": {"timeout_s": 30, "max_output_chars": 60000, "allowed_owners": ["epic0x"],
               "connect_url": "https://aiteam.space/my-sons/<son id>?connect=github"}

`allowed_owners` is off by default (full `gh` parity). Set it on Sons whose `gh` is logged in as a person
with wide access. `"github": false` removes the tools.

Live parity check: `python scripts/parity_live.py <mcp-url> [bearer-file]` runs the same calls through MCP and
through local `gh` and diffs the text.
