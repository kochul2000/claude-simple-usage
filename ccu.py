#!/usr/bin/env python3
"""
ccu - Claude Code Usage monitor

Runs Claude Code's /usage command in a hidden tmux session
and displays server-side usage data in real time.

Usage:
    ccu                # refresh every 30s (default)
    ccu 15             # refresh every 15s
    ccu --debug        # show raw tmux output
    ccu --once         # fetch once and exit

Keys:
    Space/Enter  immediate refresh
    ↑/↓          adjust refresh interval (±5s)
    ←/→          adjust bar width (±5)
    Ctrl+C       exit

Requirements:
    - tmux
    - claude (Claude Code CLI)
"""

import subprocess
import shutil
import re
import time
import os
import sys
import signal
import termios
import tty
import select
import threading
from datetime import datetime

# ─── Configuration ───────────────────────────────────────────
TMUX_SESSION = "_ccm_bg"
DEFAULT_REFRESH = 30
MIN_REFRESH = 3
MAX_REFRESH = 120
REFRESH_STEP = 5
DEFAULT_BAR_WIDTH = 40
MIN_BAR_WIDTH = 15
MAX_BAR_WIDTH = 80
BAR_WIDTH_STEP = 5
CLAUDE_STARTUP_WAIT = 12
USAGE_RENDER_WAIT = 5
MAX_RETRIES = 3

# ─── Global state ────────────────────────────────────────────
DEBUG = False
ONCE = False


class UsageData:
    """Parsed /usage data container"""
    def __init__(self):
        self.session_pct = None
        self.session_reset = None
        self.week_all_pct = None
        self.week_all_reset = None
        self.week_sonnet_pct = None
        self.raw = ""
        self.timestamp = datetime.now().strftime("%H:%M:%S")
        self.error = None
        self.parse_success = False


# ─── System checks ───────────────────────────────────────────

def check_dependencies():
    """tmux와 claude CLI 존재 여부 확인"""
    missing = []
    if not shutil.which("tmux"):
        missing.append(("tmux", "brew install tmux / sudo apt install tmux"))
    if not shutil.which("claude"):
        missing.append(("claude", "npm install -g @anthropic-ai/claude-code"))

    if missing:
        print("\033[31m❌ Missing dependencies:\033[0m")
        for name, install in missing:
            print(f"   {name}: {install}")
        sys.exit(1)


def is_session_alive():
    """tmux 세션이 살아있는지 확인"""
    r = subprocess.run(
        ["tmux", "has-session", "-t", TMUX_SESSION],
        capture_output=True
    )
    return r.returncode == 0


# ─── tmux helpers ────────────────────────────────────────────

def tmux(*args):
    """tmux 명령 실행"""
    return subprocess.run(
        ["tmux"] + list(args),
        capture_output=True, text=True
    )


def tmux_capture():
    """현재 tmux 패인 내용 캡처"""
    r = tmux("capture-pane", "-t", TMUX_SESSION, "-p", "-S", "-50")
    return r.stdout if r.returncode == 0 else ""


def tmux_send(keys, enter=False):
    """tmux 세션에 키 입력 전송.
    특수 키(Enter, Escape, Tab, Right, Left 등)는 그대로,
    일반 텍스트는 -l 옵션으로 리터럴 전송.
    """
    SPECIAL_KEYS = {"Enter", "Escape", "Tab", "Right", "Left", "Up", "Down",
                    "BSpace", "Space", "C-c", "C-d"}

    if keys in SPECIAL_KEYS:
        tmux("send-keys", "-t", TMUX_SESSION, keys)
    elif enter:
        tmux("send-keys", "-t", TMUX_SESSION, "-l", keys)
        time.sleep(0.1)
        tmux("send-keys", "-t", TMUX_SESSION, "Enter")
    else:
        tmux("send-keys", "-t", TMUX_SESSION, "-l", keys)


# ─── Claude session management ───────────────────────────────

