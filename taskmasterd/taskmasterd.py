import enum
import json
import logging
import os
import shlex
import signal
import socketserver
import sys
import threading
import time
import subprocess
import argparse

from config import ConfigError, load_config

SOCK_PATH = "/tmp/taskmaster.sock"
TICK = 0.1  # seconds between two updates of the jobs

log = logging.getLogger("taskmasterd")


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:  # one request per line, until disconnection
            try:
                req = json.loads(line)
                resp = self.server.supervisor.execute(req["cmd"], req.get("args", []))
            except Exception as e:
                log.exception("request %r failed", line)
                resp = {"ok": False, "output": f"error: {e}"}
            try:
                self.wfile.write((json.dumps(resp) + "\n").encode())
            except OSError:  # the client left during a long command (stop, restart...)
                return


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True  # client threads are daemonic, so they won't block the server from exiting


def start_server(supervisor):
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)  # delete the socket file if it already exists
    srv = Server(SOCK_PATH, Handler)
    os.chmod(SOCK_PATH, 0o600)  # only my user can connect
    srv.supervisor = supervisor
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run_as(user):
    if user is None or user.pw_uid == os.geteuid():
        return {}, {}
    if os.geteuid() != 0:
        raise PermissionError(f"only root can run a program as {user.pw_name}")
    ids = {
        "user": user.pw_uid,
        "group": user.pw_gid,
        "extra_groups": os.getgrouplist(user.pw_name, user.pw_gid),
    }
    # set env vars to prevent it being those of the root user
    return ids, {"HOME": user.pw_dir, "USER": user.pw_name, "LOGNAME": user.pw_name}


def open_output(path, nofollow):
    """Open a stdout/stderr file for appending, like open(path, "ab").

    O_NONBLOCK: a FIFO without reader fails instead of blocking the whole daemon.
    O_NOFOLLOW, prevent security issue where a user can redirect output to a symlink.
    """
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK
    if nofollow:
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o666)
    os.set_blocking(fd, True)  # the child must get a normal, blocking stdout
    return open(fd, "ab")


def spawn(program):
    """Launch one process of `program` (a config dict) and return its Popen."""
    ids, user_env = run_as(program["user"])
    outputs = []
    try:
        for path in (program["stdout"], program["stderr"]):
            outputs.append(subprocess.DEVNULL if path is None else open_output(path, nofollow=bool(ids)))
        return subprocess.Popen(
            shlex.split(program["cmd"]),
            cwd=program["workingdir"],  # entered as root, before the user switch (Popen's order)
            env={**os.environ, **user_env, **program["env"]},
            umask=-1 if program["umask"] is None else program["umask"],
            stdin=subprocess.DEVNULL,
            stdout=outputs[0],
            stderr=outputs[1],
            start_new_session=True,  # Ctrl+C in taskmasterd's terminal must not reach the children
            **ids,
        )
    finally:
        for f in outputs:  # the child has its own copies of these descriptors
            if f is not subprocess.DEVNULL:
                f.close()


def signal_group(proc, sig):
    """Send `sig` to `proc` and to everything it spawned.

    start_new_session makes each child the leader of its own process group (pgid == pid).
    Signalling the group avoids leaving orphans, e.g. the `sleep` of `sh -c 'sleep 60; echo done'`.
    """
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass  # the whole group is already gone


def describe_exit(code):
    if code >= 0:
        return f"exit status {code}"
    try:
        return f"killed by {signal.Signals(-code).name}"
    except ValueError:  # e.g. a real-time signal
        return f"killed by signal {-code}"


class State(enum.Enum):
    STOPPED = enum.auto()   # not started yet, or stopped by a command
    STARTING = enum.auto()  # alive for less than starttime
    RUNNING = enum.auto()   # alive for at least starttime: successfully started
    STOPPING = enum.auto()  # stopsignal sent, SIGKILL once stoptime is over
    EXITED = enum.auto()    # exited by itself after a successful start
    FATAL = enum.auto()     # couldn't start, even after startretries retries


