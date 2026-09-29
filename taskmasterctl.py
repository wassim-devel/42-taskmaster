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
        resp = self.client.send(command, *arg.split())
        print(resp["output"])

    def do_status(self, arg):
        """status : status of programs"""
        self._run("status", arg)

    def do_start(self, arg):
        """start <name>"""
        self._run("start", arg)

    # implement stop, restart, reload
    def do_shutdown(self, arg):
        """shutdown : stop the daemon"""
        self._run("shutdown", arg)
        return True

    def do_stop(self, arg):
        """stop <name>"""
        print("hey")
        self._run("stop", arg)

    def do_restart(self, arg):
        """restart <name>"""
        self._run("restart", arg)

    def do_reload(self, arg):
        """reload <name>"""
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
    TaskmasterShell(client).cmdloop()