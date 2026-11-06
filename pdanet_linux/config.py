"""Small JSON settings store for the GUI.

The Windows PdaNet desktop keeps its options in the registry.  The Linux
client uses a single JSON file under ``$XDG_CONFIG_HOME`` instead, so the
GUI and future tray/automation front-ends share the same state.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

APP_DIR = "pdanet-linux"
CONFIG_NAME = "config.json"

VALID_MODES = ("usb", "wifi", "bluetooth")
VALID_PRIVILEGES = ("auto", "sudo", "pkexec")


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / APP_DIR / CONFIG_NAME


def log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(base) / APP_DIR / "pdanet.log"


def append_log(text: str) -> None:
    """Append one diagnostic line to the optional log file."""
    target = log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
    )
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(text.rstrip("\n") + "\n")


@dataclass
class Settings:
    auto_connect_usb: bool = False
    show_notifications: bool = True
    log_to_file: bool = False
    close_to_tray: bool = True
    throttle_kbps: int = 0
    block_microsoft: bool = False
    mode: str = "usb"
    serial: str = ""
    bt_device: str = ""
    wifi_host: str = "192.168.49.1"
    wifi_port: int = 8740
    dns: str = "8.8.8.8"
    mtu: int = 1500
    route_all: bool = True
    privilege: str = "auto"
    wifi_share_ssid: str = "PdaNet Share"
    wifi_share_interface: str = ""

    def validated(self) -> "Settings":
        """Return a copy with out-of-range values replaced by defaults."""
        defaults = Settings()
        data = asdict(self)
        if data["mode"] not in VALID_MODES:
            data["mode"] = defaults.mode
        if data["privilege"] not in VALID_PRIVILEGES:
            data["privilege"] = defaults.privilege
        try:
            data["throttle_kbps"] = int(data["throttle_kbps"])
        except (TypeError, ValueError):
            data["throttle_kbps"] = 0
        data["throttle_kbps"] = max(0, min(data["throttle_kbps"], 100_000))
        try:
            data["wifi_port"] = int(data["wifi_port"])
        except (TypeError, ValueError):
            data["wifi_port"] = defaults.wifi_port
        if not 1 <= data["wifi_port"] <= 65535:
            data["wifi_port"] = defaults.wifi_port
        try:
            data["mtu"] = int(data["mtu"])
        except (TypeError, ValueError):
            data["mtu"] = defaults.mtu
        if not 576 <= data["mtu"] <= 9000:
            data["mtu"] = defaults.mtu
        for key in (
            "serial",
            "bt_device",
            "wifi_host",
            "dns",
            "wifi_share_ssid",
            "wifi_share_interface",
        ):
            value = data.get(key)
            data[key] = value.strip() if isinstance(value, str) else ""
        return Settings(**data)


def load(path: Path | None = None) -> Settings:
    """Load settings, falling back to defaults on a missing/broken file."""
    target = path or config_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    known = {field: raw[field] for field in Settings.__dataclass_fields__ if field in raw}
    try:
        return Settings(**known).validated()
    except TypeError:
        return Settings()


def save(settings: Settings, path: Path | None = None) -> None:
    """Atomically write settings, creating the config directory if needed."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(settings.validated())
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config-", suffix=".json", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