class Job:
    """One process of a program: numprocs: 3 gives the jobs name:0, name:1 and name:2.

    Its methods are called with the Supervisor's lock held.
    """

    def __init__(self, program_name, index, program):
        self.program_name = program_name
        self.index = index
        self.name = f"{program_name}:{index}"
        self.program = program  # config dict, shared with the other jobs of the program
        self.state = State.STOPPED
        self.info = "not started"  # why it's in this state, shown by status
        self.proc = None  # Popen of the current or last process
        self.started_at = 0
        self.kill_at = None  # when to send SIGKILL, while STOPPING
        self.retries = 0
        self.retired = False  # being removed by a reload: never start it again

    def alive(self):
        return self.proc is not None and self.proc.poll() is None  # poll() also reaps a dead child

    def set_state(self, state, info, level=logging.INFO):
        log.log(level, "%s: %s -> %s (%s)", self.name, self.state.name, state.name, info)
        self.state, self.info = state, info

    def start(self):
        if not self.alive():
            self.retries = 0
            self.spawn()

    def spawn(self):
        try:
            self.proc = spawn(self.program)
            info = f"pid {self.proc.pid}"
        except Exception as e:  # not only OSError: e.g. PermissionError from run_as()
            self.proc = None
            info = f"spawn error: {e}"
        self.started_at = time.monotonic()
        self.set_state(State.STARTING, info)

    def stop(self):
        if self.state not in (State.STARTING, State.RUNNING):
            return
        if not self.alive():  # already dead, or a spawn error waiting for its retry
            return self.set_state(State.STOPPED, self.info if self.proc is None else describe_exit(self.proc.returncode))
        signal_group(self.proc, self.program["stopsignal"])
        self.kill_at = time.monotonic() + self.program["stoptime"]
        self.set_state(State.STOPPING, f"sent {self.program['stopsignal'].name} to pid {self.proc.pid}")

    def update(self):
        """Move to the next state, from the process and the clock."""
        now = time.monotonic()
        if self.state is State.STOPPING:
            if not self.alive():
                self.set_state(State.STOPPED, describe_exit(self.proc.returncode))
            elif self.kill_at is not None and now >= self.kill_at:
                log.warning("%s: still alive after stoptime (%ss), sending SIGKILL", self.name, self.program["stoptime"])
                signal_group(self.proc, signal.SIGKILL)
                self.kill_at = None
        elif self.state in (State.STARTING, State.RUNNING):
            if not self.alive():
                self.exited(now)
            elif self.state is State.STARTING and now - self.started_at >= self.program["starttime"]:
                self.set_state(State.RUNNING, f"pid {self.proc.pid}")

    def exited(self, now):
        # judged on the time it lived, so that starttime: 0 and deaths just after starttime count as started
        if self.proc is None or (self.state is State.STARTING and now - self.started_at < self.program["starttime"]):
            why = self.info if self.proc is None else f"exited too quickly ({describe_exit(self.proc.returncode)})"
            if self.retries < self.program["startretries"]:
                self.retries += 1
                log.warning("%s: %s, retry %d/%d", self.name, why, self.retries, self.program["startretries"])
                self.spawn()
            else:
                self.set_state(State.FATAL, f"{why}, gave up after {self.retries} retries", logging.ERROR)
            return
        code = self.proc.returncode
        expected = code in self.program["exitcodes"]
        self.set_state(State.EXITED, f"{describe_exit(code)}, {'expected' if expected else 'unexpected'}",
                       logging.INFO if expected else logging.WARNING)
        if self.program["autorestart"] == "always" or (self.program["autorestart"] == "unexpected" and not expected):
            self.start()

    def describe(self):
        if self.state in (State.STARTING, State.RUNNING) and self.proc is not None:
            up = int(time.monotonic() - self.started_at)
            return f"pid {self.proc.pid}, uptime {up // 3600}:{up // 60 % 60:02}:{up % 60:02}"
        return self.info

    def result(self):
        return f"{self.name}: {self.state.name} ({self.describe()})"


