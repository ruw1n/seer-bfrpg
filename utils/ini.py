# utils/ini.py
import configparser

def new_cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(strict=False)
    cfg.optionxform = str  # preserve key case
    return cfg

def read_cfg(path: str) -> configparser.ConfigParser:
    cfg = new_cfg()
    cfg.read(path, encoding="utf-8")
    return cfg

def write_cfg(path: str, cfg: configparser.ConfigParser) -> None:
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)

def resolve_section(cfg: configparser.ConfigParser, section_name: str):
    target = (section_name or "").strip().lower()
    for s in cfg.sections():
        if s.lower() == target:
            return s
    return None

def get_compat(cfg: configparser.ConfigParser, section: str, option: str, fallback=None):
    if section not in cfg:
        return fallback
    sec = cfg[section]
    if option in sec:
        return sec.get(option)
    opt_lower = option.lower()
    for k, v in sec.items():
        if k.lower() == opt_lower:
            return v
    return fallback

def getint_compat(cfg: configparser.ConfigParser, section: str, option: str, fallback=0):
    val = get_compat(cfg, section, option, fallback=None)
    if val is None or val == "":
        return fallback
    try:
        return int(str(val).strip())
    except ValueError:
        try:
            return int(float(str(val).strip()))
        except Exception:
            return fallback
