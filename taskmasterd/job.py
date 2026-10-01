"""Jobs and their processes, with supervisor's state machine (supervisor/process.py).

Nothing here blocks: a method spawns a process or sends a signal, changes the state and returns.
The timed transitions (starttime, retry delays, stoptime) happen in transition(), called by the
main loop on every tick. Every method is called with the Supervisor's lock held.
"""
import datetime
import enum
import logging
import os
import shlex
import signal
import subprocess
import time

log = logging.getLogger("taskmasterd")


class State(enum.Enum):
    """The state of one process: supervisor's ProcessStates, without UNKNOWN."""
    STOPPED = enum.auto()   # not started yet, or stopped by a command
    STARTING = enum.auto()  # spawned, must stay up `starttime` seconds to be RUNNING
    RUNNING = enum.auto()   # stayed up `starttime` seconds: successfully started
    BACKOFF = enum.auto()   # failed to start, retried after 1s, then 2s, 3s...
    STOPPING = enum.auto()  # `stopsignal` sent, SIGKILL if still alive after `stoptime` seconds
    EXITED = enum.auto()    # exited by itself after RUNNING, with an expected code or not
    FATAL = enum.auto()     # failed to start `startretries` + 1 times in a row: given up


RUNNING_STATES = (State.STARTING, State.RUNNING, State.BACKOFF)  # start has nothing to do
STOPPED_STATES = (State.STOPPED, State.EXITED, State.FATAL)  # stop has nothing to do


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


def describe_exit(returncode):
    """A Popen returncode, like supervisor writes it: "exit status 1", "terminated by SIGKILL"."""
    if returncode >= 0:
        return f"exit status {returncode}"
    try:
        return f"terminated by {signal.Signals(-returncode).name}"
    except ValueError:
        return f"terminated by signal {-returncode}"


class Process:
    """One of the `numprocs` processes of a job (supervisor's Subprocess).

    Its state only changes through change_state(), which logs every transition.
    """

    def __init__(self, job_name, index, config):
        self.job_name = job_name
        self.index = index
        self.name = f"{job_name}:{index}"
        self.config = config  # what it runs with: a reload may give the job a new config
        self.state = State.STOPPED
        self.popen = None  # while there is a process: STARTING, RUNNING, STOPPING
        self.laststart = 0  # time.monotonic() of the last spawn, 0 if never spawned
        self.laststop = None  # time.time() of the last death, for status
        self.delay = 0  # BACKOFF: when to retry. STOPPING: when to send SIGKILL
        self.backoff = 0  # failed starts in a row
        self.runs = 0  # times it reached RUNNING
        self.killed = False  # the last stop needed SIGKILL
        self.exitstatus = None  # returncode of the last death
        self.spawnerr = None  # why the last start failed, for status

    def change_state(self, new, why, level=logging.INFO):
        if new is State.RUNNING:
            self.runs += 1
        elif new is State.BACKOFF:
            self.backoff += 1
            self.delay = time.monotonic() + self.backoff  # 1s, 2s, 3s... like supervisor
            if self.backoff <= self.config["startretries"]:
                why += f", retry {self.backoff}/{self.config['startretries']} in {self.backoff}s"
        log.log(level, "%s: %s -> %s (%s)", self.name, self.state.name, new.name, why)
        self.state = new

    def spawn(self):
        """-> STARTING, or -> BACKOFF if it can't even be launched."""
        self.laststart = time.monotonic()
        try:
            self.popen = spawn(self.config)  # the module's spawn(), not this method
        except Exception as e:  # a bad cmd, user, workingdir... is a failed start, not a crash
            self.spawnerr = f"spawn error: {e}"
            self.change_state(State.BACKOFF, self.spawnerr, logging.WARNING)
        else:
            self.spawnerr = None
            self.change_state(State.STARTING, f"pid {self.popen.pid}")

    def stop(self):
        """Never waits: transition() sends SIGKILL once stoptime is over."""
        if self.state is not State.STOPPING:  # a new stop: forget whether the last one needed SIGKILL
            self.killed = False
        if self.state is State.BACKOFF:  # no process, just don't retry
            self.change_state(State.STOPPED, "stopped while waiting to retry")
        elif self.state in (State.STARTING, State.RUNNING):
            self.kill(self.config["stopsignal"])

    def kill(self, sig):
        self.delay = time.monotonic() + self.config["stoptime"]
        if self.state is not State.STOPPING:
            self.change_state(State.STOPPING, f"sending {sig.name} to pid {self.popen.pid}")
        try:
            signal_group(self.popen, sig)
        except OSError as e:  # e.g. EPERM: SIGKILL is sent again every stoptime
            log.error("%s: cannot send %s: %s", self.name, sig.name, e)

    def reap(self):
        """poll() reaps this pid only: never os.waitpid(-1), it would steal Popen's exit status."""
        if self.popen is not None and self.popen.poll() is not None:
            self.finish(self.popen.returncode)

    def finish(self, returncode):
        """The process died: what does it mean? (supervisor's Subprocess.finish)"""
        self.popen = None
        self.laststop = time.time()
        self.exitstatus = returncode
        how = describe_exit(returncode)
        if self.state is State.STOPPING:  # we asked for it
            self.change_state(State.STOPPED, how)
        elif self.state is State.STARTING and time.monotonic() - self.laststart < self.config["starttime"]:
            self.spawnerr = f"exited too quickly ({how})"  # even with an expected code: the start failed
            self.change_state(State.BACKOFF, self.spawnerr, logging.WARNING)
        else:
            if self.state is State.STARTING:  # starttime is over, but no tick noticed it yet
                self.change_state(State.RUNNING, f"stayed up {self.config['starttime']}s")
            self.backoff = 0
            if returncode in self.config["exitcodes"]:
                self.change_state(State.EXITED, f"{how}, expected")
            else:
                self.change_state(State.EXITED, f"{how}, not expected", logging.WARNING)

    def transition(self, may_spawn):
        """Called on every tick: the process updates its own state (supervisor's Subprocess.transition).

        may_spawn is False while shutting down, and while the old process of this slot
        (replaced by a reload) is still stopping.
        """
        now = time.monotonic()
        conf = self.config
        if may_spawn:
            if self.state is State.EXITED:
                if conf["autorestart"] == "always" or (
                        conf["autorestart"] == "unexpected" and self.exitstatus not in conf["exitcodes"]):
                    self.spawn()
            elif self.state is State.STOPPED and not self.laststart:  # never started
                if conf["autostart"]:
                    self.spawn()
            elif self.state is State.BACKOFF and self.backoff <= conf["startretries"] and now >= self.delay:
                self.spawn()
        if self.state is State.STARTING and now - self.laststart >= conf["starttime"]:
            self.backoff = 0
            self.change_state(State.RUNNING, f"stayed up {conf['starttime']}s")
        elif self.state is State.BACKOFF and self.backoff > conf["startretries"]:
            self.backoff = 0
            self.change_state(State.FATAL, f"gave up after {conf['startretries']} retries", logging.ERROR)
        elif self.state is State.STOPPING and now >= self.delay:
            log.warning("%s: still alive %ss after the stop signal, sending SIGKILL", self.name, conf["stoptime"])
            self.killed = True
            self.kill(signal.SIGKILL)

    def description(self):
        """The last column of status, like supervisorctl's."""
        if self.state is State.RUNNING:
            uptime = datetime.timedelta(seconds=int(time.monotonic() - self.laststart))
            return f"pid {self.popen.pid}, uptime {uptime}"
        if self.state in (State.STARTING, State.STOPPING):
            return f"pid {self.popen.pid}"
        if self.state in (State.BACKOFF, State.FATAL):
            return self.spawnerr
        if self.laststop is None:
            return "Not started"
        when = time.strftime("%b %d %I:%M:%S %p", time.localtime(self.laststop))
        if self.state is State.EXITED:
            expected = "expected" if self.exitstatus in self.config["exitcodes"] else "not expected"
            return f"{describe_exit(self.exitstatus)} ({expected}), {when}"
        return when


