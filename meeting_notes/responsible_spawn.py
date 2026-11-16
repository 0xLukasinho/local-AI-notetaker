"""Start the macOS audio helper as its own TCC responsible process.

Screen & System Audio Recording follows the responsible process. A helper
spawned by Python under launchd inherits Python (or whoever launched
Python), so the Privacy pane never lists Meeting Audio Capture and the
helper's window titles stay blank.

`responsibility_spawnattrs_setdisclaim` (macOS 10.14+, still the mechanism
on macOS 15 and 26) marks the child as responsible for itself. The binary
lives inside MeetingAudioCapture.app, so the prompt and the Privacy list
use that app. `open -a` would also disclaim the parent, but it cannot hand
the recorder the stdin pipe it uses to stop. Pipes stay intact here.

Off macOS, or if the disclaim call fails, this is a normal subprocess so
recording still starts.
"""

import os
import subprocess
import sys
import time


def disclaim_supported(platform=None):
    return (platform or sys.platform) == "darwin"


class ResponsibleProcess:
    """The slice of subprocess.Popen the recorder and the probe use."""

    def __init__(self, pid, stdin=None, stdout=None, stderr=None, argv=None):
        self.pid = int(pid)
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.argv = argv
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        try:
            waited, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.returncode = 0
            return self.returncode
        if waited == 0:
            return None
        self.returncode = _exit_code(status)
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        if timeout is None:
            _waited, status = os.waitpid(self.pid, 0)
            self.returncode = _exit_code(status)
            return self.returncode
        deadline = time.time() + float(timeout)
        while True:
            result = self.poll()
            if result is not None:
                return result
            if time.time() >= deadline:
                raise subprocess.TimeoutExpired(self.argv, timeout)
            time.sleep(0.05)

    def communicate(self, timeout=None):
        if self.stdin is not None:
            try:
                self.stdin.close()
            except OSError:
                pass
        self.wait(timeout=timeout)
        out = _read_all(self.stdout)
        err = _read_all(self.stderr)
        return out, err


def _read_all(handle):
    if handle is None:
        return b""
    try:
        return handle.read()
    except Exception:
        return b""


def _exit_code(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return status


def popen_responsible(argv, stdin=None, stdout=None, stderr=None, spawn=None):
    """Spawn `argv`. On macOS the child is its own TCC responsible process.

    `spawn` replaces the disclaim call in tests. It receives the argv and
    the three parent-side file objects and returns a process-like object.
    """
    if spawn is not None:
        return spawn(argv, stdin=stdin, stdout=stdout, stderr=stderr)
    if not disclaim_supported():
        return subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=stderr)
    try:
        return _popen_disclaimed(argv, stdin=stdin, stdout=stdout, stderr=stderr)
    except Exception:
        return subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=stderr)


def _pipe_or(spec, for_write):
    """Return (child_fd, parent_file, close_in_parent)."""
    if spec is subprocess.DEVNULL:
        fd = os.open(os.devnull, os.O_RDWR)
        return fd, None, True
    if spec is subprocess.PIPE or spec is None and for_write:
        read_fd, write_fd = os.pipe()
        if for_write:
            parent = os.fdopen(write_fd, "wb", buffering=0)
            return read_fd, parent, False
        parent = os.fdopen(read_fd, "rb", buffering=0)
        return write_fd, parent, False
    if spec is None:
        return None, None, False
    return None, None, False


