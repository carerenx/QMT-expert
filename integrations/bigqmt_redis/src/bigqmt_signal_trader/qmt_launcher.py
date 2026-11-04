# coding: utf-8
"""Start and stop a Big QMT terminal (issue #45).

Big QMT has to be restarted most mornings, and the login dialog is the reason
it cannot simply be dropped into a scheduler. Two ways past it:

* **Passwordless (preferred)** -- ``XtMiniQmt.exe linkMini`` starts MiniQMT
  against an existing session with no dialog at all. This is what
  ``免密登录qmt.bat`` does. No UI automation, so nothing here depends on a
  desktop being visible.
* **Credential entry** -- for the full terminal (``XtItClient.exe``) the dialog
  is unavoidable. We drive it with PHYSICAL input (``keybd_event`` /
  ``mouse_event`` over ctypes), not ``SendMessage``: message-based typing never
  reaches the focused control of a Qt dialog when another window holds the
  foreground, so it silently no-ops. Physical input requires the dialog on top,
  which we enforce (Alt-unlock + topmost + foreground) and verify before
  typing anything. Needs an unlocked interactive session -- a locked desktop
  or RDP logout is out of scope.

Everything is scoped to one install directory. A machine here runs several QMT
copies side by side, so an unscoped ``taskkill /im XtItClient.exe`` would take
down someone else's trading session.

Waits are on observed readiness, never a fixed sleep: startup is complete when
the FormulaServer port accepts a connection, which is also exactly what the
rest of this package needs before it can do anything.

Windows only. ``psutil`` is used when importable, otherwise we shell out to
``wmic``/``taskkill``.
"""

import os
import socket
import subprocess
import sys
import time

from .logging_setup import get_logger


log = get_logger("launcher")

# Processes a QMT install owns. miniquote/BrokerProxy/minibroker are children
# that survive the main window and hold the ports we need to rebind.
QMT_PROCESS_NAMES = (
    "XtItClient.exe",
    "XtMiniQmt.exe",
    "miniquote.exe",
    "BrokerProxy.exe",
    "minibroker.exe",
)

# FormulaServer. Listening means the terminal is far enough up to answer.
DEFAULT_READY_PORT = 58600

__all__ = [
    "QmtLauncherError",
    "close_qmt",
    "find_qmt_processes",
    "is_qmt_running",
    "open_qmt",
    "restart_qmt",
    "wait_until_ready",
]


class QmtLauncherError(RuntimeError):
    """Launching or stopping a QMT terminal failed."""


def _normalize_dir(path):
    if not path:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def resolve_install_dir(install_dir):
    """Accept an install root, its bin.x64, or a path to an exe inside it.

    Returns the normalized ``bin.x64`` directory, which is what process paths
    are compared against.
    """
    path = str(install_dir or "").strip().strip('"').strip("'")
    if not path:
        raise QmtLauncherError("install_dir is required (QMT root, bin.x64, or an exe path)")
    if os.path.isfile(path) or path.lower().endswith(".exe"):
        path = os.path.dirname(path)
    normalized = os.path.normpath(os.path.abspath(path))
    if os.path.basename(normalized).lower() != "bin.x64":
        candidate = os.path.join(normalized, "bin.x64")
        if os.path.isdir(candidate):
            normalized = candidate
    return normalized


# ---------------------------------------------------------------- discovery
def _iter_processes_psutil():
    import psutil

    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = proc.info
            yield int(info["pid"]), str(info.get("name") or ""), str(info.get("exe") or "")
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue


def _iter_processes_wmic():
    """psutil-free fallback. wmic still ships on the Windows builds QMT runs on."""
    try:
        raw = subprocess.check_output(
            ["wmic", "process", "get", "ProcessId,Name,ExecutablePath", "/format:csv"],
            stderr=subprocess.STDOUT,
        )
    except Exception as exc:
        raise QmtLauncherError(
            "cannot enumerate processes: psutil is not installed and wmic failed (%s)" % exc
        )
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    for row in text.splitlines():
        parts = [p.strip() for p in row.split(",")]
        # CSV columns: Node,ExecutablePath,Name,ProcessId
        if len(parts) < 4 or parts[3].lower() in ("processid", ""):
            continue
        try:
            pid = int(parts[3])
        except ValueError:
            continue
        yield pid, parts[2], parts[1]


def _iter_processes():
    try:
        import psutil  # noqa: F401
    except ImportError:
        return _iter_processes_wmic()
    return _iter_processes_psutil()


def find_qmt_processes(install_dir, names=QMT_PROCESS_NAMES):
    """Return ``[(pid, name, exe), ...]`` for QMT processes under ``install_dir``.

    A process with no readable exe path is skipped rather than guessed at: on a
    machine running several QMT copies, killing by name alone is how you take
    down the wrong account.
    """
    target = _normalize_dir(resolve_install_dir(install_dir))
    wanted = set(str(n).lower() for n in names)
    found = []
    for pid, name, exe in _iter_processes():
        if name.lower() not in wanted or not exe:
            continue
        if _normalize_dir(os.path.dirname(exe)) == target:
            found.append((pid, name, exe))
    return found


def is_qmt_running(install_dir):
    return bool(find_qmt_processes(install_dir))


# ------------------------------------------------------------------ readiness
def port_is_listening(port=DEFAULT_READY_PORT, host="127.0.0.1", timeout=1.0):
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def wait_until_ready(port=DEFAULT_READY_PORT, host="127.0.0.1", timeout_seconds=180.0,
                     poll_interval=2.0):
    """Block until ``port`` accepts a connection. Returns seconds waited.

    Raises :class:`QmtLauncherError` on timeout rather than returning False, so
    a scheduled restart fails loudly instead of letting the next step run
    against a terminal that never came up.
    """
    deadline = time.time() + float(timeout_seconds)
    started = time.time()
    while time.time() < deadline:
        if port_is_listening(port, host):
            waited = time.time() - started
            log.info("qmt ready after %.1fs (%s:%d listening)", waited, host, port)
            return waited
        time.sleep(poll_interval)
    raise QmtLauncherError(
        "QMT did not become ready within %.0fs (%s:%d never listened)"
        % (timeout_seconds, host, port)
    )


def wait_until_stopped(install_dir, timeout_seconds=60.0, poll_interval=1.0):
    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        if not find_qmt_processes(install_dir):
            return True
        time.sleep(poll_interval)
    return False


# --------------------------------------------------------------------- close
def _terminate(pid, force=False):
    try:
        import psutil

        proc = psutil.Process(pid)
        if force:
            proc.kill()
        else:
            proc.terminate()
        return True
    except ImportError:
        pass
    except Exception:
        return False
    cmd = ["taskkill", "/pid", str(pid)]
    if force:
        cmd.append("/f")
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return True
    except Exception:
        return False


def close_qmt(install_dir, timeout_seconds=60.0, force_after_seconds=20.0):
    """Stop every QMT process under ``install_dir``. Returns how many were stopped.

    Asks politely first: the terminal flushes local data on a clean exit, and
    killing it outright is how the K-line store ends up truncated. Escalates to
    a hard kill only after ``force_after_seconds``.
    """
    targets = find_qmt_processes(install_dir)
    if not targets:
        log.info("no QMT process under %s; nothing to close", install_dir)
        return 0

    for pid, name, _exe in targets:
        log.info("closing %s (pid=%s)", name, pid)
        _terminate(pid, force=False)

    if wait_until_stopped(install_dir, timeout_seconds=force_after_seconds):
        log.info("closed %d process(es) cleanly", len(targets))
        return len(targets)

    remaining = find_qmt_processes(install_dir)
    log.warning("%d process(es) still alive after %.0fs; forcing",
                len(remaining), force_after_seconds)
    for pid, name, _exe in remaining:
        _terminate(pid, force=True)

    grace = max(timeout_seconds - force_after_seconds, 5.0)
    if not wait_until_stopped(install_dir, timeout_seconds=grace):
        still = find_qmt_processes(install_dir)
        raise QmtLauncherError(
            "could not stop: %s" % ", ".join("%s(pid=%s)" % (n, p) for p, n, _ in still)
        )
    return len(targets)