def setup_claude_session():
    """숨겨진 tmux 세션에서 Claude Code 시작"""
    # 기존 세션 정리
    if is_session_alive():
        tmux("kill-session", "-t", TMUX_SESSION)
        time.sleep(0.5)

    # 큰 터미널 사이즈로 세션 생성 (TUI 렌더링을 위해)
    tmux("new-session", "-d", "-s", TMUX_SESSION, "-x", "200", "-y", "60")

    # Claude Code 시작
    tmux_send("claude", enter=True)

    # 초기화 대기
    print(f"⏳ Initializing Claude Code on tmux ({TMUX_SESSION})", end="", flush=True)
    for i in range(CLAUDE_STARTUP_WAIT):
        time.sleep(1)
        print(".", end="", flush=True)
        output = tmux_capture()
        # Welcome 메시지나 프롬프트 감지
        if any(kw in output for kw in ["Welcome", "❯", "Opus", "Sonnet", "Haiku"]):
            time.sleep(2)
            print(" ✅")
            return True

    print(" ⚠️  (timeout, continuing anyway)")
    return True


def ensure_session():
    """세션이 살아있는지 확인하고, 죽었으면 재시작"""
    if not is_session_alive():
        print("\033[33m⚠️  Restarting session...\033[0m")
        setup_claude_session()


# ─── Usage query & parsing ───────────────────────────────────

def query_usage():
    """
    Claude Code에 /usage 명령을 보내고 결과를 파싱.
    /usage는 서버 사이드 데이터를 직접 조회하므로 정확함.
    """
    data = UsageData()
    data.timestamp = datetime.now().strftime("%H:%M:%S")

    ensure_session()

    try:
        # Step 1: /usage 입력 (Enter를 분리해서 보내야 자동완성 처리됨)
        tmux_send("/usage")
        time.sleep(0.5)          # 자동완성 드롭다운 렌더링 대기
        tmux_send("Enter")       # 자동완성에서 /usage 선택 → 실행
        time.sleep(USAGE_RENDER_WAIT)

        # Step 2: /usage TUI는 탭 구조 (Settings | Status | Config | Usage)
        #         Usage 탭으로 이동해야 함 → Right 키 3번 또는 Tab 3번
        for _ in range(3):
            tmux_send("Right")
            time.sleep(0.3)
        time.sleep(1)            # Usage 탭 렌더링 대기

        # Step 3: 화면 캡처
        raw = tmux_capture()
        data.raw = raw

        if DEBUG:
            debug_output(raw)

        # Step 4: 파싱
        parse_usage(raw, data)

        # 파싱 실패 시 Tab으로 탭 이동 재시도
        if not data.parse_success:
            for retry in range(MAX_RETRIES):
                tmux_send("Tab")
                time.sleep(1.5)
                raw = tmux_capture()
                data.raw = raw
                if DEBUG:
                    debug_output(raw)
                parse_usage(raw, data)
                if data.parse_success:
                    break

        # Step 5: Escape로 TUI 닫기
        tmux_send("Escape")
        time.sleep(0.5)
        tmux_send("Escape")
        time.sleep(0.3)

    except Exception as e:
        data.error = str(e)

    return data


def parse_usage(text, data):
    """
    /usage 출력에서 퍼센트와 리셋 시간을 추출.

    Expected format:
      Current session
      ████████████████                                 30% used
      Resets 10pm (Asia/Seoul)

      Current week (all models)
      █████▌                                           11% used
      Resets Mar 6, 11:59am (Asia/Seoul)

      Current week (Sonnet only)
                                                       0% used
    """
    lines = text.split('\n')
    found_any = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # ── Current session ──
        if 'Current session' in stripped and 'week' not in stripped.lower():
            pct, reset = _extract_pct_and_reset(lines, i)
            if pct is not None:
                data.session_pct = pct
                found_any = True
            if reset:
                data.session_reset = reset

        # ── Current week (all models) ──
        elif re.search(r'all\s*models', stripped, re.IGNORECASE):
            pct, reset = _extract_pct_and_reset(lines, i)
            if pct is not None:
                data.week_all_pct = pct
                found_any = True
            if reset:
                data.week_all_reset = reset

        # ── Current week (Sonnet only) ──
        elif re.search(r'sonnet', stripped, re.IGNORECASE):
            pct, reset = _extract_pct_and_reset(lines, i)
            if pct is not None:
                data.week_sonnet_pct = pct
                found_any = True
            elif data.week_sonnet_pct is None:
                # 0%인 경우 바가 비어있어서 숫자가 안 보일 수 있음
                data.week_sonnet_pct = 0
                found_any = True

    data.parse_success = found_any


