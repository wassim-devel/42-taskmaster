import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
import time
import argparse
import errno

from config import ConfigError, load_config
from job import RUNNING_STATES, STOPPED_STATES, Job, State

SOCK_PATH = "/tmp/taskmaster.sock"
TICK = 0.1  # the main loop updates every process this often (seconds)

log = logging.getLogger("taskmasterd")


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            for line in self.rfile:  # one request per line, until disconnection
                try:
                    req = json.loads(line)
                    resp = self.server.supervisor.execute(req["cmd"], req.get("args", []))
                except Exception as e:
                    resp = {"ok": False, "output": f"error: {e}"}
                self.wfile.write((json.dumps(resp) + "\n").encode())
        except OSError:
            pass  # the client left, maybe while its command was running


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True  # client threads are daemonic, so they won't block the server from exiting


def start_server(supervisor):
    if os.path.exists(SOCK_PATH):
        with socket.socket(socket.AF_UNIX) as s:
            s.setblocking(False)  # a frozen taskmasterd (Ctrl+Z) must not block this check
            if s.connect_ex(SOCK_PATH) in (0, errno.EAGAIN):  # like supervisord: never run twice
                raise OSError("another taskmasterd is listening on it")
        os.unlink(SOCK_PATH)  # delete the socket file if it already exists
    srv = Server(SOCK_PATH, Handler)
    os.chmod(SOCK_PATH, 0o600)  # only my user can connect
    srv.supervisor = supervisor
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Supervisor:
    """The jobs, the main loop that drives them, and the commands of taskmasterctl.

    The main thread runs the main loop (run), each client has its own thread (execute).
    Both only touch the jobs with self.lock held, and never block while holding it:
    a command that must wait (start, stop) waits on self.changed, which releases the lock.
    """

    def __init__(self, config_path):
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)  # notified after every tick of the main loop
        self.config_path = config_path
        self.jobs = {name: Job(name, config) for name, config in load_config(config_path).items()}
        self.shutting_down = False
        self.signals = set()  # received signals, handled by the main loop

    # --- main loop (main thread) ---

    def setup_signals(self):
        """A handler runs in the main thread between two bytecodes, maybe while it holds the lock:
        it only records the signal, and the main loop handles it, like supervisor does."""
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
            signal.signal(sig, lambda signum, frame: self.signals.add(signum))

    def run(self):
        """Every TICK, let every process update its own state. Returns once the shutdown is over."""
        while True:
            signums, self.signals = self.signals, set()
            with self.lock:
                try:
                    for signum in signums - {signal.SIGHUP}:  # first: a failed reload must not lose them
                        self.shutdown(f"{signal.Signals(signum).name} received")
                    if signal.SIGHUP in signums and not self.shutting_down:
                        log.info("SIGHUP received: reloading %s", self.config_path)
                        self.reload()
                except Exception:  # a bug must never stop the supervision
                    log.exception("error while handling signals")
                self.tick()
                self.changed.notify_all()
                if self.shutting_down and self.all_stopped():
                    return
            time.sleep(TICK)

    def tick(self):
        for job in list(self.jobs.values()):
            try:
                job.reap()
                job.transition(not self.shutting_down)
            except Exception:  # a bug in one job must not stop the supervision of the others
                log.exception("%s: error in the main loop", job.name)
            if job.removed and not job.leaving:
                del self.jobs[job.name]
                log.info("%s: removed (its processes are stopped)", job.name)

    def all_stopped(self):
        return all(p.state in STOPPED_STATES for job in self.jobs.values() for p in job.all_processes())

    def shutdown(self, why):
        if not self.shutting_down:
            log.info("shutdown (%s): stopping every process", why)
            self.shutting_down = True
            for job in self.jobs.values():
                job.stop_all()

    def reload(self, args=()):
        """Apply the config file again, like `supervisorctl update`: unchanged jobs are not touched."""
        if self.shutting_down:
            log.warning("reload refused: shutting down")
            return {"ok": False, "output": "ERROR (shutting down)"}
        try:
            programs = load_config(self.config_path)
        except ConfigError as e:
            log.error("reload failed, the current config is kept: %s", e)
            return {"ok": False, "output": f"ERROR (config not reloaded: {e})"}
        lines = []
        for name, job in self.jobs.items():
            if name not in programs and not job.removed:
                job.remove()
                lines.append(f"{name}: removed")
        for name, config in programs.items():
            job = self.jobs.get(name)
            if job is None:
                self.jobs[name] = Job(name, config)
                lines.append(f"{name}: added")
            else:
                change = job.update(config)
                if change:
                    lines.append(f"{name}: {change}")
        lines = lines or ["no changes"]
        for line in lines:
            log.info("reload: %s", line)
        return {"ok": True, "output": "\n".join(lines)}

    # --- commands (client threads) ---

    def execute(self, cmd, args):
        commands = {
            "status": self.status,
            "start": self.start,
            "stop": self.stop,
            "restart": self.restart,
            "reload": self.reload,
            "shutdown": self.shutdown_command,
        }
        if cmd not in commands:
            return {"ok": False, "output": f"unknown command: {cmd}"}
        if cmd != "status":
            log.info("command: %s", " ".join([cmd, *args]))
        with self.lock:
            self.tick()  # so that a process killed a moment ago is already seen as dead (or restarted)
            return commands[cmd](args)

    def find(self, names, leaving=False):
        """The processes named by `names`: all, a job (web) or a process (web:0). Returns (processes, errors).

        leaving: also the old processes that a reload is still stopping.
        """
        def procs_of(job):
            return job.all_processes() if leaving else job.processes
        if "all" in names:
            return [p for job in self.jobs.values() for p in procs_of(job)], []
        procs, errors = [], []
        for name in names:
            job_name, _, index = name.partition(":")
            job = self.jobs.get(job_name)
            found = [] if job is None else [p for p in procs_of(job) if index in ("", str(p.index))]
            if not found:
                errors.append(f"{name}: ERROR (no such process)")
            procs += found
        return procs, errors

    def status(self, names):
        """Never waits."""
        procs, lines = self.find(names or ["all"], leaving=True)
        leaving = [p for job in self.jobs.values() for p in job.leaving]
        width = max([30] + [len(p.name) for p in procs]) + 3
        for proc in procs:
            line = f"{proc.name:<{width}}{proc.state.name:<10}{proc.description()}"
            lines.append(line + (" (old process, stopping after a reload)" if proc in leaving else ""))
        return {"ok": True, "output": "\n".join(lines) or "no programs"}

    def start(self, names):
        """Spawn, then wait until every process is RUNNING (starttime) or failed, like supervisorctl."""
        if not names:
            return {"ok": False, "output": "usage: start <name>|<name>:<index>|all ..."}
        if self.shutting_down:
            return {"ok": False, "output": "ERROR (shutting down)"}
        procs, lines = self.find(names)
        ok = not lines
        started = []
        for proc in procs:
            if proc.state in RUNNING_STATES:
                if "all" not in names:  # supervisorctl start all is silent about those
                    lines.append(f"{proc.name}: ERROR (already started)")
                    ok = False
            elif proc.state is State.STOPPING or self.jobs[proc.job_name].busy(proc.index):
                lines.append(f"{proc.name}: ERROR (still stopping)")
                ok = False
            else:
                started.append((proc, proc.runs))
                proc.backoff = 0  # a start command gets all its retries back
                proc.spawn()
        # runs, not only the state: it may reach RUNNING, exit and be autorestarted within one tick
        self.changed.wait_for(lambda: all(p.runs > runs or p.state is not State.STARTING for p, runs in started))
        for proc, runs in started:
            if proc.runs > runs:
                lines.append(f"{proc.name}: started")
            else:
                lines.append(f"{proc.name}: ERROR ({proc.spawnerr or 'abnormal termination'})")
                ok = False
        return {"ok": ok, "output": "\n".join(lines)}

    def stop(self, names):
        """Send stopsignal, then wait until every process is dead (the main loop sends SIGKILL after stoptime)."""
        if not names:
            return {"ok": False, "output": "usage: stop <name>|<name>:<index>|all ..."}
        procs, lines = self.find(names, leaving=True)
        ok = not lines
        stopping = []
        for proc in procs:
            if proc.state not in STOPPED_STATES:
                proc.stop()
                stopping.append(proc)
            elif self.jobs[proc.job_name].busy(proc.index):  # its old process, still stopping, answers for it
                proc.laststart = proc.laststart or time.monotonic()  # cancel its pending autostart
            elif "all" not in names:  # supervisorctl stop all is silent about those
                lines.append(f"{proc.name}: ERROR (not running)")
                ok = False
        self.changed.wait_for(lambda: all(p.state is not State.STOPPING for p in stopping))
        for proc in stopping:
            killed = f" (killed with SIGKILL after {proc.config['stoptime']}s)" if proc.killed else ""
            lines.append(f"{proc.name}: stopped{killed}")
        return {"ok": ok, "output": "\n".join(lines)}

    def restart(self, names):
        if not names:
            return {"ok": False, "output": "usage: restart <name>|<name>:<index>|all ..."}
        stopped = self.stop(names)
        started = self.start(names)
        output = "\n".join(line for line in (stopped["output"], started["output"]) if line)
        return {"ok": stopped["ok"] and started["ok"], "output": output}

    def shutdown_command(self, args):
        self.shutdown("shutdown command")
        return {"ok": True, "output": "shutting down: stopping every process"}


def main(config_path, logfile):
    try:
        handler = logging.FileHandler(logfile)
    except OSError as e:
        sys.exit(f"taskmasterd: cannot open the log file: {e}")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        supervisor = Supervisor(config_path)
    except ConfigError as e:
        sys.exit(f"taskmasterd: {e}")
    supervisor.setup_signals()  # before the socket exists, so that no command comes earlier
    try:
        srv = start_server(supervisor)
    except OSError as e:  # e.g. a socket left in /tmp by a taskmasterd run as root
        sys.exit(f"taskmasterd: cannot create {SOCK_PATH}: {e}")
    log.info("taskmasterd started (pid %d), config %s", os.getpid(), config_path)
    print(f"taskmasterd listening on {SOCK_PATH}, logging to {logfile}")
    try:
        supervisor.run()  # the main loop, until the shutdown is over
    finally:
        srv.shutdown()
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)  # before closing: then a new taskmasterd can't have taken the path
        srv.server_close()
        log.info("taskmasterd stopped")

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
