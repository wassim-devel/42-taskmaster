import json
import os
import shlex
import socketserver
import sys
import threading
import subprocess
import argparse

from config import ConfigError, load_config

SOCK_PATH = "/tmp/taskmaster.sock"


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:  # one request per line, until disconnection
            try:
                req = json.loads(line)
                resp = self.server.supervisor.execute(req["cmd"], req.get("args", []))
            except Exception as e:
                resp = {"ok": False, "output": f"error: {e}"}
            self.wfile.write((json.dumps(resp) + "\n").encode())


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

def spawn(program):
    """Launch one process of `program` (a config dict) and return its Popen."""
    outputs = []
    try:
        for path in (program["stdout"], program["stderr"]):
            outputs.append(subprocess.DEVNULL if path is None else open(path, "ab"))
        return subprocess.Popen(
            shlex.split(program["cmd"]),
            cwd=program["workingdir"],
            env={**os.environ, **program["env"]},
            umask=-1 if program["umask"] is None else program["umask"],
            stdin=subprocess.DEVNULL,
            stdout=outputs[0],
            stderr=outputs[1],
            start_new_session=True,  # Ctrl+C in taskmasterd's terminal must not reach the children
        )
    finally:
        for f in outputs:  # the child has its own copies of these descriptors
            if f is not subprocess.DEVNULL:
                f.close()


class Supervisor:
    def __init__(self, config_path):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.config_path = config_path
        self.programs = {}  # name -> config dict (see config.load_config)
        self.processes = {}  # name -> list of Popen (or None), one slot per numprocs
        self.read_config()

    def read_config(self):
        """(Re)load the config file. Raises ConfigError and keeps the current config if it is invalid."""
        self.programs = load_config(self.config_path)

    def start(self, names):
        if not names:
            return {"ok": False, "output": "usage: start <name> [<name> ...] | start all"}
        if names == ["all"]:
            names = list(self.programs)
        ok = True
        lines = []
        for name in names:
            program = self.programs.get(name)
            if program is None:
                lines.append(f"{name}: ERROR (no such program)")
                ok = False
                continue
            procs = self.processes.setdefault(name, [])
            procs.extend([None] * (program["numprocs"] - len(procs)))
            for i in range(program["numprocs"]):
                label = f"{name}:{i}"
                if procs[i] is not None and procs[i].poll() is None:
                    lines.append(f"{label}: ERROR (already running)")
                    ok = False
                    continue
                try:
                    procs[i] = spawn(program)
                except (OSError, subprocess.SubprocessError) as e:
                    lines.append(f"{label}: ERROR (spawn error: {e})")
                    ok = False
                else:
                    lines.append(f"{label}: started (pid {procs[i].pid})")
        return {"ok": ok, "output": "\n".join(lines)}

    def execute(self, cmd, args):
        with self.lock:
            if cmd == "start":
                return self.start(args)
            if cmd == "status":
                return {"ok": True, "output": "no programs yet"}
            if cmd == "shutdown":
                return {"ok": True, "output": "shutting down", "shutdown": True}
            return {"ok": False, "output": f"unknown command: {cmd}"}


def main(config_path):
    try:
        supervisor = Supervisor(config_path)
    except ConfigError as e:
        sys.exit(f"taskmasterd: {e}")
    srv = start_server(supervisor)
    print(f"taskmasterd listening on {SOCK_PATH}")
    try:
        supervisor.stop_event.wait()  # blocks the main thread until shutdown
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)

def parse_arguments():
    parser = argparse.ArgumentParser(description="Taskmaster command-line interface.")
    parser.add_argument("--version", action="version", version="Taskmaster 1.0")
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to configuration file.")
    parser.add_argument("--socket", type=str, default="/tmp/taskmaster.sock", help="Path to the Unix socket file.")
    parser.add_argument("--launch", type=str, help="Command to launch the program.")
    parser.add_argument("--number", type=int, help="Number of instances to launch.")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_arguments()
    main(args.config)