class Supervisor:
    # changing them needs a new process; the other options apply to the running one
    SPAWN_KEYS = ("cmd", "env", "workingdir", "umask", "stdout", "stderr", "user")

    def __init__(self, config_path):
        self.lock = threading.Condition()  # a command can also wait on it, releasing it meanwhile
        self.config_path = config_path
        self.jobs = []  # one Job per process, in config order
        self.reloading = False  # a reload is waiting for its old processes to stop
        self.reload_requested = False  # set by SIGHUP
        self.shutdown_requested = False  # set by SIGTERM, SIGINT and the shutdown command
        self.add_jobs(load_config(config_path))

    def on_signal(self, signum, frame):
        # runs in the main thread, maybe while it holds the lock: only set a flag
        if signum == signal.SIGHUP:
            self.reload_requested = True
        else:
            self.shutdown_requested = True

    def add_jobs(self, programs):
        """Create the jobs of `programs` that don't exist yet, and return them."""
        names = {job.name for job in self.jobs}
        added = [Job(name, i, program) for name, program in programs.items()
                 for i in range(program["numprocs"]) if f"{name}:{i}" not in names]
        self.jobs += added
        order = list(programs)
        self.jobs.sort(key=lambda job: (order.index(job.program_name), job.index))
        return added

    def select(self, names):
        """Return the jobs named like web, web:0 or all, and an error line per unknown name."""
        if names == ["all"]:
            return list(self.jobs), []
        jobs, errors = [], []
        for name in names:
            matching = [job for job in self.jobs if name in (job.program_name, job.name)]
            if not matching:
                errors.append(f"{name}: ERROR (no such process)")
            jobs += [job for job in matching if job not in jobs]
        return jobs, errors

    def update(self):
        for job in self.jobs:
            try:
                job.update()
            except Exception:
                log.exception("%s: unexpected error", job.name)
        self.lock.notify_all()  # wakes up the commands waiting in wait_while()

    def wait_while(self, jobs, state, timeout=None):
        """Wait until none of `jobs` is in `state`, without holding the lock meanwhile."""
        self.lock.wait_for(lambda: all(job.state is not state for job in jobs), timeout)

    def monitor(self):
        """Thread: keeps the jobs up to date, even when nobody sends a command."""
        while True:
            with self.lock:
                self.update()
            time.sleep(TICK)

    def start(self, jobs, wait=True):
        errors = {}
        for job in jobs:
            if job.retired:
                errors[job] = "removed by a reload"
            elif job.state is State.STOPPING:
                errors[job] = "still stopping"
            elif job.state in (State.STARTING, State.RUNNING):
                errors[job] = "already started"
            else:
                job.start()
        started = [job for job in jobs if job not in errors]
        if wait:  # until running or given up, at most the time all their attempts can take
            timeout = max(((j.program["starttime"] + 2 * TICK) * (j.program["startretries"] + 1) for j in started), default=0)
            self.wait_while(started, State.STARTING, timeout)
        return [f"{job.name}: ERROR ({errors[job]})" if job in errors else job.result() for job in jobs]

    def stop(self, jobs):
        for job in jobs:
            job.stop()
        self.wait_while(jobs, State.STOPPING)
        return [job.result() for job in jobs]

    def reload(self):
        """Apply the config file again, without touching the processes it doesn't change."""
        self.lock.wait_for(lambda: not self.reloading)  # one reload at a time
        try:
            programs = load_config(self.config_path)
        except ConfigError as e:
            log.error("reload failed, keeping the current config: %s", e)
            return [f"ERROR (reload failed, keeping the current config: {e})"]
        old = []
        for job in self.jobs:
            new = programs.get(job.program_name)
            if new is None or job.index >= new["numprocs"] or any(new[k] != job.program[k] for k in self.SPAWN_KEYS):
                job.retired = True
                old.append(job)
            else:
                job.program = new  # e.g. a new autorestart or stoptime
        self.reloading = True
        try:
            self.stop(old)  # before starting their replacements, e.g. to free a port
        finally:
            self.reloading = False
        self.jobs = [job for job in self.jobs if not job.retired]
        added = self.add_jobs(programs)
        if not self.shutdown_requested:
            self.start([job for job in added if job.program["autostart"]], wait=False)
        summary = f"removed: {' '.join(j.name for j in old) or '-'}, added: {' '.join(j.name for j in added) or '-'}"
        log.info("config reloaded (%s)", summary)
        return [f"config reloaded ({summary})"]

    def run(self):
        """Main thread: handles what the signal handlers asked for, until shutdown."""
        while not self.shutdown_requested:
            if self.reload_requested:
                self.reload_requested = False
                log.info("SIGHUP received")
                with self.lock:
                    try:
                        self.reload()
                    except Exception:
                        log.exception("reload failed")
            time.sleep(TICK)
        log.info("shutting down: stopping every job")
        with self.lock:
            self.stop(self.jobs)
        log.info("taskmasterd stopped")

    def execute(self, cmd, args):
        with self.lock:
            self.update()  # every command sees the processes as they are now
            if cmd == "status":
                jobs, errors = self.select(args) if args else (self.jobs, [])
                width = max((len(job.name) for job in jobs), default=0)
                lines = errors + [f"{job.name:<{width}}  {job.state.name:<8}  {job.describe()}" for job in jobs]
                return {"ok": not errors, "output": "\n".join(lines) or "no programs"}
            if self.shutdown_requested:
                return {"ok": False, "output": "ERROR (shutting down)"}
            log.info("command: %s", " ".join([cmd, *args]))
            if cmd == "shutdown":
                self.shutdown_requested = True
                return {"ok": True, "output": "shutting down", "shutdown": True}
            if cmd == "reload":
                lines = self.reload()
            elif cmd in ("start", "stop", "restart"):
                if not args:
                    return {"ok": False, "output": f"usage: {cmd} <name>|<name>:<index>|all ..."}
                jobs, errors = self.select(args)
                if cmd == "restart":
                    self.stop(jobs)
                lines = errors + (self.stop(jobs) if cmd == "stop" else self.start(jobs))
            else:
                return {"ok": False, "output": f"unknown command: {cmd}"}
            return {"ok": not any("ERROR" in line for line in lines), "output": "\n".join(lines)}