# The options that define the process itself: a reload that changes one of them respawns the job.
# The others (numprocs, autostart, autorestart, exitcodes, starttime, startretries, stopsignal,
# stoptime) only change how it is watched: a reload applies them to the processes it keeps.
# The ones it stops are stopped with the config they ran with, like supervisor.
SPAWN_OPTIONS = ("cmd", "env", "workingdir", "umask", "stdout", "stderr", "user")


class Job:
    """A program of the config file (supervisor's ProcessGroup): its config and its processes."""

    def __init__(self, name, config):
        self.name = name
        self.config = config
        self.processes = [Process(name, i, config) for i in range(config["numprocs"])]
        self.leaving = []  # processes replaced or removed by a reload, until they are stopped
        self.removed = False  # removed by a reload: forgotten once `leaving` is empty

    def all_processes(self):
        return self.leaving + self.processes

    def busy(self, index):
        """True while the old process of slot `index`, replaced by a reload, is still stopping."""
        return any(p.index == index for p in self.leaving)

    def reap(self):
        for proc in self.all_processes():
            proc.reap()

    def transition(self, may_spawn):
        for proc in self.leaving:
            proc.transition(False)
        self.leaving = [p for p in self.leaving if p.state not in STOPPED_STATES]
        for proc in self.processes:  # never run the old and the new name:i together
            proc.transition(may_spawn and not self.busy(proc.index))

    def stop_all(self):
        for proc in self.all_processes():
            proc.stop()

    def retire(self, procs):
        """Stop `procs`, and keep them in `leaving` until they are dead."""
        for proc in procs:
            proc.stop()
        self.leaving += [p for p in procs if p.state not in STOPPED_STATES]

    def remove(self):
        self.retire(self.processes)
        self.processes = []
        self.removed = True

    def update(self, config):
        """Apply a reloaded config. Returns what was done, or None if nothing changed."""
        if not self.removed and config == self.config:
            return None
        if not self.removed and all(config[key] == self.config[key] for key in SPAWN_OPTIONS):
            self.config = config
            numprocs = config["numprocs"]
            self.retire(self.processes[numprocs:])
            kept = self.processes[:numprocs]
            for proc in kept:
                proc.config = config
            # a FATAL one gets a fresh Process (autostart may start it again), like supervisor
            self.processes = [Process(self.name, p.index, config) if p.state is State.FATAL else p for p in kept]
            self.processes += [Process(self.name, i, config) for i in range(len(kept), numprocs)]
            return "updated in place"
        self.retire(self.processes)  # the new processes wait for the old ones: see transition()
        self.removed = False
        self.config = config
        self.processes = [Process(self.name, i, config) for i in range(config["numprocs"])]
        return "changed (processes respawned)"
