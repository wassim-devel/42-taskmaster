import cmd
import json
import readline
import socket
import sys

SOCK_PATH = "/tmp/taskmaster.sock"

class Client:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.file = self.sock.makefile("rb")

    def send(self, command, *args):
        msg = json.dumps({"cmd": command, "args": list(args)}) + "\n"
        self.sock.sendall(msg.encode())  # unbuffered: nothing left to flush at exit if the daemon is gone
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

    def preloop(self):
        readline.set_completer_delims(" ")  # complete "web:0" as one word

    def complete_start(self, text, line, begidx, endidx):
        """Complete the program and process names given by status."""
        try:
            lines = self.client.send("status")["output"].splitlines()
        except OSError:
            return []
        procs = [words[0] for words in map(str.split, lines) if words and ":" in words[0]]
        names = {"all", *procs, *(proc.split(":")[0] for proc in procs)}
        return sorted(name for name in names if name.startswith(text))

    complete_stop = complete_restart = complete_status = complete_start

    def do_status(self, arg):
        """status [<name> ...] : status of all programs, or of some"""
        self._run("status", arg)

    def do_start(self, arg):
        """start <name>|<name>:<index>|all ... : start, and wait until running (starttime)"""
        self._run("start", arg)

    def do_shutdown(self, arg):
        """shutdown : stop every program, then the daemon"""
        if arg:  # like supervisorctl: "shutdown web" must not stop everything
            print("Error: shutdown accepts no arguments")
            return
        self._run("shutdown", arg)
        return True

    def do_stop(self, arg):
        """stop <name>|<name>:<index>|all ... : stop, and wait until dead (SIGKILL after stoptime)"""
        self._run("stop", arg)

    def do_restart(self, arg):
        """restart <name>|<name>:<index>|all ... : stop, then start"""
        self._run("restart", arg)

    def do_reload(self, arg):
        """reload : reload the config file, like SIGHUP (unchanged programs keep running)"""
        if arg:
            print("Error: reload accepts no arguments")
            return
        self._run("reload", arg)

    def do_quit(self, arg):
        """quit : quits client (daemon continues)"""
        return True

    do_exit = do_quit

    def do_EOF(self, arg):
        """Ctrl+D : quits client (daemon continues)"""
        print()  # end the prompt's line
        return True


if __name__ == "__main__":
    try:
        client = Client(SOCK_PATH)
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit("taskmasterd isn't launched")
    except PermissionError:
        sys.exit(f"{SOCK_PATH}: permission denied (taskmasterd runs as root? use sudo)")
    try:
        TaskmasterShell(client).cmdloop()
    except KeyboardInterrupt:
        print()  # Ctrl+C: leave the client (the daemon continues)