# ---------------------------------------------------------------------- open
def _spawn(command, cwd=None, shell=False):
    log.info("launching: %s", command if isinstance(command, str) else " ".join(command))
    kwargs = {"cwd": cwd, "shell": shell,
              "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        # Detach so the terminal outlives this process -- otherwise a scheduled
        # task exiting takes QMT with it.
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        kwargs["creationflags"] = detached | new_group
    return subprocess.Popen(command, **kwargs)


def open_qmt(install_dir, mode="auto", bat_path=None, exe_name=None,
             ready_port=DEFAULT_READY_PORT, ready_timeout_seconds=180.0,
             wait_ready=True, credentials=None, window_title_prefix=None):
    """Start a QMT terminal under ``install_dir`` and wait until it answers.

    ``mode``:
      ``"linkmini"`` -- ``XtMiniQmt.exe linkMini``, no login dialog.
      ``"bat"``      -- run ``bat_path`` (e.g. 免密登录qmt.bat).
      ``"exe"``      -- start ``exe_name`` (default XtItClient.exe) as-is; use
                        when the terminal restores its own session.
      ``"login"``    -- start the exe, then type credentials into the dialog.
      ``"auto"``     -- bat if given, else linkmini if XtMiniQmt.exe exists,
                        else exe.

    ``credentials`` (mode="login") is ``{"user": ..., "password": ...}``. Pass
    it from your local config or environment; never hardcode it, and note the
    values are typed into a window, so anything that can read that window can
    read them.
    """
    bin_dir = resolve_install_dir(install_dir)
    if not os.path.isdir(bin_dir):
        raise QmtLauncherError("no such directory: %s" % bin_dir)

    mode = str(mode or "auto").lower()
    if mode == "auto":
        if bat_path:
            mode = "bat"
        elif os.path.isfile(os.path.join(bin_dir, "XtMiniQmt.exe")):
            mode = "linkmini"
        else:
            mode = "exe"

    if mode == "bat":
        if not bat_path or not os.path.isfile(bat_path):
            raise QmtLauncherError("bat_path is required for mode='bat': %r" % bat_path)
        _spawn([bat_path], cwd=os.path.dirname(bat_path), shell=True)
    elif mode == "linkmini":
        exe = os.path.join(bin_dir, "XtMiniQmt.exe")
        if not os.path.isfile(exe):
            raise QmtLauncherError("XtMiniQmt.exe not found in %s" % bin_dir)
        _spawn([exe, "linkMini"], cwd=bin_dir)
    elif mode in ("exe", "login"):
        exe = os.path.join(bin_dir, str(exe_name or "XtItClient.exe"))
        if not os.path.isfile(exe):
            raise QmtLauncherError("%s not found in %s" % (os.path.basename(exe), bin_dir))
        _spawn([exe], cwd=bin_dir)
        if mode == "login":
            _login_via_window(credentials or {}, window_title_prefix)
    else:
        raise QmtLauncherError("unknown mode %r (bat/linkmini/exe/login/auto)" % mode)

    if not wait_ready:
        return 0.0
    return wait_until_ready(ready_port, timeout_seconds=ready_timeout_seconds)


def _login_via_window(credentials, window_title_prefix=None, appear_timeout_seconds=90.0):
    """Type credentials into the QMT login dialog.

    Matches the window by title PREFIX. The reference implementation pinned the
    full title including a build number ("国金证券QMT交易端 1.0.0.29456"), which
    stops finding the window on the next terminal update.

    Input is delivered as PHYSICAL input (keybd_event/mouse_event), not
    SendMessage: a background process's SendMessage does not reach the focused
    control of a Qt dialog, which is why purely message-based typing silently
    no-ops when anything else holds the foreground. Physical input needs the
    dialog to be actually on top, so we first unlock the foreground lock (the
    Alt-key trick), raise the window, and refuse to type if another window is
    still covering it.

    Verified live against 国金 QMT 2.1.19 (2026-08-20). Dialog field offsets
    are fractions of the window size, so they survive the dialog being moved
    or the broker build changing its absolute placement.
    """
    user = str(credentials.get("user") or credentials.get("account") or "")
    password = str(credentials.get("password") or "")
    if not user or not password:
        raise QmtLauncherError(
            "mode='login' needs credentials={'user':..., 'password':...}"
        )
    try:
        import win32con
        import win32gui
    except ImportError:
        raise QmtLauncherError(
            "mode='login' needs pywin32 (pip install pywin32)"
        )
    import ctypes

    user32 = ctypes.windll.user32
    prefix = str(window_title_prefix or "QMT")

    def _collect(hwnd, acc):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        cls = win32gui.GetClassName(hwnd)
        # Qt5QWindowIcon 限定：避免把标题前缀相近的资源管理器等窗口当成登录框。
        if cls == "Qt5QWindowIcon" and title.strip().startswith(prefix):
            acc.append(hwnd)

    def _find():
        matches = []
        win32gui.EnumWindows(_collect, matches)
        return matches[0] if matches else None

    deadline = time.time() + appear_timeout_seconds
    handle = None
    while time.time() < deadline:
        candidate = _find()
        if candidate:
            r = win32gui.GetWindowRect(candidate)
            if (r[2] - r[0]) < 800 and (r[3] - r[1]) < 600:
                handle = candidate
                break
            # 大窗 = 主界面已出现：终端自动登录了，无需输入凭据，直接跳过。
            log.info("main window already up (auto-login); skipping credentials")
            return
        time.sleep(2.0)
    if not handle:
        raise QmtLauncherError(
            "login window starting with %r did not appear within %.0fs"
            % (prefix, appear_timeout_seconds)
        )

    # 解锁前台保护并置顶：不验证可见性就打字，密码会落进遮挡窗口（实盘踩过坑）。
    user32.keybd_event(0x12, 0, 0, 0)   # Alt down — unlocks SetForegroundWindow
    user32.keybd_event(0x12, 0, 2, 0)   # Alt up
    time.sleep(0.2)
    win32gui.SetWindowPos(handle, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
    win32gui.ShowWindow(handle, win32con.SW_RESTORE)
    user32.SetForegroundWindow(handle)
    time.sleep(0.8)
    if user32.GetForegroundWindow() != handle:
        raise QmtLauncherError(
            "could not foreground the login dialog; another window would receive "
            "the password. Close covering windows and retry."
        )

    def _looks_like_login_dialog():
        # 登录框是小窗（国金 2.1.19 为 ~624x443）；主界面是大窗/最大化。
        # 自动登录完成时找到的会是主界面——打字会落进主窗口控件，必须跳过。
        r = win32gui.GetWindowRect(handle)
        return (r[2] - r[0]) < 800 and (r[3] - r[1]) < 600

    if not _looks_like_login_dialog():
        log.info("window is already the main interface (auto-login); skipping credentials")
        return

    # 国金 2.1.19 登录框（624x443）控件的相对位置，按比例适配尺寸变化。
    rect = win32gui.GetWindowRect(handle)
    wx, wy = rect[0], rect[1]
    w = max(rect[2] - rect[0], 1)
    h = max(rect[3] - rect[1], 1)
    account_xy = (0.68 * w, 0.57 * h)
    password_xy = (0.48 * w, 0.63 * h)  # 避开右侧虚拟键盘图标
    login_xy = (0.40 * w, 0.79 * h)

    def _click(fx, fy):
        user32.SetCursorPos(wx + int(fx), wy + int(fy))
        time.sleep(0.15)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
        time.sleep(0.3)

    def _key(vk):
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.06)

    def _type(text):
        for ch in str(text):
            _key(ord(ch))

    def _select_all():
        # 账号可能已预填：直接打字会变成追加。先 Ctrl+A 全选再覆盖。
        user32.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        user32.keybd_event(ord("A"), 0, 0, 0)
        user32.keybd_event(ord("A"), 0, 2, 0)
        user32.keybd_event(win32con.VK_CONTROL, 0, 2, 0)
        time.sleep(0.2)

    # Never log the values themselves.
    log.info("entering credentials into window %r (physical input)", prefix)
    _click(*account_xy)
    _select_all()
    _type(user)
    _click(*password_xy)
    _type(password)
    if not _looks_like_login_dialog():
        # 自动登录在打字过程中已完成——对话框已关闭，别再点"登录"坐标。
        log.info("login dialog gone mid-entry (auto-login completed); skipping submit click")
        return
    _click(*login_xy)


def restart_qmt(install_dir, settle_seconds=5.0, **open_kwargs):
    """Close, wait for the ports to be released, then start again.

    ``settle_seconds`` matters: the FormulaServer and RPC sockets linger briefly
    after the process dies, and the ZMQ transport binds its configured port
    exactly (no scanning), so restarting too eagerly fails the rebind.
    """
    closed = close_qmt(install_dir)
    if closed:
        time.sleep(settle_seconds)
    waited = open_qmt(install_dir, **open_kwargs)
    log.info("restart complete (closed=%d, ready in %.1fs)", closed, waited)
    return waited


# ----------------------------------------------------------------------- CLI
def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m bigqmt_signal_trader.qmt_launcher",
        description="Start/stop a Big QMT terminal, scoped to one install directory.",
    )
    parser.add_argument("action", choices=("open", "close", "restart", "status"))
    parser.add_argument("--dir", required=True,
                        help="QMT root, its bin.x64, or a path to an exe inside it")
    parser.add_argument("--mode", default="auto",
                        choices=("auto", "bat", "linkmini", "exe", "login"))
    parser.add_argument("--bat", default=None, help="batch file for --mode bat")
    parser.add_argument("--exe", default=None, help="exe name for --mode exe/login")
    parser.add_argument("--port", type=int, default=DEFAULT_READY_PORT)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--title-prefix", default=None,
                        help="login window title prefix (--mode login)")
    args = parser.parse_args(argv)

    if args.action == "status":
        procs = find_qmt_processes(args.dir)
        if not procs:
            print("not running (%s)" % resolve_install_dir(args.dir))
            return 1
        for pid, name, exe in procs:
            print("%-16s pid=%-8s %s" % (name, pid, exe))
        print("ready port %d: %s" % (
            args.port, "listening" if port_is_listening(args.port) else "not listening"))
        return 0

    credentials = None
    if args.mode == "login":
        # Read from the environment so a password never reaches argv, where it
        # would be visible to any process listing.
        credentials = {"user": os.environ.get("BIGQMT_LOGIN_USER", ""),
                       "password": os.environ.get("BIGQMT_LOGIN_PASSWORD", "")}

    try:
        if args.action == "close":
            print("closed %d process(es)" % close_qmt(args.dir))
        else:
            kwargs = dict(mode=args.mode, bat_path=args.bat, exe_name=args.exe,
                          ready_port=args.port, ready_timeout_seconds=args.timeout,
                          wait_ready=not args.no_wait, credentials=credentials,
                          window_title_prefix=args.title_prefix)
            if args.action == "restart":
                restart_qmt(args.dir, **kwargs)
            else:
                open_qmt(args.dir, **kwargs)
            print("ok")
    except QmtLauncherError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
