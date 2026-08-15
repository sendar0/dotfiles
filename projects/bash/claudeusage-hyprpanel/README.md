# claudeusage-hyprpanel

HyprPanel custom module showing Claude Code rate-limit usage in the bar:
**session %, weekly all-models %, weekly Fable %**.

## How it works

- `claude-usage.py` reads the OAuth access token from
  `~/.claude/.credentials.json` (never printed, only sent to Anthropic) and
  queries `https://api.anthropic.com/api/oauth/usage` with the
  `anthropic-beta: oauth-2025-04-20` header.
- It maps the response's `limits` array (`session`, `weekly_all`,
  `weekly_scoped` → Fable) to a single JSON line that HyprPanel's
  custom-module templating consumes:

  ```json
  {"session": 15, "weekly": 32, "fable": 60, "percentage": 60,
   "alt": "normal", "tooltip": "Session: 15% (resets 17:29)\n..."}
  ```

- On failure (offline, expired token) it falls back to the last good result
  cached in `~/.cache/hyprpanel-claude-usage.json`, marked stale (icon
  switches to a warning triangle). It never refreshes the OAuth token itself —
  Claude Code owns that; the token stays fresh as long as you use Claude Code.

## HyprPanel wiring

`~/.config/hyprpanel/modules.json` defines the module:

```json
{
    "custom/claude": {
        "execute": "/home/sander/projects/2026/claudeusage-hyprpanel/claude-usage.py",
        "executeOnAction": "/home/sander/projects/2026/claudeusage-hyprpanel/claude-usage.py",
        "interval": 60000,
        "icon": { "normal": "✳", "stale": "", "default": "✳" },
        "label": "{session}% {weekly}% {fable}%",
        "tooltip": "{tooltip}",
        "actions": { "onLeftClick": "xdg-open https://claude.ai/settings/usage" }
    }
}
```

`custom/claude` is added to `bar.layouts."0".right` in
`~/.config/hyprpanel/config.json` (add it to layouts `"1"`/`"2"` too if you
want it on the other monitors). Icon color lives in
`~/.config/hyprpanel/modules.scss` under `.cmodule-claude`.

Notes:

- Polls every 60 s; any click also triggers an immediate refresh
  (`executeOnAction`). Left click opens the Claude usage settings page.
- HyprPanel only reads `modules.json` at startup — restart it after changes
  (`hyprpanel quit; hyprctl dispatch exec hyprpanel`).
- Hovering the module shows a tooltip with all three limits and their reset
  times.
