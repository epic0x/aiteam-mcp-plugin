# Submitting the aiteam.space plugin (ChatGPT and Claude)

Owner steps. Everything you paste is listed below each step. The package source is `package/listing.json`
(every listing value lives there), `package/skills/get-started/SKILL.md` and `package/assets/`.

## 0. Before you submit: replace the placeholders
Tags ship only from main: the release workflow fails when the tag's commit is not on `main` (`scripts/tag_on_main.sh`).

Each release lists the placeholders still in the build (release notes and `dist/PLACEHOLDERS.md`). Today:

| Value | Where | Now |
| --- | --- | --- |
| Support URL | `listing.json` `interface.supportURL` | `PLACEHOLDER:https://aiteam.space/support` (page does not exist yet) |
| Privacy policy URL | `interface.privacyPolicyURL` | `PLACEHOLDER:https://aiteam.space/privacy` |
| Terms of service URL | `interface.termsOfServiceURL` | `PLACEHOLDER:https://aiteam.space/terms` |
| Demo video URL | `review.demo_recording_url` | `PLACEHOLDER:https://aiteam.space/demo` (record the 5 positive cases) |
| Countries | `publication.countries` | `PLACEHOLDER:omit` (no field shipped). `[]` = no restriction, or `["US", "GB"]` |
| Icon + logo | `package/assets/icon.png`, `icon-dark.png`, `logo.png`, `logo-dark.png` | not there yet (the build ships neutral SVG placeholders). Add the four square PNGs (48 to 4096 px, <= 5 MiB) and point `composerIcon`, `composerIconDark`, `logo`, `logoDark` at them. `--strict` fails until all four exist. No screenshot: OpenAI asks for none when the plugin has no UI |

To confirm a value, drop the `PLACEHOLDER:` prefix. `python3 scripts/build_package.py --version X --strict` fails while
any placeholder remains; the release build is non-strict and lists them instead.

## 1. Get the ZIP
1. Push a tag `vX.Y.Z` on main (the release workflow builds the ZIP and attaches it), or run the **release** workflow
   by hand with an existing tag.
2. Open the repo's **Releases** page, latest release, download `aiteam-chatgpt-plugin-X.Y.Z.zip`.
3. Local build instead: `python3 scripts/build_package.py --version X.Y.Z` writes `dist/aiteam-chatgpt-plugin-X.Y.Z.zip`.

Each new upload needs a new version number (`plugin_version_unchanged` otherwise).

## 2. ChatGPT: upload and connect
1. Sign in at https://platform.openai.com with the organization that will own the plugin (owner, or a member with
   **Apps Management Write**). Business or individual verification must be done in organization settings.
2. Open **Plugins** (apps management) and choose **Upload new or existing plugin**.
3. **Developer identity**: pick the verified identity that should appear as the publisher.
4. **Upload plugin**: choose the ZIP. The draft opens after validation. Package errors: fix `package/`, build a new
   version, upload again.
5. **Metadata & Skills**: wait for the checks and skill scans (can take up to 2 hours). **Copy issues**, fix them in
   `package/`, upload the next version.
6. **MCPs** > server `aiteam` > **Connect**. Check:
   - MCP Server URL: `https://mcp.aiteam.space/mcp`
   - Authentication: OAuth, client registration DCR (read from `mcp.json`; nothing to type, no client secret)
   - Authorization server: `https://aiteam.space` (found by discovery), scopes `son:read son:write`
7. **Domain challenge**: the portal shows a token and a URL ending in `/.well-known/openai-apps-challenge`. Copy the
   token and give it to the operator. The operator sets `OPENAI_APPS_CHALLENGE=<token>` on the relay and redeploys,
   so the URL returns exactly that token as plain text. Then select **Verify Domain**.
8. Select **Connect**, sign in with the aiteam.space review account and approve access.
9. **Scan Tools**: wait for the scan, open **Issues**, fix findings on the server (tool annotations and their
   justifications), deploy, **Rescan**.
10. **Review information > Review details**: test cases, demo URL and release notes come from the ZIP (read-only).
    Enter the reviewer credentials here (never in the package): a dedicated test account on aiteam.space with a Son
    online, no MFA, no email codes, with the sign-in steps. Select **Save details**.
11. **Submit for review**, tick the policy attestations. Feedback arrives by email. After approval, open the
    approved version and select **Publish plugin**.

### Values the portal shows (from the ZIP)
- Display name: `aiteam.space`
- Short description: `Your AI teammate, own computer`
- Category: `Productivity`
- Website: `https://aiteam.space`
- Support / privacy / terms: see the placeholder table above
- MCP server: `aiteam` at `https://mcp.aiteam.space/mcp`, OAuth (DCR)
- Test cases: 5 positive, 3 negative (`listing.json` `review.test_cases`)

## 3. Claude: directory submission
1. Open https://claude.ai/directory/manage and choose **Submit new**.
2. Type: **MCP connector** (remote).
3. Paste:
   - Server URL: `https://mcp.aiteam.space/mcp`
   - Name: `aiteam.space`
   - Short description: `Your AI teammate, own computer`
   - Long description: `interface.longDescription` from `package/listing.json`
   - Website: `https://aiteam.space`
   - Support, privacy policy, terms: the final URLs from the placeholder table
   - Authentication: OAuth (aiteam.space supports dynamic client registration and client metadata documents)
   - Example prompts: the three `interface.defaultPrompt` values
   - Logo: `package/assets/logo.png` (voice-mode art: `voice-claude.png`, `voice-chatgpt.png`; see docs/store/LISTINGS.md)
   - Test account: the same reviewer account as for ChatGPT
4. Submit and follow the review email.
