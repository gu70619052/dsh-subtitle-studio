# -*- coding: utf-8 -*-
"""字幕工坊引擎 - 来源/输出端点（local 与 ssh 两种 kind）。

统一接口：probe / list_files / stat_size / exists / ensure_dir / fetch / put / df。
所有写操作先写 <name>.partial 再 rename（本地用 os.replace，远端用 mv），
失败时清理 partial，绝不污染目标目录。
"""

import os
import shutil
import socket
import subprocess
import time

from lib.runtime import (EngineError, ensure_dir, expand, file_size, human_size,
                     iso_from_epoch, shquote)

DEFAULT_SSH_OPTIONS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "LogLevel=ERROR",
]


def ssh_argv(ssh_conf):
    """构造 ssh 基础命令（不含远端命令）。"""
    ssh_conf = ssh_conf or {}
    host = (ssh_conf.get("host") or "").strip()
    user = (ssh_conf.get("user") or "").strip()
    target = ("%s@%s" % (user, host)) if user else host
    argv = ["ssh"]
    key_path = expand(ssh_conf.get("keyPath") or "")
    if key_path:
        argv += ["-i", key_path]
    port = ssh_conf.get("port") or 22
    argv += ["-p", str(port)]
    argv += list(DEFAULT_SSH_OPTIONS)
    extra = ssh_conf.get("options") or []
    if isinstance(extra, list):
        argv += [str(x) for x in extra]
    argv += [target]
    return argv


def tcp_probe(host, port, timeout=5.0):
    """TCP 端口可达性（不做协议握手），返回 (ok, latencyMs, error)。"""
    started = time.time()
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.close()
        return True, int((time.time() - started) * 1000), ""
    except (OSError, socket.error) as exc:
        return False, int((time.time() - started) * 1000), str(exc)


