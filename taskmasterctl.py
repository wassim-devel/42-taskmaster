import cmd
import json
import socket
import sys

SOCK_PATH = "/tmp/taskmaster.sock"

class Client:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.file = self.sock.makefile("rwb")

    def send(self, command, *args):
        msg = json.dumps({"cmd": command, "args": list(args)}) + "\n"
        self.file.write(msg.encode())
        self.file.flush()
        line = self.file.readline()
        if not line:
            raise ConnectionError("daemon disconnected")
        return json.loads(line)

class TaskmasterShell(cmd.Cmd):
    prompt = "taskmaster> "

    def __init__(self, client):
        super().__init__()
        self.client = client

    def _run(self, command, arg):
        try:
            resp = self.client.send(command, *arg.split())
        except OSError as e:  # ConnectionError included: taskmasterd is gone
            sys.exit(f"taskmasterd: {e}")
        print(resp["output"])

    def emptyline(self):
        pass  # cmd.Cmd would repeat the last command, e.g. a restart

    def do_status(self, arg):
        """status [<name> ...] : status of all programs, or of some"""
        self._run("status", arg)

    def do_start(self, arg):
        """start <name>|<name>:<index>|all"""
        self._run("start", arg)

    def do_shutdown(self, arg):
        """shutdown : stop the daemon"""
        self._run("shutdown", arg)
        return True

    def do_stop(self, arg):
        """stop <name>|<name>:<index>|all"""
        self._run("stop", arg)

    def do_restart(self, arg):
        """restart <name>|<name>:<index>|all"""
        self._run("restart", arg)

    def do_reload(self, arg):
        """reload : reload the config file, like SIGHUP"""
        self._run("reload", arg)

    def do_quit(self, arg):
        """quit : quits client (daemon continues)"""
        return True

    do_EOF = do_quit # Handle Ctrl+D


if __name__ == "__main__":
    try:
        client = Client(SOCK_PATH)
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit("taskmasterd isn't launched")
    except PermissionError:
        sys.exit(f"{SOCK_PATH}: permission denied (taskmasterd runs as root? use sudo)")
    TaskmasterShell(client).cmdloop()