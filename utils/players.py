# utils/players.py
import json, os, threading
from utils.ini import read_cfg, write_cfg, get_compat

REG_PATH = "data/players.json"
os.makedirs("data", exist_ok=True)
_lock = threading.Lock()

def _coe_path(char_name: str) -> str:
    """Map a display name to the actual .coe filename."""
    return f"{char_name.replace(' ', '_')}.coe"

def _load():
    if not os.path.exists(REG_PATH):
        return {}
    with open(REG_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def _save(data):
    with open(REG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _coe_owner(char_name: str) -> str | None:
    """Return owner_id string from the .coe, or None if missing."""
    path = _coe_path(char_name)
    if not os.path.exists(path):
        return None
    cfg = read_cfg(path)
    owner = get_compat(cfg, "info", "owner_id", fallback="")
    return owner or None

def _set_coe_owner(char_name: str, owner_id: int) -> None:
    path = _coe_path(char_name)
    cfg = read_cfg(path)
    if not cfg.has_section("info"):
        cfg.add_section("info")
    cfg["info"]["owner_id"] = str(owner_id)
    write_cfg(path, cfg)

def list_chars(user_id: int):
    with _lock:
        data = _load()
        return data.get(str(user_id), {}).get("characters", [])

def add_char(user_id: int, char_name: str):
    """Index char under user, but only if the .coe says they own it."""
    owner = _coe_owner(char_name)
    if owner != str(user_id):
        raise PermissionError("You do not own this character.")
    with _lock:
        data = _load()
        u = str(user_id)
        if u not in data:
            data[u] = {"characters": [], "active": None}
        if char_name not in data[u]["characters"]:
            data[u]["characters"].append(char_name)
        if not data[u]["active"]:
            data[u]["active"] = char_name
        _save(data)

def set_active(user_id: int, char_name: str) -> bool:
    if _coe_owner(char_name) != str(user_id):
        return False
    with _lock:
        data = _load()
        u = str(user_id)
        if u not in data or char_name not in data[u].get("characters", []):
            return False
        data[u]["active"] = char_name
        _save(data)
        return True

def get_active(user_id: int) -> str | None:
    with _lock:
        data = _load()
        return data.get(str(user_id), {}).get("active")

def owns_char(user_id: int, char_name: str) -> bool:
    return _coe_owner(char_name) == str(user_id)

def link_existing(user_id: int, char_name: str):
    """Link an existing .coe that is already owned by the caller."""
    if _coe_owner(char_name) != str(user_id):
        raise PermissionError("Character is not owned by you.")
    add_char(user_id, char_name)

def claim_char(user_id: int, char_name: str) -> bool:
    """Claim a .coe that has no owner. Returns True if claim succeeds."""
    if not os.path.exists(_coe_path(char_name)):
        return False
    if _coe_owner(char_name) is not None:
        return False
    _set_coe_owner(char_name, user_id)
    add_char(user_id, char_name)
    return True

def transfer_char(current_owner_id: int, char_name: str, new_owner_id: int) -> bool:
    """Transfer ownership in the .coe, and move index entries."""
    if _coe_owner(char_name) != str(current_owner_id):
        return False
    _set_coe_owner(char_name, new_owner_id)
    with _lock:
        data = _load()
        cur = str(current_owner_id)
        new = str(new_owner_id)
        if cur in data and char_name in data[cur].get("characters", []):
            data[cur]["characters"].remove(char_name)
            if data[cur].get("active") == char_name:
                data[cur]["active"] = data[cur]["characters"][0] if data[cur]["characters"] else None
        if new not in data:
            data[new] = {"characters": [], "active": None}
        if char_name not in data[new]["characters"]:
            data[new]["characters"].append(char_name)
        if not data[new]["active"]:
            data[new]["active"] = char_name
        _save(data)
    return True
    
    

def _norm(name: str) -> str:
    return str(name or "").replace("_", " ").strip().lower()

def remove_char(user_id, char_id):
    """
    Remove `char_id` from the user's registry and clear/shift 'active' if needed.
    Returns (removed: bool, was_active: bool).
    """
    uid = str(user_id)
    with _lock:
        data = _load()
        entry = data.get(uid)
        if not entry:
            return (False, False)
        chars = list(entry.get("characters", []))
        idx = next((i for i, c in enumerate(chars) if _norm(c) == _norm(char_id)), None)
        if idx is None:
            return (False, False)
        removed_char = chars.pop(idx)
        was_active = _norm(entry.get("active", "")) == _norm(removed_char)
        if was_active:
            entry["active"] = chars[0] if chars else ""
        entry["characters"] = chars
        data[uid] = entry
        _save(data)
        return (True, was_active)

def find_owner_by_char(char_id):
    """
    Scan the registry to discover who owns `char_id`. Returns user_id or None.
    """
    with _lock:
        data = _load()
        t = _norm(char_id)
        for uid, entry in data.items():
            for n in entry.get("characters", []):
                if _norm(n) == t:
                    return uid
    return None    