class Endpoint(object):
    """source / output 的统一端点。"""

    def __init__(self, kind, path, ssh_conf=None, logger=None, timeout=120):
        self.kind = kind if kind in ("local", "ssh") else "local"
        self.path = expand(path or "")
        self.ssh = ssh_conf or {}
        self.logger = logger
        self.timeout = timeout

    # ---------------------------------------------------------- 基础属性
    @property
    def is_local(self):
        return self.kind == "local"

    @property
    def host(self):
        return (self.ssh.get("host") or "").strip()

    @property
    def port(self):
        return int(self.ssh.get("port") or 22)

    def describe(self):
        if self.is_local:
            return "local:%s" % self.path
        return "ssh://%s@%s:%s%s" % (self.ssh.get("user") or "-", self.host, self.port, self.path)

    # ---------------------------------------------------------- 远端执行
    def _remote_cmd(self, command):
        return ssh_argv(self.ssh) + [command]

    def run_remote(self, command, timeout=None):
        """执行远端 shell 命令，返回 (rc, out, err)。"""
        if self.is_local:
            raise EngineError(4, "本地端点不支持 run_remote")
        try:
            proc = subprocess.run(self._remote_cmd(command),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            return 124, "", "ssh 超时（%ss）" % (timeout or self.timeout)
        except OSError as exc:
            raise EngineError(4, "ssh 无法启动：%s" % exc)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))

    def probe(self):
        """来源可达性探测。"""
        if self.is_local:
            if os.path.isdir(self.path):
                return {"ok": True, "detail": "目录存在", "latencyMs": 0}
            if os.path.exists(self.path):
                return {"ok": True, "detail": "文件存在（非目录）", "latencyMs": 0}
            return {"ok": False, "detail": "目录不存在：%s" % self.path, "latencyMs": 0}
        started = time.time()
        rc, out, err = self.run_remote("echo __ST_OK__", timeout=20)
        ms = int((time.time() - started) * 1000)
        if rc == 0 and "__ST_OK__" in out:
            return {"ok": True, "detail": "ssh 登录正常（%dB 回显）" % len(out.strip()), "latencyMs": ms}
        return {"ok": False, "detail": (err or out or "ssh 失败").strip().splitlines()[-1][:300],
                "latencyMs": ms}

    # ---------------------------------------------------------- 目录内容
    def list_files(self, include=None, recursive=False):
        """列出目录内匹配 include glob 的普通文件。

        返回 [{name, path, size, mtime, mtimeISO, ageDays}]（按 mtime 升序）。
        """
        include = include or ["*"]
        if self.is_local:
            return self._list_local(include, recursive)
        return self._list_ssh(include, recursive)

    def _list_local(self, include, recursive):
        import fnmatch
        results = []
        base = self.path
        if not os.path.isdir(base):
            return results
        walker = os.walk(base) if recursive else [(base, [], os.listdir(base))]
        for root, _dirs, files in walker:
            for name in files:
                if not any(fnmatch.fnmatch(name.lower(), pat.lower()) for pat in include):
                    continue
                full = os.path.join(root, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                results.append({
                    "name": name,
                    "path": full,
                    "size": int(st.st_size),
                    "mtime": float(st.st_mtime),
                    "mtimeISO": iso_from_epoch(st.st_mtime),
                    "ageDays": round(max(0.0, (time.time() - st.st_mtime) / 86400.0), 2),
                })
        results.sort(key=lambda r: (r["mtime"], r["name"]))
        return results

    def _list_ssh(self, include, recursive):
        # 远端目录还不存在（首次运行、自定义输出子目录）按“空目录”处理，不当成故障
        if not self.exists(self.path):
            return []
        name_args = " -o ".join("-name %s" % shquote(pat) for pat in include)
        depth = "" if recursive else "-maxdepth 1 "
        cmd = ("find %s %s-type f \\( %s \\) -printf '%%T@\\t%%s\\t%%p\\n' 2>/dev/null"
               % (shquote(self.path), depth, name_args))
        rc, out, err = self.run_remote(cmd, timeout=max(self.timeout, 120))
        if rc != 0 or not out.strip():
            # 回退：GNU stat / BSD stat（个别 busybox 无 -printf）
            alt = ("find %s %s-type f \\( %s \\) -exec stat -c '%%Y\\t%%s\\t%%n' {} + 2>/dev/null"
                   % (shquote(self.path), depth, name_args))
            rc2, out2, _ = self.run_remote(alt, timeout=max(self.timeout, 120))
            if rc2 == 0 and out2.strip():
                out = out2
            else:
                alt2 = ("find %s %s-type f \\( %s \\) -exec stat -f '%%m\\t%%z\\t%%N' {} + 2>/dev/null"
                        % (shquote(self.path), depth, name_args))
                rc3, out3, _ = self.run_remote(alt2, timeout=max(self.timeout, 120))
                if rc3 == 0 and out3.strip():
                    out = out3
                elif rc != 0:
                    raise EngineError(4, "列目录失败：%s" % (err or out2 or "find 不可用").strip()[:300])
        results = []
        for line in out.splitlines():
            parts = line.strip().split("\t")
            if len(parts) < 3:
                continue
            try:
                mtime = float(parts[0])
                size = int(parts[1])
            except ValueError:
                continue
            full = "\t".join(parts[2:])
            name = os.path.basename(full)
            results.append({
                "name": name,
                "path": full,
                "size": size,
                "mtime": mtime,
                "mtimeISO": iso_from_epoch(mtime),
                "ageDays": round(max(0.0, (time.time() - mtime) / 86400.0), 2),
            })
        results.sort(key=lambda r: (r["mtime"], r["name"]))
        return results

    def stat_size(self, path):
        if self.is_local:
            return file_size(path)
        rc, out, _ = self.run_remote("stat -c %%s %s 2>/dev/null || wc -c < %s"
                                     % (shquote(path), shquote(path)), timeout=60)
        if rc != 0:
            return None
        try:
            return int(out.strip().split()[0])
        except (ValueError, IndexError):
            return None

    def exists(self, path):
        if self.is_local:
            return os.path.exists(path)
        rc, out, _ = self.run_remote("test -e %s && echo YES || echo NO" % shquote(path), timeout=30)
        return "YES" in out

    def ensure_dir(self, path=None):
        target = path or self.path
        if self.is_local:
            ensure_dir(target)
            return True
        rc, out, err = self.run_remote("mkdir -p %s && echo OK" % shquote(target), timeout=60)
        if rc != 0 or "OK" not in out:
            raise EngineError(7, "无法创建远端目录：%s" % (err or out).strip()[:200])
        return True

    # ---------------------------------------------------------- 传输
    def fetch(self, remote_path, local_target):
        """下载到 local_target（先 .partial 校验后改名），返回本地路径。"""
        if self.is_local:
            src = os.path.abspath(remote_path)
            dst = os.path.abspath(local_target)
            if src == dst:
                return src
            ensure_dir(os.path.dirname(dst))
            partial = dst + ".partial"
            shutil.copyfile(src, partial)
            expected = file_size(src)
            got = file_size(partial)
            if expected is not None and got != expected:
                os.unlink(partial)
                raise EngineError(4, "本地复制不完整（%s != %s）" % (human_size(got), human_size(expected)))
            os.replace(partial, dst)
            return dst

        expected = self.stat_size(remote_path)
        ensure_dir(os.path.dirname(local_target))
        partial = local_target + ".partial"
        argv = self._remote_cmd("cat %s" % shquote(remote_path))
        try:
            with open(partial, "wb") as out:
                proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.PIPE)
                _out, err = proc.communicate(timeout=3600)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            if os.path.exists(partial):
                os.unlink(partial)
            raise EngineError(4, "下载超时（>1h）：%s" % remote_path)
        except OSError as exc:
            if os.path.exists(partial):
                os.unlink(partial)
            raise EngineError(4, "下载启动失败：%s" % exc)
        got = file_size(partial)
        if proc.returncode != 0:
            os.unlink(partial)
            raise EngineError(4, "下载失败 rc=%s：%s" % (proc.returncode,
                                                       err.decode("utf-8", "replace").strip()[:300]))
        if expected is not None and got != expected:
            os.unlink(partial)
            raise EngineError(4, "下载尺寸不符（远端 %s，本地 %s），已丢弃 partial"
                              % (human_size(expected), human_size(got)))
        os.replace(partial, local_target)
        return local_target

    def put(self, local_path, target_path=None, overwrite=False):
        """把本地文件放到端点（先 .partial 再 rename），返回结果字典。"""
        target_path = target_path or os.path.join(
            self.path, os.path.basename(local_path))
        size = file_size(local_path)
        if size is None:
            raise EngineError(7, "待上传文件不存在：%s" % local_path)

        if self.is_local:
            ensure_dir(os.path.dirname(target_path))
            if os.path.exists(target_path) and not overwrite:
                return {"uploaded": False, "reason": "exists", "path": target_path,
                        "size": size}
            partial = target_path + ".partial"
            shutil.copyfile(local_path, partial)
            if file_size(partial) != size:
                os.unlink(partial)
                raise EngineError(7, "本地写入不完整：%s" % target_path)
            os.replace(partial, target_path)
            return {"uploaded": True, "reason": "", "path": target_path, "size": size}

        self.ensure_dir(os.path.dirname(target_path))  # 远端目录必须走 SSH，不能本地 mkdir
        if self.exists(target_path) and not overwrite:
            return {"uploaded": False, "reason": "exists", "path": target_path, "size": size}
        partial = target_path + ".partial"
        argv = self._remote_cmd("cat > %s" % shquote(partial))
        try:
            with open(local_path, "rb") as src:
                proc = subprocess.Popen(argv, stdin=src, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                _out, err = proc.communicate(timeout=3600)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            self._cleanup_remote(partial)
            raise EngineError(7, "上传超时（>1h）：%s" % target_path)
        except OSError as exc:
            raise EngineError(7, "上传启动失败：%s" % exc)
        if proc.returncode != 0:
            self._cleanup_remote(partial)
            raise EngineError(7, "上传失败 rc=%s：%s" % (
                proc.returncode, err.decode("utf-8", "replace").strip()[:300]))
        remote_size = self.stat_size(partial)
        if remote_size != size:
            self._cleanup_remote(partial)
            raise EngineError(7, "上传尺寸不符（本地 %s，远端 %s），已清理 partial"
                              % (human_size(size), human_size(remote_size)))
        rc, _out, err = self.run_remote("mv -f %s %s && echo OK"
                                        % (shquote(partial), shquote(target_path)), timeout=120)
        if rc != 0:
            self._cleanup_remote(partial)
            raise EngineError(7, "远端改名失败：%s" % (err or "").strip()[:200])
        return {"uploaded": True, "reason": "", "path": target_path, "size": size}

    def _cleanup_remote(self, path):
        try:
            self.run_remote("rm -f %s" % shquote(path), timeout=60)
        except EngineError:
            pass

    def remove(self, path):
        if self.is_local:
            try:
                os.unlink(path)
            except OSError:
                pass
            return
        self._cleanup_remote(path)

    # ---------------------------------------------------------- 磁盘
    def df(self):
        """返回 {totalBytes, usedBytes, availBytes, usedPct} 或 None。"""
        if self.is_local:
            probe = self.path
            while probe and not os.path.exists(probe):
                parent = os.path.dirname(probe.rstrip("/"))
                if parent == probe:
                    break
                probe = parent
            try:
                usage = shutil.disk_usage(probe or "/")
            except OSError:
                return None
            pct = round(usage.used * 100.0 / usage.total, 1) if usage.total else 0
            return {"totalBytes": usage.total, "usedBytes": usage.used,
                    "availBytes": usage.free, "usedPct": pct}
        rc, out, _err = self.run_remote("df -Pk %s | tail -1" % shquote(self.path), timeout=60)
        if rc != 0:
            return None
        parts = out.split()
        if len(parts) < 4:
            return None
        try:
            total = int(parts[1]) * 1024
            used = int(parts[2]) * 1024
            avail = int(parts[3]) * 1024
        except ValueError:
            return None
        pct = round(used * 100.0 / total, 1) if total else 0
        return {"totalBytes": total, "usedBytes": used, "availBytes": avail, "usedPct": pct}
