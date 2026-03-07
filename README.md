# claude-simple-usage (`ccu`)

Real-time Claude Code usage monitor. Internally runs `claude /usage` via a hidden tmux session, so the numbers are **identical to what Claude Code itself shows** — no estimation or approximation.

On top of that, pace bars let you compare your current usage against elapsed time in the billing cycle, so you can tell at a glance whether you're on track or burning too fast.

### Vanilla mode (`--no-pace --no-profile`)

Same data as Claude Code's `/usage`, just auto-refreshing:

![vanilla](docs/vanilla.png)

### Full mode (default)

Adds pace bars and profile info. Supports monitoring multiple profiles simultaneously:

![full](docs/default.png)

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

## Install as a command

```bash
python3 ccu.py install
```

You can bake in preset flags:

```bash
python3 ccu.py install --no-pace -d ~/.claude-personal
```

This generates a wrapper script in `~/.local/bin/ccu` with your flags built in. You can still pass additional flags at runtime. To remove:

```bash
ccu uninstall
```

Make sure `~/.local/bin` is in your `PATH`.

## Requirements

- **tmux** — `brew install tmux` / `sudo apt install tmux`
- **claude** — [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)

## Usage

```bash
ccu                            # refresh every 600s (default)
ccu 900                        # refresh every 900s
ccu -d ~/.claude               # use specific config directory
ccu --config-dir ~/.claude     # (same, long form)
ccu --no-pace                  # start with pace bar hidden
ccu --no-profile               # start with profile info hidden
ccu --no-sonnet                # hide Sonnet weekly usage
ccu --debug                    # show raw tmux output
ccu install                    # install ccu to ~/.local/bin
ccu install --no-pace          # install with preset flags
ccu uninstall                  # remove ccu from ~/.local/bin
```

## Keys

| Key | Action |
|---|---|
| `r` | Immediate refresh |
| `w` / `s` | Adjust refresh interval (w=+30s, s=-30s, 600s–1200s) |
| `a` / `d` | Adjust bar width (a=-5, d=+5) |
| `` ` `` | Toggle all details on/off |
| `1` | Toggle pace bar |
| `2` | Toggle profile info |
| `3` | Toggle sonnet weekly |
| `h` / `ESC` | Toggle help |
| `Ctrl+C` | Exit |

## Rate Limit

Anthropic enforces strict rate limits on the Claude API. The minimum refresh interval is set to **600 seconds (10 minutes)** to avoid triggering these limits. Setting it lower may result in rate-limited or failed requests.

## How it works

1. Starts Claude Code in a hidden tmux session
2. Sends `/usage` command periodically
3. Parses the TUI output for usage percentages and reset times
4. Displays a clean dashboard with progress bars and pace comparison

## License

MIT