def _extract_pct_and_reset(lines, start_idx):
    """
    주어진 위치 이후 3줄 내에서 퍼센트와 리셋 정보를 추출.
    """
    pct = None
    reset = None

    for j in range(start_idx, min(start_idx + 5, len(lines))):
        text = lines[j]

        # 퍼센트 추출: "30% used" 또는 "30%"
        m = re.search(r'(\d+)%', text)
        if m and pct is None:
            pct = int(m.group(1))

        # 리셋 시간 추출: "Resets 10pm (Asia/Seoul)" 등
        r = re.search(r'Resets\s+(.+?)(?:\s*$)', text)
        if r and reset is None:
            reset = r.group(1).strip()

    return pct, reset


# ─── Display ─────────────────────────────────────────────────

def make_bar(pct, width=40):
    """컬러 프로그레스 바 생성"""
    if pct is None:
        return f"\033[2m{'·' * width}\033[0m   --"

    filled = int(width * pct / 100)
    empty = width - filled

    if pct < 50:
        color = "\033[32m"
    elif pct < 80:
        color = "\033[33m"
    else:
        color = "\033[31m"

    bar = f"{color}{'█' * filled}{'░' * empty}\033[0m"
    return f"{bar} {pct:3d}% used"


def display(data, bar_width):
    """Dashboard display"""
    sys.stdout.write("\033[H\033[2J")

    # Header
    print(f"  \033[1mClaude Code Usage Monitor\033[0m{' ' * 17}\033[2m{data.timestamp}\033[0m")
    print()

    if data.error:
        print(f"  \033[31m❌ Error: {data.error}\033[0m")
        print()

    if not data.parse_success:
        print(f"  \033[33m⚠️  Could not parse /usage data yet.\033[0m")
        print(f"  \033[2m   Will retry on next refresh...\033[0m")
        print()
        if DEBUG:
            print(f"  \033[2mRaw output ({len(data.raw)} chars):\033[0m")
            for line in data.raw.split('\n')[:20]:
                print(f"  \033[2m  {repr(line)}\033[0m")
        print()
        sys.stdout.flush()
        return

    # ── Current Session ──
    print(f"  \033[1mCurrent Session\033[0m")
    print(f"  {make_bar(data.session_pct, bar_width)}")
    if data.session_reset:
        print(f"  Resets {data.session_reset}")
    print()

    # ── Current Week (All Models) ──
    print(f"  \033[1mCurrent Week (All Models)\033[0m")
    print(f"  {make_bar(data.week_all_pct, bar_width)}")
    if data.week_all_reset:
        print(f"  Resets {data.week_all_reset}")
    print()

    # ── Current Week (Sonnet) ──
    print(f"  \033[1mCurrent Week (Sonnet Only)\033[0m")
    print(f"  {make_bar(data.week_sonnet_pct, bar_width)}")
    print()

    sys.stdout.flush()


def debug_output(raw):
    """디버그: raw tmux capture 출력"""
    print("\n\033[35m─── DEBUG: Raw tmux capture ───\033[0m")
    for i, line in enumerate(raw.split('\n')):
        print(f"\033[2m  {i:3d}: {repr(line)}\033[0m")
    print(f"\033[35m─── END DEBUG ({len(raw)} chars) ───\033[0m\n")


# ─── Input ───────────────────────────────────────────────────

def _read_key():
    """Read a keypress in cbreak mode.
    Returns 'up', 'down', 'left', 'right', or the raw character."""
    ch = sys.stdin.read(1)
    if ch == '\x1b':
        if select.select([sys.stdin], [], [], 0.15)[0]:
            seq = sys.stdin.read(1)
            if seq == '[' and select.select([sys.stdin], [], [], 0.15)[0]:
                code = sys.stdin.read(1)
                if code == 'A':
                    return 'up'
                if code == 'B':
                    return 'down'
                if code == 'C':
                    return 'right'
                if code == 'D':
                    return 'left'
        return None  # ESC or unknown sequence → ignore
    return ch


# ─── Lifecycle ───────────────────────────────────────────────