def _popen_disclaimed(argv, stdin=None, stdout=None, stderr=None):
    import ctypes

    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.posix_spawnattr_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    libc.posix_spawnattr_init.restype = ctypes.c_int
    libc.posix_spawnattr_setflags.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_short]
    libc.posix_spawnattr_setflags.restype = ctypes.c_int
    libc.posix_spawnattr_destroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    libc.posix_spawnattr_destroy.restype = ctypes.c_int
    libc.posix_spawn_file_actions_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    libc.posix_spawn_file_actions_init.restype = ctypes.c_int
    libc.posix_spawn_file_actions_adddup2.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_int,
        ctypes.c_int,
    ]
    libc.posix_spawn_file_actions_adddup2.restype = ctypes.c_int
    libc.posix_spawn_file_actions_destroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    libc.posix_spawn_file_actions_destroy.restype = ctypes.c_int
    libc.responsibility_spawnattrs_setdisclaim.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
    ]
    libc.responsibility_spawnattrs_setdisclaim.restype = ctypes.c_int
    libc.posix_spawn.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.POINTER(ctypes.c_char_p),
    ]
    libc.posix_spawn.restype = ctypes.c_int

    child_fds = []
    parent_files = []
    mapping = []
    specs = (
        (stdin if stdin is not None else subprocess.DEVNULL, 0, True),
        (stdout if stdout is not None else subprocess.DEVNULL, 1, False),
        (stderr if stderr is not None else subprocess.DEVNULL, 2, False),
    )
    try:
        for spec, target, for_write in specs:
            child_fd, parent, close_child_is_devnull = _pipe_or(spec, for_write)
            if child_fd is None:
                continue
            child_fds.append(child_fd)
            mapping.append((child_fd, target))
            if parent is not None:
                parent_files.append((target, parent))
            elif close_child_is_devnull:
                pass
        attr = ctypes.c_void_p()
        actions = ctypes.c_void_p()
        if libc.posix_spawnattr_init(ctypes.byref(attr)) != 0:
            raise OSError(ctypes.get_errno(), "posix_spawnattr_init")
        try:
            # 0x0002 POSIX_SPAWN_SETPGROUP so the helper is not in Python's group.
            libc.posix_spawnattr_setflags(ctypes.byref(attr), 0x0002)
            disclaim = libc.responsibility_spawnattrs_setdisclaim(ctypes.byref(attr), 1)
            if disclaim != 0:
                raise OSError(disclaim, "responsibility_spawnattrs_setdisclaim")
            if libc.posix_spawn_file_actions_init(ctypes.byref(actions)) != 0:
                raise OSError(ctypes.get_errno(), "posix_spawn_file_actions_init")
            try:
                for child_fd, target in mapping:
                    if libc.posix_spawn_file_actions_adddup2(
                        ctypes.byref(actions), child_fd, target
                    ) != 0:
                        raise OSError(ctypes.get_errno(), "posix_spawn_file_actions_adddup2")
                encoded = [os.fsencode(part) for part in argv]
                c_argv = (ctypes.c_char_p * (len(encoded) + 1))()
                for index, part in enumerate(encoded):
                    c_argv[index] = part
                env_encoded = [
                    os.fsencode("%s=%s" % (key, value)) for key, value in os.environ.items()
                ]
                c_env = (ctypes.c_char_p * (len(env_encoded) + 1))()
                for index, part in enumerate(env_encoded):
                    c_env[index] = part
                pid = ctypes.c_int()
                rc = libc.posix_spawn(
                    ctypes.byref(pid),
                    os.fsencode(argv[0]),
                    ctypes.byref(actions),
                    ctypes.byref(attr),
                    c_argv,
                    c_env,
                )
                if rc != 0:
                    raise OSError(rc, "posix_spawn")
            finally:
                libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
        finally:
            libc.posix_spawnattr_destroy(ctypes.byref(attr))
    finally:
        for fd in child_fds:
            try:
                os.close(fd)
            except OSError:
                pass
    files = {target: handle for target, handle in parent_files}
    return ResponsibleProcess(
        pid.value,
        stdin=files.get(0),
        stdout=files.get(1),
        stderr=files.get(2),
        argv=argv,
    )


def run_responsible(argv, timeout=8, spawn=None):
    """Capture stdout from a short helper command. Returns (returncode, stdout text)."""
    process = popen_responsible(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        spawn=spawn,
    )
    try:
        out, _err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, ""
    text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
    return process.returncode, text
