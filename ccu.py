#!/usr/bin/env python3
"""
ccu - Claude Code Usage monitor

Runs Claude Code's /usage command in a hidden tmux session
and displays server-side usage data in real time.

Usage:
    ccu                          # refresh every 600s (default)
    ccu 900                      # refresh every 900s
    ccu -d ~/.claude             # use specific config directory
    ccu --config-dir ~/.claude   # (same, long form)
    ccu --no-pace                # start with pace bar hidden
    ccu --no-profile             # start with profile info hidden
    ccu --no-sonnet              # hide Sonnet weekly usage
    ccu --no-refresh             # start with refresh status hidden
    ccu --horizontal             # horizontal layout (side by side)
    ccu --width 60               # set initial bar width (default: 40)
    ccu --debug                  # show raw tmux output
    ccu install                  # install ccu to ~/.local/bin
    ccu install --no-pace        # install with preset flags
    ccu uninstall                # remove ccu from ~/.local/bin

Keys:
    r            immediate refresh
    w/s          adjust refresh interval (w=+30s, s=-30s)
    a/d          adjust bar width (a=-1, d=+1)
    `            toggle all details
    1            toggle pace bar
    2            toggle title & profile info
    3            toggle sonnet weekly
    4            toggle refresh status
    e            toggle horizontal layout
    t            show tmux pane capture (debug)
    !            force restart Claude session
    h/ESC        toggle help
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
import atexit
import json
import fcntl
import hashlib
from datetime import datetime, timedelta

# ─── Configuration ───────────────────────────────────────────
TMUX_SESSION = None  # Set in main() based on profile
DEFAULT_REFRESH = 600
MIN_REFRESH = 600
MAX_REFRESH = 1200
REFRESH_STEP = 30
DEFAULT_BAR_WIDTH = 40
MIN_BAR_WIDTH = 15
MAX_BAR_WIDTH = 200
BAR_WIDTH_STEP = 1
SESSION_HOURS = 5
CLAUDE_STARTUP_WAIT = 12
USAGE_RENDER_WAIT = 5
MAX_RETRIES = 3
PARSE_FAIL_REFRESH = 10
MAX_CONSECUTIVE_FAILURES = 5
QUERY_TIMEOUT = 30

# ─── Global state ────────────────────────────────────────────
DEBUG = False
CONFIG_DIR = None
ACCOUNT_INFO = {}  # {"account": ..., "plan": ...}


# ─── Profile-based session sharing ──────────────────────────

def _profile_key():
    """Return a short deterministic key for the current profile (config dir)."""
    path = CONFIG_DIR or os.path.join(os.path.expanduser("~"), ".claude")
    return hashlib.md5(os.path.realpath(path).encode()).hexdigest()[:8]


def _shared_dir():
    d = f"/tmp/_ccu_{_profile_key()}"
    os.makedirs(d, exist_ok=True)
    return d


def _pid_dir():
    d = os.path.join(_shared_dir(), "pids")
    os.makedirs(d, exist_ok=True)
    return d


def _data_file():
    return os.path.join(_shared_dir(), "data.json")


def _lock_file():
    return os.path.join(_shared_dir(), "query.lock")


def _account_file():
    return os.path.join(_shared_dir(), "account.json")


def _register_pid():
    with open(os.path.join(_pid_dir(), str(os.getpid())), 'w') as f:
        f.write('')


def _unregister_pid():
    try:
        os.unlink(os.path.join(_pid_dir(), str(os.getpid())))
    except FileNotFoundError:
        pass


def _alive_pids():
    """Return set of alive ccu PIDs using this profile's session."""
    pids = set()
    try:
        entries = os.listdir(_pid_dir())
    except FileNotFoundError:
        return pids
    for name in entries:
        try:
            pid = int(name)
            os.kill(pid, 0)
            pids.add(pid)
        except (ValueError, OSError):
            try:
                os.unlink(os.path.join(_pid_dir(), name))
            except FileNotFoundError:
                pass
    return pids


