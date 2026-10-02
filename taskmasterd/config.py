import pwd
import shlex
import signal
import yaml


class ConfigError(Exception):
    pass


DEFAULTS = {
    "numprocs": 1, 
    "umask": None, 
    "workingdir": None, 
    "autostart": True,
    "autorestart": "unexpected",
    "exitcodes": [0], 
    "startretries": 3,
    "starttime": 1, 
    "stopsignal": "TERM",
    "stoptime": 10,
    "stdout": None, 
    "stderr": None, 
    "env": {},
    "user": None,
}


def is_number(value, kind=(int, float)):
    return isinstance(value, kind) and not isinstance(value, bool)  # YAML's `yes` is an int for Python


def check(name, prog):
    """Reject the values the daemon couldn't use: a bad reload must not crash it."""
    try:
        if not isinstance(prog["cmd"], str) or not shlex.split(prog["cmd"]):
            raise ValueError("empty command")
    except ValueError as e:  # shlex raises it too, e.g. "No closing quotation"
        raise ConfigError(f"{name}: invalid cmd: {e}") from None
    if not is_number(prog["numprocs"], int) or not 1 <= prog["numprocs"] <= 100:
        raise ConfigError(f"{name}: numprocs must be an integer between 1 and 100")
    if not is_number(prog["startretries"], int) or prog["startretries"] < 0:
        raise ConfigError(f"{name}: startretries must be an integer >= 0")
    for key in ("starttime", "stoptime"):
        if not is_number(prog[key]) or not 0 <= prog[key] <= 3600:  # also rejects .nan and .inf
            raise ConfigError(f"{name}: {key} must be a number of seconds between 0 and 3600")
    if not isinstance(prog["autostart"], bool):
        raise ConfigError(f"{name}: autostart must be true or false")
    if prog["autorestart"] not in ("always", "never", "unexpected"):
        raise ConfigError(f"{name}: autorestart must be always, never or unexpected")
    if not all(is_number(code, int) for code in prog["exitcodes"]):
        raise ConfigError(f"{name}: exitcodes must be integers")
    if prog["umask"] is not None and not (is_number(prog["umask"], int) and 0 <= prog["umask"] <= 0o777):
        raise ConfigError(f"{name}: umask must be an octal number like 022")
    for key in ("workingdir", "stdout", "stderr"):
        if prog[key] is not None and not isinstance(prog[key], str):
            raise ConfigError(f"{name}: {key} must be a path")
    if not isinstance(prog["env"] or {}, dict):
        raise ConfigError(f"{name}: env must be a mapping")


def load_config(path):
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("programs"), dict):
        raise ConfigError(f"{path}: a 'programs' mapping is required")
    programs = {}
    for name, raw in data["programs"].items():
        if not isinstance(name, str) or ":" in name or name == "all":  # status names are name:index
            raise ConfigError(f"{name!r}: invalid program name")
        if not isinstance(raw, dict):
            raise ConfigError(f"{name}: expected a mapping of options")
        unknown = set(raw) - set(DEFAULTS) - {"cmd"}
        if unknown:
            raise ConfigError(f"{name}: unknown option(s): {', '.join(unknown)}")
        if "cmd" not in raw:
            raise ConfigError(f"{name}: missing cmd")
        if "user" in raw and raw["user"] is None:  # a forgotten value must not silently mean root
            raise ConfigError(f"{name}: empty user (remove the key to run as taskmasterd's user)")
        prog = {**DEFAULTS, **raw}
        if not isinstance(prog["exitcodes"], list):
            prog["exitcodes"] = [prog["exitcodes"]]
        check(name, prog)
        try:
            prog["stopsignal"] = signal.Signals["SIG" + str(prog["stopsignal"]).removeprefix("SIG")]
        except KeyError:
            raise ConfigError(f"{name}: unknown stopsignal: {prog['stopsignal']}") from None
        prog["env"] = {k: str(v) for k, v in (prog["env"] or {}).items()}
        if prog["user"] is not None:
            user = str(prog["user"])  # a name or a uid, like supervisor
            try:
                prog["user"] = pwd.getpwuid(int(user)) if user.isdecimal() else pwd.getpwnam(user)
            except KeyError:
                raise ConfigError(f"{name}: unknown user: {user}") from None
        programs[name] = prog
    return programs
