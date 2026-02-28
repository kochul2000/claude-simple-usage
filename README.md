# claude-simple-usage (`ccu`)

Real-time Claude Code usage monitor. Runs `/usage` in a hidden tmux session and displays accurate server-side data.

![screenshot](screen-shot.png)

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

To install `ccu` as a command:

```bash
python3 ccu.py install
```

This creates a symlink in `~/.local/bin`. Make sure it's in your `PATH`.

## Requirements

- **tmux** — `brew install tmux` / `sudo apt install tmux`
- **claude** — [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)

## Usage

```bash
ccu                            # refresh every 30s (default)
ccu 15                         # refresh every 15s
ccu --config-dir ~/.claude     # use specific config directory
ccu --no-pace                  # start with pace bar hidden
ccu --no-profile               # start with profile info hidden
ccu --once                     # fetch once and exit
ccu --debug                    # show raw tmux output
ccu install                    # install ccu to ~/.local/bin
ccu uninstall                  # remove ccu from ~/.local/bin
```

## Keys

| Key | Action |
|---|---|
| `r` | Immediate refresh |
| `w` / `s` | Adjust refresh interval (w=+5s, s=-5s, 3s–120s) |
| `a` / `d` | Adjust bar width (a=-5, d=+5) |
| `e` | Toggle pace bar (elapsed time comparison) |
| `q` | Toggle profile info |
| `h` / `ESC` | Toggle help |
| `Ctrl+C` | Exit |

## How it works

1. Starts Claude Code in a hidden tmux session
2. Sends `/usage` command periodically
3. Parses the TUI output for usage percentages and reset times
4. Displays a clean dashboard with progress bars and pace comparison

## License

MIT