def _read_shared_data():
    try:
        with open(_data_file(), 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return None


def _write_shared_data(data, next_query_at):
    d = {
        'queried_at': time.time(),
        'next_query_at': next_query_at,
        'session_pct': data.session_pct,
        'session_reset': data.session_reset,
        'week_all_pct': data.week_all_pct,
        'week_all_reset': data.week_all_reset,
        'week_sonnet_pct': data.week_sonnet_pct,
        'raw': data.raw,
        'timestamp': data.timestamp,
        'error': data.error,
        'parse_success': data.parse_success,
    }
    tmp = _data_file() + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(d, f)
    os.replace(tmp, _data_file())


def _shared_to_usage(d):
    data = UsageData()
    data.session_pct = d.get('session_pct')
    data.session_reset = d.get('session_reset')
    data.week_all_pct = d.get('week_all_pct')
    data.week_all_reset = d.get('week_all_reset')
    data.week_sonnet_pct = d.get('week_sonnet_pct')
    data.raw = d.get('raw', '')
    data.timestamp = d.get('timestamp')
    data.error = d.get('error')
    data.parse_success = d.get('parse_success', False)
    return data


def _save_account_info():
    if ACCOUNT_INFO:
        tmp = _account_file() + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(ACCOUNT_INFO, f)
        os.replace(tmp, _account_file())


def _load_account_info():
    try:
        with open(_account_file(), 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


class UsageData:
    """Parsed /usage data container"""
    def __init__(self):
        self.session_pct = None
        self.session_reset = None
        self.week_all_pct = None
        self.week_all_reset = None
        self.week_sonnet_pct = None
        self.raw = ""
        self.timestamp = None
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
        print("\033[31mMissing dependencies:\033[0m")
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


def cleanup_zombie_sessions():
    """등록된 PID가 모두 죽은 _ccu_bg_* 좀비 tmux 세션 정리"""
    r = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return
    killed = []
    for name in r.stdout.strip().split('\n'):
        if not name.startswith("_ccu_bg_"):
            continue
        profile_hash = name[len("_ccu_bg_"):]
        if TMUX_SESSION and name == TMUX_SESSION:
            continue
        pid_dir = f"/tmp/_ccu_{profile_hash}/pids"
        if not os.path.isdir(pid_dir):
            subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
            shared_dir = f"/tmp/_ccu_{profile_hash}"
            try:
                shutil.rmtree(shared_dir)
            except Exception:
                pass
            killed.append(name)
            continue
        any_alive = False
        for pid_name in os.listdir(pid_dir):
            try:
                pid = int(pid_name)
                os.kill(pid, 0)
                any_alive = True
                break
            except (ValueError, OSError):
                try:
                    os.unlink(os.path.join(pid_dir, pid_name))
                except FileNotFoundError:
                    pass
        if not any_alive:
            subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
            shared_dir = f"/tmp/_ccu_{profile_hash}"
            try:
                shutil.rmtree(shared_dir)
            except Exception:
                pass
            killed.append(name)
    if killed:
        print(f"\033[2mCleaned up {len(killed)} orphaned session(s)\033[0m")


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


def parse_account_info(text):
    """Parse account/plan info from claude welcome screen.
    Expected format:  Opus 4.6 · Claude Max
    """
    info = {}
    for line in text.split('\n'):
        stripped = line.strip()
        # "Opus 4.6 · Claude Max" or "Sonnet 4 · Claude Pro"
        m = re.search(r'((?:Opus|Sonnet|Haiku)\s+[\d.]+)\s*·\s*(Claude\s+\w+(?:\s+\w+)?)', stripped)
        if m:
            info['model'] = m.group(1).strip()
            info['plan'] = m.group(2).strip()

            break
    return info


# ─── Claude session management ───────────────────────────────

def setup_claude_session():
    """숨겨진 tmux 세션에서 Claude Code 시작"""
    # 기존 세션 정리
    if is_session_alive():
        tmux("kill-session", "-t", TMUX_SESSION)
        time.sleep(0.5)

    # 큰 터미널 사이즈로 세션 생성 (TUI 렌더링을 위해)
    tmux("new-session", "-d", "-s", TMUX_SESSION, "-x", "200", "-y", "60")

    # PID watchdog: 모든 ccu 프로세스가 죽으면 tmux 세션 자동 종료
    pid_dir = _pid_dir()
    watchdog = (
        f'(while true; do sleep 10; a=0; '
        f'for f in {pid_dir}/*; do '
        f'[ -f "$f" ] && kill -0 "$(basename "$f")" 2>/dev/null && a=1 && break; '
        f'done; [ "$a" = 0 ] && tmux kill-session -t {TMUX_SESSION} && break; done) &'
    )
    tmux_send(watchdog, enter=True)
    time.sleep(0.3)

    # Claude Code 시작
    cmd = "claude"
    if CONFIG_DIR:
        cmd = f"CLAUDE_CONFIG_DIR={CONFIG_DIR} claude"
    tmux_send(cmd, enter=True)

    # 초기화 대기
    print(f"Initializing Claude Code on tmux ({TMUX_SESSION})", end="", flush=True)
    trust_handled = False
    for i in range(CLAUDE_STARTUP_WAIT):
        time.sleep(1)
        print(".", end="", flush=True)
        output = tmux_capture()

        # Trust prompt 감지 → Enter로 승인
        if not trust_handled and "trust this folder" in output.lower():
            tmux_send("Enter")
            trust_handled = True
            time.sleep(2)
            continue

        # Welcome 메시지 감지 (trust prompt의 ❯와 구분)
        if "Welcome" in output or any(
            kw in output for kw in ["Opus", "Sonnet", "Haiku"]
        ):
            time.sleep(2)
            output = tmux_capture()
            ACCOUNT_INFO.update(parse_account_info(output))
            print(" ok")
            return True

    # 타임아웃 — 실패 처리
    output = tmux_capture()
    print()
    if CONFIG_DIR:
        print(f"\033[31mFailed to start Claude Code with --config-dir {CONFIG_DIR}\033[0m")
    else:
        print(f"\033[31mFailed to start Claude Code (timeout after {CLAUDE_STARTUP_WAIT}s)\033[0m")
    # 원인 파악을 위해 tmux 출력 표시
    stripped = output.strip()
    if stripped:
        print(f"\033[2mOutput:\033[0m")
        for line in stripped.split('\n')[-15:]:
            if line.strip():
                print(f"\033[2m  {line}\033[0m")
    tmux("kill-session", "-t", TMUX_SESSION)
    sys.exit(1)


def is_claude_alive():
    """tmux 세션이 살아있고 Claude가 실행 중인지 확인"""
    if not is_session_alive():
        return False
    output = tmux_capture().strip()
    return bool(output)


# ─── Usage query & parsing ───────────────────────────────────

def query_usage():
    """
    Claude Code에 /usage 명령을 보내고 결과를 파싱.
    /usage는 서버 사이드 데이터를 직접 조회하므로 정확함.
    """
    data = UsageData()

    if not is_claude_alive():
        data.error = "session_dead"
        return data

    try:
        # Step 0: 혹시 남아있는 dialog/popup 닫기
        tmux_send("Escape")
        time.sleep(0.3)

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


def query_usage_shared(refresh_sec, force=False):
    """Query usage with inter-process coordination.
    All instances share next_query_at — only the first to arrive actually queries.
    """
    if not force:
        shared = _read_shared_data()
        if shared and shared.get('next_query_at', 0) > time.time():
            return _shared_to_usage(shared)

    lock_fd = open(_lock_file(), 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        if not force:
            shared = _read_shared_data()
            if shared and shared.get('next_query_at', 0) > time.time():
                return _shared_to_usage(shared)

        data = query_usage()
        next_at = time.time() + (refresh_sec if data.parse_success else PARSE_FAIL_REFRESH)
        _write_shared_data(data, next_at)
        return data
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


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

    # 서버 에러 감지
    for line in lines:
        if 'Error:' in line or 'Failed to load' in line:
            data.error = line.strip()
            break

    data.parse_success = found_any
    if found_any:
        data.timestamp = datetime.now().strftime("%H:%M:%S")


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

def strip_ansi(text):
    """Remove ANSI escape codes for visible width calculation"""
    return re.sub(r'\033\[[0-9;]*m', '', text)


def make_bar(pct, width=40):
    """컬러 프로그레스 바 생성"""
    if pct is None:
        return f"\033[2m{'·' * width}\033[0m   --"

    filled = int(width * pct / 100)
    empty = width - filled

    bar = f"\033[96m{'█' * filled}\033[0m\033[38;5;240m{'█' * empty}\033[0m"
    return f"{bar} \033[2m{pct:3d}% used\033[0m"


def _parse_ampm_time(hour, minute, ampm):
    """Convert 12h to 24h."""
    if ampm == 'pm' and hour != 12:
        hour += 12
    elif ampm == 'am' and hour == 12:
        hour = 0
    return hour, minute


def calc_session_elapsed(reset_str):
    """Calculate elapsed % of current session window from reset time."""
    m = re.match(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)', reset_str.strip(), re.IGNORECASE)
    if not m:
        return None
    hour, minute = _parse_ampm_time(
        int(m.group(1)), int(m.group(2)) if m.group(2) else 0, m.group(3).lower()
    )

    now = datetime.now()
    reset_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset_dt <= now:
        reset_dt += timedelta(days=1)

    start_dt = reset_dt - timedelta(hours=SESSION_HOURS)
    elapsed = (now - start_dt).total_seconds() / (SESSION_HOURS * 3600) * 100
    return max(0, min(100, int(elapsed)))


WEEK_DAYS = 7

def calc_week_elapsed(reset_str):
    """Calculate elapsed % of current week window from reset time.
    reset_str format: 'Mar 6, 11:59am (Asia/Seoul)' or '11:59am (Asia/Seoul)'
    """
    # Try date+time format first: 'Mar 6, 11:59am'
    m = re.match(
        r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)',
        reset_str.strip(), re.IGNORECASE
    )
    if m:
        month_str = m.group(1)
        day = int(m.group(2))
        hour, minute = _parse_ampm_time(
            int(m.group(3)), int(m.group(4)) if m.group(4) else 0, m.group(5).lower()
        )

        now = datetime.now()
        try:
            month = datetime.strptime(month_str, "%b").month
        except ValueError:
            return None

        year = now.year
        reset_dt = datetime(year, month, day, hour, minute, 0)
        if reset_dt < now - timedelta(days=WEEK_DAYS):
            reset_dt = reset_dt.replace(year=year + 1)

        start_dt = reset_dt - timedelta(days=WEEK_DAYS)
        total = WEEK_DAYS * 24 * 3600
        elapsed = (now - start_dt).total_seconds() / total * 100
        return max(0, min(100, int(elapsed)))

    # Fallback: time-only format: '11:59am (Asia/Seoul)'
    m = re.match(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)', reset_str.strip(), re.IGNORECASE)
    if not m:
        return None

    hour, minute = _parse_ampm_time(
        int(m.group(1)), int(m.group(2)) if m.group(2) else 0, m.group(3).lower()
    )

    now = datetime.now()
    reset_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset_dt <= now:
        reset_dt += timedelta(days=1)

    start_dt = reset_dt - timedelta(days=WEEK_DAYS)
    total = WEEK_DAYS * 24 * 3600
    elapsed = (now - start_dt).total_seconds() / total * 100
    return max(0, min(100, int(elapsed)))


def make_pace_bar(pct, width=40):
    """Pace bar showing elapsed time percentage. Yellow thin style."""
    if pct is None:
        return f"\033[38;5;240m{'─' * width}\033[0m   --"
    filled = int(width * pct / 100)
    empty = width - filled
    bar = f"\033[33m{'━' * filled}\033[38;5;240m{'─' * empty}\033[0m"
    return f"{bar} \033[33m{pct:3d}% elapsed\033[0m"


def display_help():
    """Show key bindings help screen"""
    sys.stdout.write("\033[H\033[2J")
    print(f"  \033[1mClaude Simple Usage\033[0m")
    print()
    print(f"  \033[1mKeys\033[0m")
    print(f"  \033[2m{'r':12s}\033[0m immediate refresh")
    print(f"  \033[2m{'w/s':12s}\033[0m adjust refresh interval (w=+30s, s=-30s)")
    print(f"  \033[2m{'a/d':12s}\033[0m adjust bar width (a=-5, d=+5)")
    print(f"  \033[2m{'`':12s}\033[0m toggle all details")
    print(f"  \033[2m{'1':12s}\033[0m toggle pace bar")
    print(f"  \033[2m{'2':12s}\033[0m toggle title & profile info")
    print(f"  \033[2m{'3':12s}\033[0m toggle sonnet weekly")
    print(f"  \033[2m{'4':12s}\033[0m toggle refresh status")
    print(f"  \033[2m{'e':12s}\033[0m toggle horizontal layout")
    print(f"  \033[2m{'t':12s}\033[0m show tmux pane capture")
    print(f"  \033[2m{'!':12s}\033[0m force restart session")
    print(f"  \033[2m{'h/ESC':12s}\033[0m toggle this help")
    print(f"  \033[2m{'Ctrl+C':12s}\033[0m exit")
    print()
    sys.stdout.flush()


def display_tmux_capture():
    """현재 tmux 패인 내용을 화면에 표시 (디버그용)"""
    sys.stdout.write("\033[H\033[2J")
    print(f"  \033[1mTmux Capture\033[0m  \033[2m({TMUX_SESSION})\033[0m")
    print()
    if is_session_alive():
        raw = tmux_capture()
        if raw.strip():
            for line in raw.split('\n'):
                print(f"  \033[2m{line}\033[0m")
        else:
            print("  \033[2m(pane is empty)\033[0m")
    else:
        print("  \033[31m(session is dead)\033[0m")
    print()
    print(f"  \033[2mPress any key to return\033[0m")
    sys.stdout.flush()


def display(data, bar_width, show_pace=True, show_profile=True, show_sonnet=True, horizontal=False):
    """Dashboard display"""
    if horizontal:
        return _display_horizontal(data, bar_width, show_pace, show_profile, show_sonnet)
    sys.stdout.write("\033[H\033[2J")

    # Header
    if show_profile:
        print(f"  \033[1mClaude Simple Usage\033[0m")
        info_parts = []
        if ACCOUNT_INFO.get('plan'):
            info_parts.append(ACCOUNT_INFO['plan'])
        config_path = CONFIG_DIR or os.path.join(os.path.expanduser("~"), ".claude")
        info_parts.append(config_path)
        if info_parts:
            print(f"  \033[2m{' · '.join(info_parts)}\033[0m")
        print()

    if data.error and data.error != "session_dead":
        print(f"  \033[31mClaude server error: {data.error}\033[0m")
        print()

    if not data.parse_success:
        if DEBUG and data.raw:
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
        if show_pace:
            elapsed_pct = calc_session_elapsed(data.session_reset)
            print(f"  {make_pace_bar(elapsed_pct, bar_width)}")
        print(f"  \033[2mResets {data.session_reset}\033[0m")
    print()

    # ── Current Week (All Models) ──
    print(f"  \033[1mCurrent Week (All Models)\033[0m")
    print(f"  {make_bar(data.week_all_pct, bar_width)}")
    if data.week_all_reset:
        if show_pace:
            week_elapsed = calc_week_elapsed(data.week_all_reset)
            print(f"  {make_pace_bar(week_elapsed, bar_width)}")
        print(f"  \033[2mResets {data.week_all_reset}\033[0m")
    print()

    # ── Current Week (Sonnet) ──
    if show_sonnet:
        print(f"  \033[1mCurrent Week (Sonnet Only)\033[0m")
        print(f"  {make_bar(data.week_sonnet_pct, bar_width)}")
        print()

    sys.stdout.flush()


def _display_horizontal(data, bar_width, show_pace=True, show_profile=True, show_sonnet=True):
    """Horizontal dashboard display - usage blocks side by side"""
    sys.stdout.write("\033[H\033[2J")

    # Header
    if show_profile:
        print(f"  \033[1mClaude Simple Usage\033[0m")
        info_parts = []
        if ACCOUNT_INFO.get('plan'):
            info_parts.append(ACCOUNT_INFO['plan'])
        config_path = CONFIG_DIR or os.path.join(os.path.expanduser("~"), ".claude")
        info_parts.append(config_path)
        if info_parts:
            print(f"  \033[2m{' · '.join(info_parts)}\033[0m")
        print()

    if data.error and data.error != "session_dead":
        print(f"  \033[31mClaude server error: {data.error}\033[0m")
        print()

    if not data.parse_success:
        if DEBUG and data.raw:
            print(f"  \033[2mRaw output ({len(data.raw)} chars):\033[0m")
            for line in data.raw.split('\n')[:20]:
                print(f"  \033[2m  {repr(line)}\033[0m")
            print()
        sys.stdout.flush()
        return

    # Build columns
    columns = []

    # Column 1: Current Session
    col = []
    col.append(f"\033[1mCurrent Session\033[0m")
    col.append(make_bar(data.session_pct, bar_width))
    if data.session_reset:
        if show_pace:
            elapsed_pct = calc_session_elapsed(data.session_reset)
            col.append(make_pace_bar(elapsed_pct, bar_width))
        col.append(f"\033[2mResets {data.session_reset}\033[0m")
    columns.append(col)

    # Column 2: Current Week (All Models)
    col = []
    col.append(f"\033[1mCurrent Week (All Models)\033[0m")
    col.append(make_bar(data.week_all_pct, bar_width))
    if data.week_all_reset:
        if show_pace:
            week_elapsed = calc_week_elapsed(data.week_all_reset)
            col.append(make_pace_bar(week_elapsed, bar_width))
        col.append(f"\033[2mResets {data.week_all_reset}\033[0m")
    columns.append(col)

    # Column 3: Current Week (Sonnet)
    if show_sonnet:
        col = []
        col.append(f"\033[1mCurrent Week (Sonnet Only)\033[0m")
        col.append(make_bar(data.week_sonnet_pct, bar_width))
        columns.append(col)

    if not columns:
        sys.stdout.flush()
        return

    # Calculate column widths (visible characters only)
    col_widths = []
    for col in columns:
        w = max(len(strip_ansi(line)) for line in col) if col else 0
        col_widths.append(w)

    # Render columns side by side
    max_height = max(len(c) for c in columns)
    gap = 4
    for row in range(max_height):
        parts = []
        for ci, col in enumerate(columns):
            if row < len(col):
                line = col[row]
                visible_len = len(strip_ansi(line))
                padding = col_widths[ci] - visible_len
                parts.append(line + ' ' * padding)
            else:
                parts.append(' ' * col_widths[ci])
        print(f"  {(' ' * gap).join(parts)}")

    print()
    sys.stdout.flush()


def debug_output(raw):
    """디버그: raw tmux capture 출력"""
    print("\n\033[35m─── DEBUG: Raw tmux capture ───\033[0m")
    for i, line in enumerate(raw.split('\n')):
        print(f"\033[2m  {i:3d}: {repr(line)}\033[0m")
    print(f"\033[35m─── END DEBUG ({len(raw)} chars) ───\033[0m\n")


# ─── Lifecycle ───────────────────────────────────────────────

_cleanup_done = False

def cleanup(sig=None, frame=None):
    """Clean up on exit"""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    # Restore cursor and terminal
    sys.stdout.write("\033[?25h")
    try:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN,
                          termios.tcgetattr(sys.stdin))
    except Exception:
        pass

    _unregister_pid()
    remaining = _alive_pids()

    if not remaining:
        print(f"\n\033[33mClosing shared session ({TMUX_SESSION})...\033[0m")
        try:
            if is_session_alive():
                tmux_send("/exit", enter=True)
                time.sleep(1)
                tmux("kill-session", "-t", TMUX_SESSION)
        except Exception:
            pass
        try:
            shutil.rmtree(_shared_dir())
        except Exception:
            pass
        print("\033[32mDone\033[0m")
    else:
        print(f"\n\033[33mDetached ({len(remaining)} instance(s) still active)\033[0m")

    if sig is not None:
        sys.exit(0)


INSTALL_DIR = os.path.expanduser("~/.local/bin")
INSTALL_PATH = os.path.join(INSTALL_DIR, "ccu")


def do_install(extra_args=None):
    """Install ccu to ~/.local/bin as a wrapper script with optional preset flags.

    Usage:
        ccu install                              # plain install
        ccu install --no-pace --config-dir ~/.x   # bake in flags
    """
    src = os.path.abspath(__file__)
    preset = " ".join(extra_args) if extra_args else ""

    os.makedirs(INSTALL_DIR, exist_ok=True)

    if os.path.exists(INSTALL_PATH) or os.path.islink(INSTALL_PATH):
        os.remove(INSTALL_PATH)

    # Generate wrapper script with preset flags
    with open(INSTALL_PATH, "w") as f:
        f.write("#!/bin/sh\n")
        if preset:
            f.write(f'exec python3 "{src}" {preset} "$@"\n')
        else:
            f.write(f'exec python3 "{src}" "$@"\n')
    os.chmod(INSTALL_PATH, 0o755)

    print(f"Installed: {INSTALL_PATH}")
    if preset:
        print(f"  Preset flags: {preset}")
    print(f"  Script: {src}")

    path_dirs = os.environ.get("PATH", "").split(":")
    if INSTALL_DIR not in path_dirs:
        print()
        print(f"\033[33m~/.local/bin is not in PATH. Add this to your shell profile:\033[0m")
        print(f'  export PATH="$HOME/.local/bin:$PATH"')
    else:
        print("Run \033[1mccu\033[0m from anywhere.")


def do_uninstall():
    """Remove ccu from ~/.local/bin."""
    if os.path.exists(INSTALL_PATH) or os.path.islink(INSTALL_PATH):
        os.remove(INSTALL_PATH)
        print(f"Removed: {INSTALL_PATH}")
    else:
        print(f"Not installed: {INSTALL_PATH}")


def main():
    global DEBUG, CONFIG_DIR, TMUX_SESSION

    # ── Parse args ──
    refresh_sec = DEFAULT_REFRESH
    init_pace = True
    init_profile = True
    init_sonnet = True
    init_refresh = True
    init_horizontal = False
    init_bar_width = None
    args = sys.argv[1:]

    # Handle subcommands
    if args and args[0] in ("install", "--install"):
        do_install(args[1:] if len(args) > 1 else None)
        sys.exit(0)
    if args and args[0] in ("uninstall", "--uninstall"):
        do_uninstall()
        sys.exit(0)

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--debug":
            DEBUG = True
        elif arg == "--help" or arg == "-h":
            print(__doc__)
            sys.exit(0)
        elif arg in ("--config-dir", "-d"):
            if i + 1 >= len(args):
                print(f"\033[31m{arg} requires a path argument\033[0m")
                sys.exit(1)
            i += 1
            CONFIG_DIR = os.path.expanduser(args[i])
            if not os.path.isdir(CONFIG_DIR):
                print(f"\033[31mConfig directory not found: {CONFIG_DIR}\033[0m")
                sys.exit(1)
        elif arg == "--no-pace":
            init_pace = False
        elif arg == "--no-profile":
            init_profile = False
        elif arg == "--no-sonnet":
            init_sonnet = False
        elif arg == "--no-refresh":
            init_refresh = False
        elif arg in ("--horizontal", "--wide"):
            init_horizontal = True
        elif arg in ("--width", "-w"):
            if i + 1 >= len(args):
                print(f"\033[31m{arg} requires a number argument\033[0m")
                sys.exit(1)
            i += 1
            try:
                init_bar_width = max(MIN_BAR_WIDTH, min(MAX_BAR_WIDTH, int(args[i])))
            except ValueError:
                print(f"\033[31mInvalid width: {args[i]}\033[0m")
                sys.exit(1)
        elif arg.isdigit():
            refresh_sec = int(arg)
        i += 1

    # ── Setup ──
    TMUX_SESSION = f"_ccu_bg_{_profile_key()}"
    check_dependencies()
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    atexit.register(cleanup)
    cleanup_zombie_sessions()
    _register_pid()

    print("\033[1mClaude Simple Usage\033[0m")
    opts = [f"refresh={refresh_sec}s"]
    if CONFIG_DIR:
        opts.append(f"config-dir={CONFIG_DIR}")
    if not init_pace:
        opts.append("no-pace")
    if not init_profile:
        opts.append("no-profile")
    if not init_sonnet:
        opts.append("no-sonnet")
    if not init_refresh:
        opts.append("no-refresh")
    if init_horizontal:
        opts.append("horizontal")
    if DEBUG:
        opts.append("debug")
    print(f"  \033[2m{' | '.join(opts)}\033[0m")
    print()

    if is_session_alive():
        print(f"Reusing shared session ({TMUX_SESSION})")
        ACCOUNT_INFO.update(_load_account_info())
    else:
        setup_claude_session()
        _save_account_info()

    if DEBUG:
        print()
        print("\033[2m─── DEBUG: Welcome screen capture ───\033[0m")
        output = tmux_capture()
        for line in output.strip().split('\n'):
            if line.strip():
                print(f"\033[2m  {line}\033[0m")
        print(f"\033[2m─── Account info: {ACCOUNT_INFO} ───\033[0m")
        print()
        input("Press Enter to continue...")

    # 커서 숨기기
    sys.stdout.write("\033[?25l")

    data = UsageData()

    # 공유 데이터가 있으면 즉시 로드 (빈 화면 방지)
    shared = _read_shared_data()
    if shared and shared.get('parse_success'):
        data = _shared_to_usage(shared)

    bar_width = init_bar_width if init_bar_width is not None else DEFAULT_BAR_WIDTH
    show_pace = init_pace
    show_profile = init_profile
    show_sonnet = init_sonnet
    show_refresh = init_refresh
    show_horizontal = init_horizontal
    show_help = False
    redraw = True
    first_run = True
    consecutive_failures = 0

    while True:
        if redraw:
            if show_help:
                display_help()
            else:
                display(data, bar_width, show_pace, show_profile, show_sonnet, show_horizontal)
            redraw = False
        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())

            # Phase 1: Countdown (공유 next_query_at 기준 동기화)
            shared = _read_shared_data()
            if shared:
                next_query_at = shared.get('next_query_at') or (shared.get('queried_at', 0) + refresh_sec)
            elif first_run:
                next_query_at = 0  # 최초 실행: 즉시 쿼리
            else:
                next_query_at = time.time() + refresh_sec
            first_run = False
            force_refresh = False
            tick = 0
            while True:
                remaining = next_query_at - time.time()
                if remaining <= 0:
                    break
                if select.select([sys.stdin], [], [], 0)[0]:
                    key = sys.stdin.read(1)
                    if key in ('w', 'W'):
                        refresh_sec = min(MAX_REFRESH, refresh_sec + REFRESH_STEP)
                    elif key in ('s', 'S'):
                        refresh_sec = max(MIN_REFRESH, refresh_sec - REFRESH_STEP)
                    elif key in ('d', 'D'):
                        bar_width = min(MAX_BAR_WIDTH, bar_width + BAR_WIDTH_STEP)
                        display(data, bar_width, show_pace, show_profile, show_sonnet, show_horizontal)
                    elif key in ('a', 'A'):
                        bar_width = max(MIN_BAR_WIDTH, bar_width - BAR_WIDTH_STEP)
                        display(data, bar_width, show_pace, show_profile, show_sonnet, show_horizontal)
                    elif key == '`':
                        if show_pace or show_profile or show_sonnet or show_refresh:
                            show_pace = show_profile = show_sonnet = show_refresh = False
                        else:
                            show_pace = show_profile = show_sonnet = show_refresh = True
                        display(data, bar_width, show_pace, show_profile, show_sonnet, show_horizontal)
                    elif key == '1':
                        show_pace = not show_pace
                        display(data, bar_width, show_pace, show_profile, show_sonnet, show_horizontal)
                    elif key == '2':
                        show_profile = not show_profile
                        show_help = False
                        display(data, bar_width, show_pace, show_profile, show_sonnet, show_horizontal)
                    elif key == '3':
                        show_sonnet = not show_sonnet
                        display(data, bar_width, show_pace, show_profile, show_sonnet, show_horizontal)
                    elif key == '4':
                        show_refresh = not show_refresh
                        display(data, bar_width, show_pace, show_profile, show_sonnet, show_horizontal)
                    elif key in ('e', 'E'):
                        show_horizontal = not show_horizontal
                        display(data, bar_width, show_pace, show_profile, show_sonnet, show_horizontal)
                    elif key in ('h', 'H', '\x1b'):
                        show_help = not show_help
                        if show_help:
                            display_help()
                        else:
                            display(data, bar_width, show_pace, show_profile, show_sonnet, show_horizontal)
                    elif key in ('t', 'T'):
                        display_tmux_capture()
                        sys.stdin.read(1)
                        if show_help:
                            display_help()
                        else:
                            display(data, bar_width, show_pace, show_profile, show_sonnet, show_horizontal)
                    elif key == '!':
                        sys.stdout.write("\033[H\033[2J")  # 화면 클리어
                        sys.stdout.flush()
                        if is_session_alive():
                            tmux("kill-session", "-t", TMUX_SESSION)
                            time.sleep(0.5)
                        setup_claude_session()
                        _save_account_info()
                        consecutive_failures = 0
                        break
                    elif key in ('r', 'R'):
                        force_refresh = True
                        break
                    continue
                if show_refresh:
                    secs = int(remaining) + 1
                    if data.parse_success:
                        sys.stdout.write(
                            f"\r  \033[2mRefresh: {refresh_sec}s · Next in {secs}s · Last: {data.timestamp}\033[0m\033[K"
                        )
                    elif data.error and data.error != "session_dead":
                        # 서버 에러 — 명시적으로 표시
                        sys.stdout.write(
                            f"\r  \033[31mClaude server error\033[0m \033[2m· Retry in {secs}s\033[0m\033[K"
                        )
                    else:
                        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
                        hint = ""
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            hint = " · \033[33mrestarting...\033[0m\033[2m"
                        elif consecutive_failures > 0:
                            hint = f" ({consecutive_failures + 1}/{MAX_CONSECUTIVE_FAILURES})"
                        if data.timestamp:
                            sys.stdout.write(
                                f"\r  \033[2mRefresh: {refresh_sec}s · {spinner[tick % len(spinner)]} Refreshing{hint} · Last: {data.timestamp}\033[0m\033[K"
                            )
                        else:
                            sys.stdout.write(
                                f"\r  \033[2m{spinner[tick % len(spinner)]} Loading usage data{hint}\033[0m\033[K"
                            )
                    sys.stdout.flush()
                tick += 1
                time.sleep(0.1)

            # Auto-restart on consecutive failures
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                sys.stdout.write("\033[H\033[2J")  # 화면 클리어
                sys.stdout.flush()
                if is_session_alive():
                    tmux("kill-session", "-t", TMUX_SESSION)
                    time.sleep(0.5)
                setup_claude_session()
                _save_account_info()
                consecutive_failures = 0

            # Phase 2: Refresh in background thread
            result = [None]
            def _bg_query(rs=refresh_sec, force=force_refresh):
                result[0] = query_usage_shared(rs, force=force)
            thread = threading.Thread(target=_bg_query, daemon=True)
            thread.start()

            thread_start = time.time()
            while thread.is_alive():
                # Timeout: bail out if query takes too long
                if time.time() - thread_start > QUERY_TIMEOUT:
                    break
                redraw_now = False
                if select.select([sys.stdin], [], [], 0)[0]:
                    key = sys.stdin.read(1)
                    if key in ('w', 'W'):
                        refresh_sec = min(MAX_REFRESH, refresh_sec + REFRESH_STEP)
                    elif key in ('s', 'S'):
                        refresh_sec = max(MIN_REFRESH, refresh_sec - REFRESH_STEP)
                    elif key in ('d', 'D'):
                        bar_width = min(MAX_BAR_WIDTH, bar_width + BAR_WIDTH_STEP)
                        redraw_now = True
                    elif key in ('a', 'A'):
                        bar_width = max(MIN_BAR_WIDTH, bar_width - BAR_WIDTH_STEP)
                        redraw_now = True
                    elif key == '`':
                        if show_pace or show_profile or show_sonnet or show_refresh:
                            show_pace = show_profile = show_sonnet = show_refresh = False
                        else:
                            show_pace = show_profile = show_sonnet = show_refresh = True
                        redraw_now = True
                    elif key == '1':
                        show_pace = not show_pace
                        redraw_now = True
                    elif key == '2':
                        show_profile = not show_profile
                        redraw_now = True
                    elif key == '3':
                        show_sonnet = not show_sonnet
                        redraw_now = True
                    elif key == '4':
                        show_refresh = not show_refresh
                        redraw_now = True
                    elif key in ('e', 'E'):
                        show_horizontal = not show_horizontal
                        redraw_now = True
                    elif key in ('t', 'T'):
                        display_tmux_capture()
                        sys.stdin.read(1)
                        redraw_now = True
                    elif key in ('h', 'H', '\x1b'):
                        show_help = not show_help
                        redraw_now = True
                    if redraw_now:
                        if show_help:
                            display_help()
                        else:
                            display(data, bar_width, show_pace, show_profile, show_sonnet, show_horizontal)
                        redraw_now = False
                if show_refresh:
                    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
                    if data.error and data.error != "session_dead":
                        sys.stdout.write(
                            f"\r  \033[2m{spinner[tick % len(spinner)]}\033[0m \033[31mClaude server error\033[0m \033[2m· Retrying\033[0m\033[K"
                        )
                    elif data.timestamp:
                        sys.stdout.write(
                            f"\r  \033[2mRefresh: {refresh_sec}s · {spinner[tick % len(spinner)]} Refreshing · Last: {data.timestamp}\033[0m\033[K"
                        )
                    else:
                        sys.stdout.write(
                            f"\r  \033[2m{spinner[tick % len(spinner)]} Loading usage data\033[0m\033[K"
                        )
                    sys.stdout.flush()
                tick += 1
                time.sleep(0.1)

            # Update consecutive failure tracking
            qdata = result[0]
            if qdata is not None and qdata.parse_success:
                data = qdata
                consecutive_failures = 0
            elif qdata is not None and qdata.error == "session_dead":
                # 세션 죽음 감지 → 즉시 재시작
                sys.stdout.write("\033[H\033[2J")
                sys.stdout.flush()
                if is_session_alive():
                    tmux("kill-session", "-t", TMUX_SESSION)
                    time.sleep(0.5)
                setup_claude_session()
                _save_account_info()
                consecutive_failures = 0
            elif qdata is not None and qdata.error and "session_dead" not in qdata.error:
                # 서버 에러 (e.g. "Failed to load usage data") → 세션 재시작 무의미
                data = qdata
                consecutive_failures = 0
            else:
                if qdata is not None:
                    data = qdata
                consecutive_failures += 1
            redraw = True
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    main()