def main(config_path, logfile):
    logging.basicConfig(filename=logfile, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        supervisor = Supervisor(config_path)
    except ConfigError as e:
        sys.exit(f"taskmasterd: {e}")
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, supervisor.on_signal)
    try:
        srv = start_server(supervisor)
    except OSError as e:  # e.g. a socket left in /tmp by a taskmasterd run as root
        sys.exit(f"taskmasterd: cannot create {SOCK_PATH}: {e}")
    print(f"taskmasterd listening on {SOCK_PATH}")
    log.info("taskmasterd started (pid %d, config %s)", os.getpid(), config_path)
    threading.Thread(target=supervisor.monitor, daemon=True).start()
    with supervisor.lock:
        supervisor.start([job for job in supervisor.jobs if job.program["autostart"]], wait=False)
    try:
        supervisor.run()  # until shutdown, which stops every job
    finally:
        srv.server_close()
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)

def parse_arguments():
    parser = argparse.ArgumentParser(description="Taskmaster command-line interface.")
    parser.add_argument("--version", action="version", version="Taskmaster 1.0")
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to configuration file.")
    parser.add_argument("--logfile", type=str, default="taskmasterd.log", help="Path to the log file.")
    parser.add_argument("--socket", type=str, default="/tmp/taskmaster.sock", help="Path to the Unix socket file.")
    parser.add_argument("--launch", type=str, help="Command to launch the program.")
    parser.add_argument("--number", type=int, help="Number of instances to launch.")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_arguments()
    main(args.config, args.logfile)

