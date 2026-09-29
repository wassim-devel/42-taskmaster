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
}


def load_config(path):
    with open(path) as f:
        data = yaml.safe_load(f)
    programs = {}
    for name, raw in data["programs"].items():
        unknown = set(raw) - set(DEFAULTS) - {"cmd"}
        if unknown:
            raise ConfigError(f"{name}: unknown option(s): {', '.join(unknown)}")
        if "cmd" not in raw:
            raise ConfigError(f"{name}: missing cmd")
        prog = {**DEFAULTS, **raw}
        if not isinstance(prog["exitcodes"], list):
            prog["exitcodes"] = [prog["exitcodes"]]
        prog["stopsignal"] = signal.Signals["SIG" + prog["stopsignal"]]
        prog["env"] = {k: str(v) for k, v in (prog["env"] or {}).items()}
        programs[name] = prog
    return programs
