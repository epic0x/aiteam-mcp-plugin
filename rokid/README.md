# aiteam.space for Rokid Glasses

Talk to your Son from Rokid Glasses. Same Son, same tools and same sign-in as the Claude and ChatGPT connectors in
this repo.

## How it connects

```
Rokid Glasses (AI Team app or the "AI Team" AIUI agent)
  -> https://rokid.aiteam.space   voice bridge: speech to text, the model, tap-to-confirm cards
  -> your Son's MCP tools          the same tool surface as https://mcp.aiteam.space/mcp
```

- The glasses never hold an MCP token or an aiteam.space password. Each device gets its own token when it pairs,
  and you can disconnect it on its own from aiteam.space.
- Write actions (messages to other people, GitHub writes) show a confirm card on the lens. Tap to send. No tap, no
  action.
- Pairing only accepts https bridges on `aiteam.space` or `*.aiteam.space`.

## Set up

1. Sign in at https://aiteam.space and open **My Sons**.
2. On the **Rokid Glasses** card, choose **Get setup QR code**. The code works once, for 10 minutes.
3. On the glasses, open the AI Team app (or say "Hi Rokid, open AI Team") and look at the QR code.
4. The card on aiteam.space switches to **Connected**. Ask your Son anything, or hand it a task by voice.

To disconnect a pair of glasses, choose **Disconnect** on the same card.

## Shared with the other clients

| Piece | Claude | ChatGPT | Rokid Glasses |
| --- | --- | --- | --- |
| Entry point | `claude-plugin/` (plugin bundle) | `package/` (plugin ZIP) | this folder + the AI Team app |
| MCP server | `https://mcp.aiteam.space/mcp` | `https://mcp.aiteam.space/mcp` | through `https://rokid.aiteam.space` |
| Sign-in | aiteam.space OAuth | aiteam.space OAuth | one-time setup QR from aiteam.space |
| Usage guide | `skills/aiteam-mcp` | `package/skills/get-started` | served by the bridge |
| Brand assets | `package/assets/` | `package/assets/` | `package/assets/` |

Support: https://aiteam.space/support . Privacy: https://aiteam.space/privacy . Terms: https://aiteam.space/terms .
