# -*- coding: utf-8 -*-
"""字幕工坊引擎 - 共享运行时工具。

只依赖 Python 标准库；兼容 macOS 系统自带 python 3.9。
提供：错误类型、时间/大小格式化、原子写、按天滚动+5MB 自截断日志、
单实例锁（含 pid 存活检测）、子进程调用。
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

MAX_LOG_BYTES = 5 * 1024 * 1024
TAIL_KEEP_BYTES = 1024 * 1024
LOCK_STALE_SECONDS = 0  # 只按 pid 存活判断，不按时间


class EngineError(Exception):
    """带契约退出码的引擎错误（DESIGN §4）。"""

    def __init__(self, code, message, detail=None):
        Exception.__init__(self, message)
        self.code = int(code)
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------- 时间 / 大小

def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def today_str():
    return datetime.date.today().isoformat()


def iso_from_epoch(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def age_days(ts):
    try:
        return max(0.0, (time.time() - float(ts)) / 86400.0)
    except (TypeError, ValueError):
        return 0.0


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def human_duration(sec):
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return "?"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh%02dm%02ds" % (h, m, s)
    if m:
        return "%dm%02ds" % (m, s)
    return "%ds" % s


# ---------------------------------------------------------------- 路径 / 原子写

def expand(path):
    if not path:
        return path
    return os.path.expanduser(os.path.expandvars(str(path)))


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path


def file_size(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def atomic_write_text(path, text):
    """先写 <path>.partial 再 rename，保证读到的是完整文件。"""
    directory = os.path.dirname(os.path.abspath(path))
    ensure_dir(directory)
    tmp = path + ".partial"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def atomic_write_json(path, obj):
    return atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def tail_text(path, max_bytes=4096):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def df_avail_bytes(path):
    """本地磁盘可用字节；路径不存在时向上找第一个存在的父目录。"""
    path = expand(path)
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe.rstrip("/"))
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe or "/").free
    except OSError:
        return None


def sanitize_stem(name):
    """把文件名变成安全的目录名：保留中英日、数字、.-_，其余替换为 _。"""
    stem = os.path.splitext(os.path.basename(name))[0]
    safe = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", stem, flags=re.UNICODE).strip("._-")
    if not safe:
        safe = "item"
    return safe[:80]


def shquote(s):
    """POSIX shell 单引号转义（远端 find/cat 用）。"""
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


# ---------------------------------------------------------------- 日志

class Log(object):
    """按天滚动 + 5MB 自截断的日志器；--json 模式下进度只写 stderr/文件。"""

    def __init__(self, log_dir=None, name="run", echo=True):
        self.name = name
        self.log_dir = log_dir
        self.echo = echo
        self.path = None
        if log_dir:
            ensure_dir(log_dir)
            self.path = os.path.join(log_dir, name + ".log")

    # -- 内部：按天滚动
    def _roll_if_needed(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            mtime_day = datetime.date.fromtimestamp(os.stat(self.path).st_mtime).isoformat()
        except OSError:
            return
        if mtime_day == today_str():
            return
        rolled = os.path.join(self.log_dir, "%s-%s.log" % (self.name, mtime_day))
        try:
            if os.path.exists(rolled):
                os.unlink(rolled)
            os.replace(self.path, rolled)
        except OSError:
            pass

    def _truncate_if_needed(self):
        if not self.path:
            return
        try:
            if os.stat(self.path).st_size <= MAX_LOG_BYTES:
                return
            data = tail_text(self.path, TAIL_KEEP_BYTES)
            tmp = self.path + ".partial"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("... [日志超过 5MB，已自截断，保留末尾 %dKB] ...\n" % (TAIL_KEEP_BYTES // 1024))
                f.write(data)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _write(self, level, message):
        line = "%s [%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, message)
        if self.echo:
            try:
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
            except (IOError, OSError):
                pass
        if not self.path:
            return
        try:
            self._roll_if_needed()
            self._truncate_if_needed()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def stage(self, message):
        self._write("STAGE", message)

    def info(self, message):
        self._write("INFO", message)

    def warn(self, message):
        self._write("WARN", message)

    def error(self, message):
        self._write("ERROR", message)

    def debug(self, message):
        if os.environ.get("SUBTITLE_DEBUG"):
            self._write("DEBUG", message)


# ---------------------------------------------------------------- 子进程

def run_cmd(cmd, timeout=None, cwd=None, env=None, input_bytes=None):
    """执行命令，返回 (rc, stdout_text, stderr_text)。命令不存在 → EngineError(3)。"""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise EngineError(3, "找不到可执行文件：%s" % cmd[0],
                          detail="请在 config.json 的 burn.ffmpeg 指定绝对路径，或安装到 PATH")
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace")
        err = (exc.stderr or b"").decode("utf-8", "replace")
        return 124, out, err + "\n[timeout] 超过 %ss" % timeout
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


# ---------------------------------------------------------------- 单实例锁

def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class RunLock(object):
    """state/run.lock 单实例锁；pid 已死的锁自动回收。"""

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "run.lock")
        self.holder = None

    def read_holder(self):
        data = read_json(self.path, None)
        if isinstance(data, dict) and data.get("pid"):
            return data
        return None

    def acquire(self, meta=None):
        ensure_dir(self.state_dir)
        meta = dict(meta or {})
        meta.setdefault("pid", os.getpid())
        meta.setdefault("startedAt", now_iso())
        meta.setdefault("host", os.uname().nodename)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                holder = self.read_holder()
                if holder and pid_alive(holder.get("pid")):
                    self.holder = holder
                    return False
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
                continue
            except OSError as exc:
                raise EngineError(9, "无法创建运行锁：%s" % exc, detail=self.path)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
                f.write("\n")
            self.holder = meta
            return True
        holder = self.read_holder()
        self.holder = holder
        return False

    def release(self):
        try:
            holder = self.read_holder()
            if holder and int(holder.get("pid") or 0) == os.getpid():
                os.unlink(self.path)
        except (OSError, TypeError, ValueError):
            pass


# ---------------------------------------------------------------- 其它

def cjk_ratio(text):
    """假名占比（用于判断哪些字幕仍是日文原文）。"""
    if not text:
        return 0.0
    kana = len(re.findall(r"[\u3040-\u30ff]", text))
    return kana / max(1, len(text.replace(" ", "").replace("\n", "")))
