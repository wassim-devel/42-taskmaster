import pwd
import re
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


def is_number(value, integer=False):
    if isinstance(value, bool):  # YAML's `yes` is an int for Python
        return False
    return isinstance(value, int) or (not integer and isinstance(value, float))


def is_name(value):
    """A short name or a small number: safe to give to str() and to the C library."""
    return isinstance(value, str) and len(value) <= 64 or is_number(value, integer=True) and 0 <= value < 2**32


def check(name, prog):
    """Reject the values the main loop could not use: a reload must never crash the daemon."""
    try:
        if not isinstance(prog["cmd"], str) or not shlex.split(prog["cmd"]):
            raise ValueError("expected a non-empty command line")
    except ValueError as e:  # also raised by shlex, e.g. "No closing quotation"
        raise ConfigError(f"{name}: invalid cmd: {e}") from None
    if not is_number(prog["numprocs"], integer=True) or not 1 <= prog["numprocs"] <= 1000:
        raise ConfigError(f"{name}: numprocs must be an integer between 1 and 1000")
    if not is_number(prog["startretries"], integer=True) or prog["startretries"] < 0:
        raise ConfigError(f"{name}: startretries must be an integer >= 0")
    for key in ("starttime", "stoptime"):
        if not is_number(prog[key]) or not 0 <= prog[key] <= 86400:  # also rejects .nan and .inf
            raise ConfigError(f"{name}: {key} must be a number of seconds between 0 and 86400")
    if not isinstance(prog["autostart"], bool):
        raise ConfigError(f"{name}: autostart must be true or false")
    if prog["autorestart"] not in ("always", "never", "unexpected"):
        raise ConfigError(f"{name}: autorestart must be always, never or unexpected")
    if not all(is_number(code, integer=True) and 0 <= code <= 255 for code in prog["exitcodes"]):
        raise ConfigError(f"{name}: exitcodes must be integers between 0 and 255")
    if prog["umask"] is not None and not (is_number(prog["umask"], integer=True) and 0 <= prog["umask"] <= 0o777):
        raise ConfigError(f"{name}: umask must be an octal number like 022")
    for key in ("workingdir", "stdout", "stderr"):
        if prog[key] is not None and not isinstance(prog[key], str):
            raise ConfigError(f"{name}: {key} must be a path")
    if not isinstance(prog["env"] or {}, dict):
        raise ConfigError(f"{name}: env must be a mapping")
    for key in ("stopsignal", "user"):
        if prog[key] is not None and not is_name(prog[key]):
            raise ConfigError(f"{name}: {key} must be a name or a number")


def load_config(path):
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except Exception as e:  # anything raised while reading or parsing it: not UTF-8, bad YAML...
        raise ConfigError(f"cannot read {path}: {' '.join(str(e).split())}") from None  # on one line, for the log
    if isinstance(data, dict) and data.get("programs", {}) is None:
        data["programs"] = {}  # every program is commented out
    if not isinstance(data, dict) or not isinstance(data.get("programs"), dict):
        raise ConfigError(f"{path}: a 'programs' mapping is required")
    programs = {}
    for name, raw in data["programs"].items():
        if not isinstance(name, str):  # YAML reads `42:` as a number and `yes:` as true
            raise ConfigError(f"{name}: invalid program name: quote it")
        if not re.fullmatch(r"[\w.-]+", name) or name == "all":
            raise ConfigError(f"{name!r}: invalid program name (letters, digits, '_', '-', '.', and not 'all')")
        if not isinstance(raw, dict):
            raise ConfigError(f"{name}: expected a mapping of options")
        unknown = set(raw) - set(DEFAULTS) - {"cmd"}
        if unknown:
            raise ConfigError(f"{name}: unknown option(s): {', '.join(map(str, unknown))}")
        if "cmd" not in raw:
            raise ConfigError(f"{name}: missing cmd")
        if "user" in raw and raw["user"] is None:  # a forgotten value must not silently mean root
            raise ConfigError(f"{name}: empty user (remove the key to run as taskmasterd's user)")
        prog = {**DEFAULTS, **raw}
        if not isinstance(prog["exitcodes"], list):
            prog["exitcodes"] = [prog["exitcodes"]]
        if isinstance(prog["umask"], str):  # "022", like supervisor's octal strings
            try:
                prog["umask"] = int(prog["umask"], 8)
            except ValueError:
                raise ConfigError(f"{name}: umask must be an octal number like 022") from None
        check(name, prog)
        try:
            prog["stopsignal"] = signal.Signals["SIG" + str(prog["stopsignal"]).upper().removeprefix("SIG")]
        except KeyError:
            raise ConfigError(f"{name}: unknown stopsignal: {prog['stopsignal']!r}") from None
        prog["env"] = {str(k): str(v) for k, v in (prog["env"] or {}).items()}
        if prog["user"] is not None:
            user = str(prog["user"])  # a name or a uid, like supervisor
            try:
                prog["user"] = pwd.getpwuid(int(user)) if user.isdecimal() else pwd.getpwnam(user)
            except (KeyError, ValueError):  # ValueError: a NUL in the name
                raise ConfigError(f"{name}: unknown user: {user!r}") from None
        programs[name] = prog
    return programs
