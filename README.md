# claude-simple-usage (`ccu`)

Real-time Claude Code usage monitor. Runs `/usage` in a hidden tmux session and displays accurate server-side data.

```
  Claude Code Usage Monitor                 21:06:30

  Current Session
  █████████████████████░░░░░░░░░░░░░░░░░░░  53% used
  Resets 10pm (Asia/Seoul)

  Current Week (All Models)
  █████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  13% used
  Resets Mar 6, 12pm (Asia/Seoul)

  Current Week (Sonnet Only)
  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   0% used

  Refresh: 30s · Next in 25s
```

## Quick Start

One-liner with uvx (no install needed):

```bash
uvx --from git+https://github.com/kochul2000/claude-simple-usage.git ccu
```

Or install with pip/uv:

```bash
pip install git+https://github.com/kochul2000/claude-simple-usage.git
ccu
```

Or just download and run:

```bash
curl -sO https://raw.githubusercontent.com/kochul2000/claude-simple-usage/master/ccu.py
python3 ccu.py
```

## Requirements

- **tmux** — `brew install tmux` / `sudo apt install tmux`
- **claude** — [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)

## Usage

```bash
ccu              # refresh every 30s (default)
ccu 15           # refresh every 15s
ccu --once       # fetch once and exit
ccu --debug      # show raw tmux output
```

## Keys

| Key | Action |
|---|---|
| `Space` / `Enter` | Immediate refresh |
| `↑` / `↓` | Adjust refresh interval ±5s (3s–120s) |
| `←` / `→` | Adjust bar width ±5 (15–80) |
| `Ctrl+C` | Exit |

## How it works

1. Starts Claude Code in a hidden tmux session
2. Sends `/usage` command periodically
3. Parses the TUI output for usage percentages and reset times
4. Displays a clean dashboard with color-coded progress bars

## License

MIT