def cleanup(sig=None, frame=None):
    """Clean up on exit"""
    # Restore cursor and terminal
    sys.stdout.write("\033[?25h")
    try:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN,
                          termios.tcgetattr(sys.stdin))
    except Exception:
        pass
    print(f"\n\033[33mClosing background tmux session ({TMUX_SESSION})...\033[0m")

    try:
        if is_session_alive():
            tmux_send("/exit", enter=True)
            time.sleep(1)
            tmux("kill-session", "-t", TMUX_SESSION)
    except Exception:
        pass

    print("\033[32mDone\033[0m")
    sys.exit(0)


def main():
    global DEBUG, ONCE

    # ── Parse args ──
    refresh_sec = DEFAULT_REFRESH
    args = sys.argv[1:]

    for arg in args:
        if arg == "--debug":
            DEBUG = True
        elif arg == "--once":
            ONCE = True
        elif arg == "--help" or arg == "-h":
            print(__doc__)
            sys.exit(0)
        elif arg.isdigit():
            refresh_sec = int(arg)

    # ── Setup ──
    check_dependencies()
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print("\033[1mClaude Code Usage Monitor\033[0m")
    print(f"   Refresh: {refresh_sec}s | Debug: {DEBUG} | Once: {ONCE}")
    print()

    setup_claude_session()

    # 커서 숨기기
    sys.stdout.write("\033[?25l")

    if ONCE:
        data = query_usage()
        sys.stdout.write("\033[?25h")
        display(data, DEFAULT_BAR_WIDTH)
        cleanup()
    else:
        # Initial query (blocking — setup phase covers the wait)
        data = query_usage()
        bar_width = DEFAULT_BAR_WIDTH
        redraw = True

        while True:
            if redraw:
                display(data, bar_width)
                redraw = False
            old_settings = termios.tcgetattr(sys.stdin)
            try:
                tty.setcbreak(sys.stdin.fileno())
                start = time.time()

                # Phase 1: Countdown
                while True:
                    remaining = refresh_sec - (time.time() - start)
                    if remaining <= 0:
                        break
                    if select.select([sys.stdin], [], [], 0)[0]:
                        key = _read_key()
                        if key == 'up':
                            refresh_sec = max(MIN_REFRESH, refresh_sec - REFRESH_STEP)
                        elif key == 'down':
                            refresh_sec = min(MAX_REFRESH, refresh_sec + REFRESH_STEP)
                        elif key == 'right':
                            bar_width = min(MAX_BAR_WIDTH, bar_width + BAR_WIDTH_STEP)
                            display(data, bar_width)
                        elif key == 'left':
                            bar_width = max(MIN_BAR_WIDTH, bar_width - BAR_WIDTH_STEP)
                            display(data, bar_width)
                        elif key in (' ', '\r', '\n'):
                            break
                        continue
                    secs = int(remaining) + 1
                    sys.stdout.write(
                        f"\r  \033[2mRefresh: {refresh_sec}s · Next in {secs}s\033[0m{' ' * 20}"
                    )
                    sys.stdout.flush()
                    time.sleep(0.1)

                # Phase 2: Refresh in background thread
                result = [None]
                def _bg_query():
                    result[0] = query_usage()
                thread = threading.Thread(target=_bg_query, daemon=True)
                thread.start()

                tick = 0
                while thread.is_alive():
                    if select.select([sys.stdin], [], [], 0)[0]:
                        key = _read_key()
                        if key == 'up':
                            refresh_sec = max(MIN_REFRESH, refresh_sec - REFRESH_STEP)
                        elif key == 'down':
                            refresh_sec = min(MAX_REFRESH, refresh_sec + REFRESH_STEP)
                        elif key == 'right':
                            bar_width = min(MAX_BAR_WIDTH, bar_width + BAR_WIDTH_STEP)
                        elif key == 'left':
                            bar_width = max(MIN_BAR_WIDTH, bar_width - BAR_WIDTH_STEP)
                    dots = "." * (tick // 4 % 3 + 1)
                    pad = "." * (3 - len(dots))
                    sys.stdout.write(
                        f"\r  \033[2mRefresh: {refresh_sec}s · Refreshing{dots}\033[0m\033[2m{pad}\033[0m{' ' * 20}"
                    )
                    sys.stdout.flush()
                    tick += 1
                    time.sleep(0.1)

                data = result[0]
                redraw = True
            finally:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    main()
