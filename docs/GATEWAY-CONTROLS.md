# Gateway controls over MCP (local implementation; activation not performed)

The AI-control PR is rebased on `origin/main`'s existing model-control hook. It retains both `ai_*` secure sign-in/API-session handling and `son_model_*` gateway model routing. The existing `son/model_control/handler.py` owns the new session/command verbs too; no second model hook is installed.

## Tools and semantics

| Tool | Meaning | Destructive / idempotent |
| --- | --- | --- |
| `son_session_stop` | Native hard interrupt and queued-user-work cleanup of exactly one routing session | yes / yes |
| `son_session_interrupt` | Explicit alias for the same hard interrupt, **not** a soft pause | yes / yes |
| `son_session_reset` | Native reset of that conversation; returns its newly read routing session ID | yes / no |
| `son_slash` | Audited native built-in dispatcher, no shell or LLM fallback | conservatively yes / no |
| `son_approvals_set` | Persistent **profile-wide** `smart` or `yolo`; yolo maps to native `off` | yes / yes |

All are writes, `openWorldHint=true` (native handlers can interact with adapters/delegations). All require `sessionKey` or `sessionId`. If both are provided they must agree; unknown keys do not fall back to an unrelated valid ID. Historical/CLI/API-only sessions without a current gateway origin are refused. Multiplex gateways fail closed.

Approval mode uses Hermes' typed `run_approval_mode_command`, including managed-policy checks and effective-mode verification. A failure is returned as failure without echoing native exception text. Existing session-level `/yolo` overrides are **not** cleared when setting smart; this is explicitly reported. No approval policy on the actual host was changed during this work.

## Native slash boundary

Allowed: `/status`, `/context`, `/help`, `/commands`, `/profile`, `/version` (`/v`), `/whoami`, `/agents`, `/stop`, `/new` (`/reset`), and constrained enum arguments for `/reasoning`, `/fast`, `/footer`.

This is deliberately **not every Hermes command**. Unknown commands, CLI-only commands, plugin commands, exec/quick aliases, prompt rewrites, multiline text, cross-session `/resume`, `/login`, credential-taking `/model` forms, and generic `/approvals` or `/yolo` are refused. Model/fallback changes use dedicated model tools. Credential operations use `ai_signin_start` and the secure entry page; never paste a code/key into a tool command or chat.

Uses installed `resolve_command`, `_hm_dispatch_canonical_command(event, source, key, canonical)`, and `_dispatch_busy_slash_command(event, cmd_def, key, source)`. Stop/reset always use the exact-key busy path, even when idle: the ordinary idle `/stop` handler may broaden to sibling runs and is therefore unsuitable for this endpoint. Native busy rejection is respected; no new LLM turn is started.

Native free-text replies are deliberately suppressed, not merely regex-redacted: arbitrary exception/config strings can contain unknown secret formats. `ok=true, dispatched=true` means the native handler returned, **not** that an arbitrary slash command's requested effect succeeded. Typed approvals outcomes and refreshed reset IDs are available. No shell eval, subprocess shell, or client-supplied owner/identity field is accepted.

## Owner and session binding (default closed)

1. Local connector requires the verified gate subject (`X-Aiteam-Sub`, inserted only by the existing JWT gate) for **all** AI/model/control tools. A command argument never grants authority.
2. For session/command controls the installed hook additionally requires a deployment-owned mapping in Hermes configuration:

   ```yaml
   aiteam_control:
     owners:
       owner@example.com:
         discord: 'platform-user-id'
         telegram: 'platform-user-id'
   ```

   This documentation is an example, not a request to edit a live host. Normal managed configuration APIs must provision it. No mapping means refused. The stored session origin must have that platform user ID, recompute to the selected session key, pass native source authorization and slash policy. Approval writes also require native admin permission. A stale/reset ID is checked again after entering the profile scope.
3. Local UNIX protocol adds `principal` at the **envelope**, supplied by the authenticated connector/box, never from tool arguments. The existing same-uid `0600` socket remains the trust boundary.

## Multiple configured destinations, not implicit fleet authorization

Schemas derive from configured `sons`, never from hard-coded `dee` or guessed Davis identities. This does **not** authorize every owned Son from a singleton JWT.

A remote `SonLink` needs its own box URL/token, `son_id` for secure sign-in, and explicit `control_owners` list. No remote Son is sent to the local socket; no fallback is attempted on missing destinations or errors. The local first Son may use existing gate authority; remote destinations without an explicit owner grant are refused. Duplicate names and bad deployment data should be avoided; this is not a fleet-discovery mechanism.

Remote `son_model_*` and new controls use `POST <box>/v1/control`:

```json
{"tool":"son_session_stop","args":{"sessionId":"existing-id"}}
```

The destination box must bind its caller token to `control_owner` and explicitly list its local Son in the caller's `sons` (legacy `*` alone is insufficient). `ai_control.son` is required for multi-Son boxes; a one-Son box derives it. Optional `ai_control.control_socket` selects its local hook socket. The connector forwards its verified subject in `X-Aiteam-Sub`; the box requires an exact match to the token-bound `control_owner` (the header alone never authenticates). The box rejects a request-supplied principal/identity and remote `son` selection. It returns an MCP CallToolResult. Sign-in still uses the existing `/v1/ai/*` proxy; missing remote `son_id` is refused before starting a login.

**Release dependency:** the public relay is still singleton-transparent. Site #190 selected-grant foundation and the separate experimental selected-Son router are not folded into this PR. Davis credentials/grants and selected-router deployment are outside this work. Updating this PR alone cannot make all owned Sons appear through Claude or ChatGPT.

## Verification and activation

Local tests cover fake authorized destinations over two real boxes and two real UNIX sockets, negative authentication, mismatched/stale selectors, forged identity fields, native handler exceptions and suppression of unstructured results. The installed-Hermes probe exercises real registry/dispatch, durable SessionStore and native approval configuration **only in an isolated temporary home**, with interrupt/reset side effects replaced by isolated test operations. It is not live Davis/Claude proof, a provider completion, or production session-stop proof.

Run connector and relay suites as documented in `MODEL-CONTROL.md`; plugin tests require Hermes dependencies. The opt-in `tests/test_installed_gateway_controls.py` requires a fresh `HERMES_HOME` named `control-probe-*` and the installed Hermes import path. No image build, Actions workflow, push, merge, deployment, reload, credential modification or real session interruption is part of this change. Activation still requires the consuming Son image to ship the updated hook and a planned owner-approved gateway start.
