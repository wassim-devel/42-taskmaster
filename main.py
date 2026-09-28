import cmd
import readline
import argparse

def parse_arguments():
    parser = argparse.ArgumentParser(description="Taskmaster command-line interface.")
    parser.add_argument("--version", action="version", version="Taskmaster 1.0")
    return parser.parse_args()

def main():
    args = parse_arguments()
    shell = TaskmasterShell()
    shell.cmdloop()

    print("Hello, World!")



class TaskmasterShell(cmd.Cmd):
    intro = "Welcome to taskmaster! Type help or ? to list commands.\n"
    prompt = "(taskmaster) "

    def do_start(self, arg):
        """Start program."""
        print("Starting taskmaster...")

    def do_stop(self, arg):
        """Stop program."""
        print("Stopping taskmaster...")
        return True

    def do_restart(self, arg):
        """Restart program."""
        print("Restarting taskmaster...")

    def do_exit(self, arg):
        """Exit the shell."""
        print("Goodbye!")
        return True

if __name__ == "__main__":
    main()