"""Dialogs for PdaNet Desktop feature parity.

Kept separate from :mod:`pdanet_linux.gui` so the main window stays
readable.  Every long operation runs in a worker thread and reports back
through ``GLib.idle_add``.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import replace
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from . import (  # noqa: E402
    __app_id__,
    bt,
    config,
    netconfig,
    privileges,
    selftest,
    server as pdanet_server,
    wifi,
)


def _background(
    work: Callable[[], object],
    done: Callable[[object], None],
    failed: Callable[[str], None],
) -> None:
    def runner() -> None:
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - shown in the dialog
            GLib.idle_add(failed, str(exc) or exc.__class__.__name__)
            return
        GLib.idle_add(done, result)

    threading.Thread(target=runner, daemon=True).start()


def _dialog_header(title: str, dialog: Adw.Dialog) -> Adw.HeaderBar:
    header = Adw.HeaderBar()
    header.set_title_widget(Adw.WindowTitle(title=title))
    close = Gtk.Button(icon_name="window-close-symbolic")
    close.set_tooltip_text("Close")
    close.connect("clicked", lambda _b: dialog.close())
    header.pack_end(close)
    return header


def _status_label() -> Gtk.Label:
    label = Gtk.Label(xalign=0)
    label.set_wrap(True)
    label.add_css_class("dim-label")
    return label


def _hint(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0)
    label.set_wrap(True)
    label.add_css_class("caption")
    label.add_css_class("dim-label")
    return label


def _app_icon_file() -> str | None:
    """Return an existing app icon file, ignoring stale icon-theme caches.

    GtkIconTheme trusts its cache without checking that the cached files
    still exist.  A source-tree uninstall can therefore leave a cache entry
    that points at a removed /usr/local icon, making has_icon() report true
    and Gtk.Image render the image-missing placeholder.  Verify the file and
    fall back to the known install locations for the current process.
    """
    display = Gdk.Display.get_default()
    if display is not None:
        theme = Gtk.IconTheme.get_for_display(display)
        info = theme.lookup_icon(
            __app_id__,
            None,
            128,
            1,
            Gtk.TextDirection.NONE,
            Gtk.IconLookupFlags(0),
        )
        if info is not None:
            gfile = info.get_file()
            path = gfile.get_path() if gfile is not None else None
            if path and os.path.isfile(path):
                return path

    here = os.path.dirname(os.path.abspath(__file__))
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    candidates = [
        os.path.join(here, os.pardir, "data", f"{__app_id__}.svg"),
        os.path.join(here, os.pardir, "data", f"{__app_id__}.png"),
    ]
    for base in (data_home, *data_dirs.split(":"), "/usr/local/share", "/usr/share"):
        if not base:
            continue
        candidates.append(
            os.path.join(
                base, "icons", "hicolor", "scalable", "apps", f"{__app_id__}.svg"
            )
        )
        candidates.append(
            os.path.join(
                base, "icons", "hicolor", "256x256", "apps", f"{__app_id__}.png"
            )
        )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _welcome_icon() -> Gtk.Image:
    """Return the app icon, with a theme fallback for incomplete installs."""
    path = _app_icon_file()
    image = Gtk.Image.new_from_file(path) if path else Gtk.Image()
    if image.get_paintable() is None:
        image = Gtk.Image.new_from_icon_name("network-vpn-symbolic")
    image.set_pixel_size(84)
    image.set_halign(Gtk.Align.CENTER)
    return image


def _readiness_icon(ok: bool) -> Gtk.Image:
    image = Gtk.Image.new_from_icon_name(
        "object-select-symbolic" if ok else "dialog-warning-symbolic"
    )
    if not ok:
        image.add_css_class("warning")
    return image


class SetupDialog(Adw.Dialog):
    """First-run welcome and default connection setup.

    The dialog is deliberately advisory: a missing adb or helper does not
    block the user from exploring the app.  The phone-side PdaNet+ app must
    still be licensed and running normally.
    """

    _MODES = ("usb", "wifi", "bluetooth")
    _MODE_LABELS = ("USB (recommended)", "Wi-Fi", "Bluetooth")

    def __init__(
        self,
        settings: config.Settings,
        on_save: Callable[[config.Settings], None],
    ) -> None:
        super().__init__()
        self.set_title("Welcome to PdaNet Linux")
        self.set_content_width(620)
        self.set_content_height(700)
        self._settings = settings
        self._on_save = on_save
        self._saved = False
        self.connect("closed", self._on_closed)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(_dialog_header("Welcome", self))
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(580)
        clamp.set_tightening_threshold(480)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(16)
        box.set_margin_end(16)
        clamp.set_child(box)
        scroller.set_child(clamp)
        toolbar.set_content(scroller)
        self.set_child(toolbar)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hero.set_halign(Gtk.Align.CENTER)
        hero.append(_welcome_icon())
        title = Gtk.Label(label="Welcome to PdaNet Linux")
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        title.add_css_class("title-1")
        hero.append(title)
        subtitle = Gtk.Label(
            label="Share your phone's mobile data over USB, Wi-Fi or "
            "Bluetooth. Choose a few defaults now; everything can be changed "
            "later in Settings.",
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=54,
        )
        subtitle.add_css_class("dim-label")
        hero.append(subtitle)
        box.append(hero)

        readiness = Adw.PreferencesGroup(
            title="Readiness check",
            description="Optional checks. The phone remains in charge of "
            "PdaNet registration and licensing.",
        )
        phone_row = Adw.ActionRow(
            title="PdaNet+ on the phone",
            subtitle="Install it, keep it licensed, and enable the same USB, "
            "Wi-Fi or Bluetooth mode you will use here.",
        )
        phone_row.add_prefix(Gtk.Image.new_from_icon_name("phone-symbolic"))
        readiness.add(phone_row)

        adb = shutil.which("adb")
        adb_row = Adw.ActionRow(
            title="USB debugging (USB mode)",
            subtitle=(
                f"adb found at {adb}. Approve the authorization prompt on "
                "the phone."
                if adb
                else "adb was not found. Install android-tools for USB mode; "
                "Wi-Fi and Bluetooth do not need it."
            ),
        )
        adb_row.add_prefix(
            Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
        )
        adb_row.add_suffix(_readiness_icon(bool(adb)))
        readiness.add(adb_row)

        helper = privileges.find_helper()
        helper_row = Adw.ActionRow(
            title="Root helper on this computer",
            subtitle=(
                f"Found at {helper}. It creates the TUN device and routes "
                "when you connect."
                if helper
                else "Not installed yet. Run sudo ./tools/install.sh from "
                "the project folder; the app opens, but it cannot create the "
                "tunnel without the helper."
            ),
        )
        helper_row.add_prefix(
            Gtk.Image.new_from_icon_name("system-lock-screen-symbolic")
        )
        helper_row.add_suffix(_readiness_icon(bool(helper)))
        readiness.add(helper_row)

        defaults = Adw.PreferencesGroup(
            title="Defaults",
            description="These choices are saved for the next launch.",
        )
        self.mode_row = Adw.ComboRow(
            title="Preferred connection",
            subtitle="Used by the Connect button and the tray menu",
        )
        self.mode_row.set_model(Gtk.StringList.new(list(self._MODE_LABELS)))
        self.mode_row.set_selected(
            self._MODES.index(settings.mode) if settings.mode in self._MODES else 0
        )
        defaults.add(self.mode_row)

        self.auto_row = Adw.SwitchRow(
            title="Auto-connect on USB plug-in",
            subtitle="Connect automatically when a new authorized phone appears",
        )
        self.auto_row.set_active(settings.auto_connect_usb)
        defaults.add(self.auto_row)

        self.tray_row = Adw.SwitchRow(
            title="Keep running in the system tray",
            subtitle="Closing the window hides it and keeps the tunnel alive",
        )
        self.tray_row.set_active(settings.close_to_tray)
        defaults.add(self.tray_row)
        box.append(defaults)
        box.append(readiness)

        box.append(
            _hint(
                "You can reopen this assistant from the main menu: "
                "Welcome & Setup. A single authentication prompt is needed "
                "when the helper creates the tunnel; see the README for the "
                "optional passwordless install."
            )
        )

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        footer.set_halign(Gtk.Align.END)
        footer.set_margin_top(12)
        footer.set_margin_bottom(12)
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        skip = Gtk.Button(label="Skip for now")
        skip.connect("clicked", lambda _b: self._complete(False))
        footer.append(skip)
        finish = Gtk.Button(label="Finish setup")
        finish.add_css_class("suggested-action")
        finish.connect("clicked", lambda _b: self._complete(True))
        footer.append(finish)
        toolbar.add_bottom_bar(footer)

    def _selected_mode(self) -> str:
        index = self.mode_row.get_selected()
        if 0 <= index < len(self._MODES):
            return self._MODES[index]
        return "usb"

    def _complete(self, apply_changes: bool) -> None:
        if self._saved:
            return
        self._saved = True
        if apply_changes:
            updated = replace(
                self._settings,
                mode=self._selected_mode(),
                auto_connect_usb=self.auto_row.get_active(),
                close_to_tray=self.tray_row.get_active(),
                setup_complete=True,
            ).validated()
        else:
            updated = replace(
                self._settings, setup_complete=True
            ).validated()
        self._on_save(updated)
        self.close()

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        if not self._saved:
            self._saved = True
            self._on_save(
                replace(self._settings, setup_complete=True).validated()
            )


class SettingsDialog(Adw.PreferencesDialog):
    """Linux equivalent of the desktop Settings dialog.

    Windows Mobile options (serial port, RAS entries, Treo screen) have no
    counterpart here and are listed as not applicable instead of silently
    disappearing.
    """

    def __init__(
        self,
        settings: config.Settings,
        on_save: Callable[[config.Settings], None],
    ) -> None:
        super().__init__()
        self.set_title("Settings")
        self.set_content_width(620)
        self.set_content_height(680)
        self._settings = settings
        self._on_save = on_save

        page = Adw.PreferencesPage()
        self.add(page)

        general = Adw.PreferencesGroup(title="General")
        page.add(general)
        self.auto_row = Adw.SwitchRow(
            title="Auto Connect on USB plug-in",
            subtitle="Connect automatically when a new authorized phone appears",
        )
        self.auto_row.set_active(settings.auto_connect_usb)
        general.add(self.auto_row)
        self.notify_row = Adw.SwitchRow(
            title="Desktop notifications",
            subtitle="Show connected, disconnected and error messages",
        )
        self.notify_row.set_active(settings.show_notifications)
        general.add(self.notify_row)
        self.tray_row = Adw.SwitchRow(
            title="Keep running in the system tray",
            subtitle="Closing the window hides it and keeps the tunnel alive; "
            "use the tray icon to reopen or quit",
        )
        self.tray_row.set_active(settings.close_to_tray)
        general.add(self.tray_row)
        self.log_row = Adw.SwitchRow(
            title="Write diagnostic log",
            subtitle=f"Append connection details to {config.log_path()}",
        )
        self.log_row.set_active(settings.log_to_file)
        general.add(self.log_row)

        options = Adw.PreferencesGroup(
            title="Connection options",
            description="These options are applied to the next connection.",
        )
        page.add(options)
        self.throttle_row = Adw.SwitchRow(
            title="Throttle Connection",
            subtitle="Limit the tunnel speed; useful for phones that drop "
            "the USB link at full speed",
        )
        self.throttle_row.set_active(settings.throttle_kbps > 0)
        options.add(self.throttle_row)
        self.throttle_spin = Adw.SpinRow.new_with_range(64, 100_000, 64)
        self.throttle_spin.set_title("Limit")
        self.throttle_spin.set_subtitle("kbit/s (upload + download)")
        self.throttle_spin.set_value(settings.throttle_kbps or 512)
        self.throttle_spin.set_sensitive(self.throttle_row.get_active())
        self.throttle_row.connect(
            "notify::active",
            lambda row, _pspec: self.throttle_spin.set_sensitive(row.get_active()),
        )
        options.add(self.throttle_spin)
        self.block_row = Adw.SwitchRow(
            title="Block Microsoft Connections",
            subtitle="Block Windows Update/Store/OneDrive names in /etc/hosts "
            "while connected",
        )
        self.block_row.set_active(settings.block_microsoft)
        options.add(self.block_row)

        share = Adw.PreferencesGroup(
            title="WiFi Share (Beta)",
            description="The hotspot is created by NetworkManager with "
            "shared IPv4 (NAT and DNS included).",
        )
        page.add(share)
        self.share_ssid_row = Adw.EntryRow(title="Network name (SSID)")
        self.share_ssid_row.set_text(settings.wifi_share_ssid or "PdaNet Share")
        share.add(self.share_ssid_row)
        self.share_iface_row = Adw.EntryRow(title="Wi-Fi interface")
        self.share_iface_row.set_text(settings.wifi_share_interface)
        self.share_iface_row.set_tooltip_text(
            "Leave empty to use the first Wi-Fi interface"
        )
        share.add(self.share_iface_row)

        legacy = Adw.PreferencesGroup(
            title="Windows-only options (not applicable)",
            description="PdaNet Desktop carries these for Windows Mobile "
            "phones and the Windows RAS stack.",
        )
        page.add(legacy)
        for title, subtitle in (
            ("Maximum Speed (192 Kbps)",
             "Serial-port speed for Palm/Treo; Android uses adb or RFCOMM."),
            ("Allow Bluetooth Connection / Bluetooth Serial Port",
             "Linux discovers the PdaNet RFCOMM service automatically."),
            ("Advanced dial-up / RAS entry",
             "Android tethering has no dial-up entry."),
            ("Turn off Treo screen when connected",
             "Android devices have no equivalent API."),
            ("Windows Hotspot Settings",
             "Use WiFi Share instead; it creates a NetworkManager AP."),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.add_css_class("dim-label")
            legacy.add(row)

        self.connect("closed", self._closed)

    def _closed(self, _dialog: Adw.Dialog) -> None:
        throttle = (
            int(self.throttle_spin.get_value()) if self.throttle_row.get_active() else 0
        )
        current = self._settings
        updated = config.Settings(
            auto_connect_usb=self.auto_row.get_active(),
            show_notifications=self.notify_row.get_active(),
            log_to_file=self.log_row.get_active(),
            close_to_tray=self.tray_row.get_active(),
            throttle_kbps=throttle,
            block_microsoft=self.block_row.get_active(),
            mode=current.mode,
            serial=current.serial,
            bt_device=current.bt_device,
            wifi_host=current.wifi_host,
            wifi_port=current.wifi_port,
            dns=current.dns,
            mtu=current.mtu,
            route_all=current.route_all,
            privilege=current.privilege,
            wifi_share_ssid=self.share_ssid_row.get_text().strip(),
            wifi_share_interface=self.share_iface_row.get_text().strip(),
            setup_complete=current.setup_complete,
        ).validated()
        self._on_save(updated)


class HelpDialog(Adw.Dialog):
    def __init__(self) -> None:
        super().__init__()
        self.set_title("Online Help")
        self.set_content_width(680)
        self.set_content_height(640)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(_dialog_header("Online Help", self))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(box)
        toolbar.set_content(scroller)
        self.set_child(toolbar)

        box.append(
            _hint(
                "This Linux client implements the PdaNet Desktop workflow. "
                "The phone-side PdaNet+ app must be licensed and running in "
                "the matching USB, Wi-Fi or Bluetooth mode."
            )
        )
        for title, body in (
            ("USB",
             "Connect the phone, enable USB debugging and approve the "
             "authorization prompt. If connection fails, run 'adb kill-server' "
             "and make sure another adb.exe/adb process is not holding the device."),
            ("Wi-Fi",
             "Turn on the phone's PdaNet Wi-Fi hotspot, then use "
             "Connect WiFi... -> Scan. PdaNet usually uses 192.168.49.1; "
             "Discover hotspot also works through a UDP probe."),
            ("Bluetooth",
             "Enable Bluetooth tethering in PdaNet, make the phone "
             "discoverable, then use Bluetooth Pairing... to scan, pair and "
             "trust it. Linux discovers the 'PdaNet Server' RFCOMM channel."),
            ("WiFi Share",
             "Turns this computer into a WPA2 hotspot and shares its current "
             "Internet connection (Ethernet, Wi-Fi or PdaNet) with other "
             "devices using NetworkManager shared IPv4. The Wi-Fi adapter "
             "must support AP mode. The receiving device does not need "
             "PdaNet client mode."),
            ("Reverse PdaNet",
             "The opposite direction: this computer creates a hotspot and "
             "runs a PdaNet server, while the phone uses PdaNet+'s 'Connect "
             "to a PdaNet Hotspot' mode. The laptop needs an upstream "
             "connection and an AP-capable Wi-Fi adapter; one radio cannot "
             "also join the same phone's hotspot."),
            ("Phone signal",
             "When the phone is reachable through adb (USB debugging), the "
             "dashboard and tray show its operator, LTE/5G technology, bars "
             "and RSRP/RSRQ/SNR. This telemetry is optional and never affects "
             "the tunnel; some ROMs do not expose it."),
            ("IPv6",
             "PdaNet carries IPv4 only. While connected, IPv6 internet is "
             "blocked so it cannot leak over Wi-Fi. IPv6 LAN traffic (ULA, "
             "link-local, multicast and on-link global prefixes) still uses "
             "the normal route."),
            ("Troubleshooting",
             "Use the phone's VPN rather than a desktop VPN/PPTP client. "
             "5 GHz-only adapters may not see some phone hotspots. "
             "On Windows the equivalent desktop options are USB driver, "
             "hosted network and RAS; those do not exist on Linux and are "
             "replaced by adb, NetworkManager and policy routing."),
            ("Internet Test",
             "Checks the TUN interface, system DNS, UDP DNS through the "
             "tunnel, TCP and HTTPS. ICMP is not forwarded by PdaNet."),
        ):
            heading = Gtk.Label(label=title, xalign=0)
            heading.add_css_class("heading")
            box.append(heading)
            body_label = Gtk.Label(label=body, xalign=0)
            body_label.set_wrap(True)
            body_label.set_selectable(True)
            box.append(body_label)
        link = Gtk.LinkButton.new_with_label(
            "https://www.junefabrics.com/android/faq.php",
            "Official PdaNet FAQ (external)",
        )
        box.append(link)


class BluetoothPairingDialog(Adw.Dialog):
    """Desktop 'Bluetooth Pairing...': scan, pair and trust a phone."""

    def __init__(self, on_paired: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self.set_title("Bluetooth Pairing")
        self.set_content_width(560)
        self.set_content_height(560)
        self._on_paired = on_paired
        self._rows: dict[Gtk.ListBoxRow, bt.BluetoothDevice] = {}
        self._busy = False

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(_dialog_header("Bluetooth Pairing", self))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        toolbar.set_content(box)
        self.set_child(toolbar)

        box.append(
            _hint(
                "Enable Bluetooth in PdaNet+ and make the phone discoverable. "
                "Pairing uses the BlueZ agent; confirm the prompt on the phone."
            )
        )
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.scan_button = Gtk.Button(label="Scan")
        self.scan_button.add_css_class("pill")
        self.scan_button.connect("clicked", lambda _b: self._scan())
        controls.append(self.scan_button)
        self.spinner = Gtk.Spinner()
        controls.append(self.spinner)
        self.status = _status_label()
        self.status.set_hexpand(True)
        controls.append(self.status)
        box.append(controls)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_show_separators(True)
        self.listbox.connect("row-selected", self._row_selected)
        scroller.set_child(self.listbox)
        box.append(scroller)

        self.pair_button = Gtk.Button(label="Pair and trust")
        self.pair_button.add_css_class("suggested-action")
        self.pair_button.add_css_class("pill")
        self.pair_button.set_sensitive(False)
        self.pair_button.connect("clicked", lambda _b: self._pair())
        box.append(self.pair_button)

        GLib.idle_add(self._scan)

    def _scan(self) -> bool:
        if self._busy:
            return False
        self._busy = True
        self.scan_button.set_sensitive(False)
        self.pair_button.set_sensitive(False)
        self.spinner.start()
        self.status.set_text("Scanning for nearby Bluetooth devices...")
        _background(
            lambda: bt.scan_devices(
                duration=8.0,
                log=lambda message: GLib.idle_add(self.status.set_text, message),
            ),
            self._scan_done,
            self._failed,
        )
        return False

    def _scan_done(self, result: object) -> None:
        devices = list(result) if result else []
        self._busy = False
        self.scan_button.set_sensitive(True)
        self.spinner.stop()
        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)
        self._rows.clear()
        for device in devices:
            row = Adw.ActionRow()
            row.set_title(device.label)
            state = "Paired" if device.paired else "Not paired"
            if device.trusted:
                state += ", trusted"
            if device.connected:
                state += ", connected"
            row.set_subtitle(state)
            row.set_icon_name(
                "bluetooth-active-symbolic" if device.connected
                else "bluetooth-symbolic"
            )
            self.listbox.append(row)
            self._rows[row] = device
        self.status.set_text(
            f"{len(devices)} device(s) found; select the phone and pair."
            if devices
            else "No device found. Make the phone discoverable and scan again."
        )

    def _row_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        device = self._rows.get(row) if row is not None else None
        self.pair_button.set_sensitive(device is not None and not self._busy)
        if device is not None and device.paired:
            self.pair_button.set_label("Trust / pair again")
        else:
            self.pair_button.set_label("Pair and trust")

    def _pair(self) -> None:
        row = self.listbox.get_selected_row()
        device = self._rows.get(row) if row is not None else None
        if device is None or self._busy:
            return
        self._busy = True
        self.scan_button.set_sensitive(False)
        self.pair_button.set_sensitive(False)
        self.spinner.start()
        self.status.set_text(f"Pairing {device.label}...")
        _background(
            lambda: bt.pair_device(
                device.address,
                timeout=45.0,
                log=lambda message: GLib.idle_add(self.status.set_text, message),
            ),
            self._paired,
            self._failed,
        )

    def _paired(self, result: object) -> None:
        device = result
        self._busy = False
        self.spinner.stop()
        self.scan_button.set_sensitive(True)
        self.pair_button.set_sensitive(True)
        self.status.set_text(f"Paired and trusted: {device.label}")
        if self._on_paired is not None:
            self._on_paired(device.address)

    def _failed(self, message: str) -> None:
        self._busy = False
        self.spinner.stop()
        self.scan_button.set_sensitive(True)
        self.pair_button.set_sensitive(True)
        self.status.set_text(message)
        self.status.add_css_class("error")


class WifiPairingDialog(Adw.Dialog):
    """Desktop 'WiFi Pairing...': join a hotspot through NetworkManager."""

    def __init__(
        self, on_connected: Callable[[str | None], None] | None = None
    ) -> None:
        super().__init__()
        self.set_title("WiFi Pairing")
        self.set_content_width(600)
        self.set_content_height(600)
        self._on_connected = on_connected
        self._rows: dict[Gtk.ListBoxRow, wifi.WifiNetwork] = {}
        self._selected: wifi.WifiNetwork | None = None
        self._active_network: wifi.WifiNetwork | None = None
        self._interfaces: list[wifi.WifiInterface] = []
        self._busy = False

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(_dialog_header("WiFi Pairing", self))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        toolbar.set_content(box)
        self.set_child(toolbar)

        box.append(
            _hint(
                "Join the phone's PdaNet hotspot (or any Wi-Fi network). "
                "NetworkManager remembers the profile for later connections."
            )
        )
        self.interface_row = Adw.ComboRow(title="Wi-Fi interface")
        self.interface_row.connect("notify::selected", lambda *_: self._scan())
        box.append(self.interface_row)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.scan_button = Gtk.Button(label="Scan")
        self.scan_button.add_css_class("pill")
        self.scan_button.connect("clicked", lambda _b: self._scan())
        controls.append(self.scan_button)
        self.spinner = Gtk.Spinner()
        controls.append(self.spinner)
        self.status = _status_label()
        self.status.set_hexpand(True)
        controls.append(self.status)
        box.append(controls)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_show_separators(True)
        self.listbox.connect("row-selected", self._row_selected)
        scroller.set_child(self.listbox)
        box.append(scroller)

        self.password_row = Adw.PasswordEntryRow(title="Password")
        self.password_row.set_show_apply_button(False)
        box.append(self.password_row)

        self.connect_button = Gtk.Button(label="Connect")
        self.connect_button.add_css_class("suggested-action")
        self.connect_button.add_css_class("pill")
        self.connect_button.set_sensitive(False)
        self.connect_button.connect("clicked", lambda _b: self._connect())
        box.append(self.connect_button)

        GLib.idle_add(self._load_interfaces)

    def _load_interfaces(self) -> bool:
        try:
            self._interfaces = wifi.interfaces()
        except wifi.WifiError as exc:
            self.status.set_text(str(exc))
            return False
        if not self._interfaces:
            self.status.set_text("No Wi-Fi interface found.")
            return False
        self.interface_row.set_model(
            Gtk.StringList.new([item.name for item in self._interfaces])
        )
        self.interface_row.set_selected(0)
        self._scan()
        return False

    def _selected_interface(self) -> str | None:
        if not self._interfaces:
            return None
        index = min(self.interface_row.get_selected(), len(self._interfaces) - 1)
        return self._interfaces[index].name

    def _scan(self) -> bool:
        if self._busy:
            return False
        interface = self._selected_interface()
        if interface is None:
            return False
        self._busy = True
        self.scan_button.set_sensitive(False)
        self.connect_button.set_sensitive(False)
        self.spinner.start()
        self.status.set_text("Scanning for Wi-Fi networks...")
        _background(
            lambda: wifi.scan(interface, rescan=True),
            self._scan_done,
            self._failed,
        )
        return False

    def _scan_done(self, result: object) -> None:
        networks = list(result) if result else []
        self._busy = False
        self.scan_button.set_sensitive(True)
        self.spinner.stop()
        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)
        self._rows.clear()
        for network in networks:
            row = Adw.ActionRow(title=network.ssid, subtitle=network.label)
            row.set_icon_name(
                "network-wireless-signal-excellent-symbolic"
                if network.signal >= 60
                else "network-wireless-signal-ok-symbolic"
            )
            self.listbox.append(row)
            self._rows[row] = network
        self.status.set_text(
            f"{len(networks)} network(s) found; select the PdaNet hotspot."
            if networks
            else "No network found. Move closer and scan again."
        )

    def _row_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        network = self._rows.get(row) if row is not None else None
        self._selected = network
        self.connect_button.set_sensitive(network is not None and not self._busy)
        if network is not None:
            secured = network.security not in ("", "--")
            self.password_row.set_sensitive(secured)
            self.status.set_text(
                f"Selected {network.ssid}"
                + (" (password required)" if secured else " (open network)")
            )

    def _connect(self) -> None:
        network = self._selected
        interface = self._selected_interface()
        if network is None or interface is None or self._busy:
            return
        password = self.password_row.get_text() or None
        if network.security not in ("", "--") and not password:
            self._failed("Enter the Wi-Fi password first.")
            return
        self._busy = True
        self.scan_button.set_sensitive(False)
        self.connect_button.set_sensitive(False)
        self.spinner.start()
        self.status.set_text(f"Connecting to {network.ssid}...")
        _background(
            lambda: (
                wifi.connect(network.ssid, password, interface=interface),
                self._gateway(interface),
            ),
            self._connected,
            self._failed,
        )
        self._active_network = network

    @staticmethod
    def _gateway(interface: str) -> str | None:
        try:
            return wifi.gateway(interface)
        except wifi.WifiError:
            return None

    def _connected(self, result: object) -> None:
        _output, address = result
        self._busy = False
        self.spinner.stop()
        name = self._active_network.ssid if self._active_network else "the network"
        self.status.set_text(
            f"Connected to {name}." + (f" Gateway: {address}" if address else "")
        )
        if self._on_connected is not None:
            self._on_connected(address)
        GLib.timeout_add(900, self.close)

    def _failed(self, message: str) -> None:
        self._busy = False
        self.spinner.stop()
        self.scan_button.set_sensitive(True)
        self.connect_button.set_sensitive(True)
        self.status.set_text(message)
        self.status.add_css_class("error")


class WifiShareDialog(Adw.Dialog):
    """Desktop 'WiFi Share (Beta)...' using a NetworkManager access point."""

    def __init__(
        self,
        settings: config.Settings,
        on_state: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__()
        self.set_title("WiFi Share")
        self.set_content_width(580)
        self.set_content_height(540)
        self._settings = settings
        self._on_state = on_state
        self._active = False
        self._busy = False
        self._interfaces: list[wifi.WifiInterface] = []

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(_dialog_header("WiFi Share (Beta)", self))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        toolbar.set_content(box)
        self.set_child(toolbar)

        box.append(
            _hint(
                "Create a WPA2 hotspot on this computer and share its "
                "Internet connection (Ethernet, Wi-Fi or PdaNet) with phones "
                "and tablets. NetworkManager provides NAT and DNS; the Wi-Fi "
                "adapter must support AP mode. For a phone that should use "
                "PdaNet's client mode, choose Reverse PdaNet instead."
            )
        )
        self.warning = _hint("")
        self.warning.add_css_class("warning")
        box.append(self.warning)

        self.ssid_row = Adw.EntryRow(title="Network name (SSID)")
        self.ssid_row.set_text(settings.wifi_share_ssid or "PdaNet Share")
        box.append(self.ssid_row)
        self.password_row = Adw.PasswordEntryRow(title="Password (8-63 characters)")
        self.password_row.set_show_apply_button(False)
        box.append(self.password_row)
        self.interface_row = Adw.ComboRow(title="Wi-Fi interface")
        box.append(self.interface_row)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.action_button = Gtk.Button(label="Activate")
        self.action_button.add_css_class("suggested-action")
        self.action_button.add_css_class("pill")
        self.action_button.connect("clicked", lambda _b: self._toggle())
        controls.append(self.action_button)
        self.spinner = Gtk.Spinner()
        controls.append(self.spinner)
        self.status = _status_label()
        self.status.set_hexpand(True)
        controls.append(self.status)
        box.append(controls)

        self.details = Gtk.Label(xalign=0)
        self.details.set_wrap(True)
        self.details.add_css_class("caption")
        self.details.add_css_class("dim-label")
        box.append(self.details)

        GLib.idle_add(self._load)

    def _load(self) -> bool:
        try:
            self._interfaces = wifi.interfaces()
            active = wifi.share_active()
        except wifi.WifiError as exc:
            self._failed(str(exc))
            return False
        if self._interfaces:
            names = [item.name for item in self._interfaces]
            self.interface_row.set_model(Gtk.StringList.new(names))
            selected = 0
            if self._settings.wifi_share_interface in names:
                selected = names.index(self._settings.wifi_share_interface)
            self.interface_row.set_selected(selected)
        else:
            self.interface_row.set_subtitle("No Wi-Fi interface found")
            self.action_button.set_sensitive(False)
        self._set_active(active)
        try:
            selftest.check_tunnel()
            self.warning.set_text("")
        except Exception:  # noqa: BLE001 - advisory only
            self.warning.set_text(
                "No PdaNet tunnel is up. The hotspot can still share any "
                "other Internet connection on this computer; connect PdaNet "
                "first if the computer has no Internet of its own."
            )
        return False

    def _selected_interface(self) -> str | None:
        if not self._interfaces:
            return None
        index = min(self.interface_row.get_selected(), len(self._interfaces) - 1)
        return self._interfaces[index].name

    def _set_active(self, active: bool) -> None:
        changed = active != self._active
        self._active = active
        self.ssid_row.set_sensitive(not active)
        self.password_row.set_sensitive(not active)
        self.interface_row.set_sensitive(not active)
        if active:
            self.action_button.set_label("Deactivate")
            self.action_button.remove_css_class("suggested-action")
            self.action_button.add_css_class("destructive-action")
            self.status.set_text("Hotspot is active")
            self.details.set_text(
                "Clients can now join the hotspot. They use this computer's "
                "current Internet connection; if PdaNet is connected, the "
                "traffic follows the PdaNet tunnel."
            )
        else:
            self.action_button.set_label("Activate")
            self.action_button.remove_css_class("destructive-action")
            self.action_button.add_css_class("suggested-action")
            self.status.set_text("Hotspot is not active")
            self.details.set_text("")
        if changed and self._on_state is not None:
            self._on_state(active)

    def _toggle(self) -> None:
        if self._busy:
            return
        if self._active:
            self._run(lambda: wifi.share_stop(), self._stopped)
            return
        ssid = self.ssid_row.get_text().strip()
        password = self.password_row.get_text()
        interface = self._selected_interface()
        if not ssid:
            self._failed("Enter a network name (SSID).")
            return
        if not 8 <= len(password) <= 63:
            self._failed("The WPA2 password must be 8 to 63 characters.")
            return
        if interface is None:
            self._failed("No Wi-Fi interface is available.")
            return
        self.status.set_text("Activating hotspot...")
        self._run(
            lambda: wifi.share_start(ssid, password, interface=interface),
            self._started,
        )

    def _run(
        self, work: Callable[[], object], done: Callable[[object], None]
    ) -> None:
        self._busy = True
        self.action_button.set_sensitive(False)
        self.spinner.start()
        _background(work, done, self._failed)

    def _started(self, result: object) -> None:
        self._busy = False
        self.spinner.stop()
        self.action_button.set_sensitive(True)
        self._set_active(True)
        self.status.set_text(
            f"Hotspot active: {self.ssid_row.get_text().strip()}"
        )

    def _stopped(self, _result: object) -> None:
        self._busy = False
        self.spinner.stop()
        self.action_button.set_sensitive(True)
        self._set_active(False)
        self.status.set_text("WiFi Share stopped")

    def _failed(self, message: str) -> None:
        self._busy = False
        self.spinner.stop()
        self.action_button.set_sensitive(True)
        self.status.set_text(message)
        self.status.add_css_class("error")


def _wait_for_share_info(timeout: float = 20.0) -> wifi.ShareInfo:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            info = wifi.share_info()
        except wifi.WifiError as exc:
            last = exc
            info = None
        if info is not None:
            return info
        time.sleep(0.5)
    detail = f": {last}" if last else ""
    raise wifi.WifiError(
        "the hotspot was activated but did not get an IPv4 address" + detail
    )


class ReverseTetherDialog(Adw.Dialog):
    """Let a phone use this computer's Internet through PdaNet client mode.

    The dialog owns the hotspot, TUN and server for its lifetime.  Closing it
    while active stops them, so a stray PdaNet server can never keep running
    without a visible control.
    """

    def __init__(
        self,
        settings: config.Settings,
        on_state: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__()
        self.set_title("Reverse PdaNet")
        self.set_content_width(620)
        self.set_content_height(620)
        self._settings = settings
        self._on_state = on_state
        self._active = False
        self._busy = False
        self._interfaces: list[wifi.WifiInterface] = []
        self._tun = None
        self._server: pdanet_server.PdaNetServer | None = None
        self._started_hotspot = False
        self._password = ""
        self._ssid = ""
        self._info: wifi.ShareInfo | None = None
        self._stop_lock = threading.Lock()
        self._cleaned = True
        self._closed = False
        self.connect("closed", self._on_closed)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(_dialog_header("Reverse PdaNet", self))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        toolbar.set_content(box)
        self.set_child(toolbar)

        box.append(
            _hint(
                "Reverse PdaNet lets a phone use this computer's Internet. "
                "The phone joins the laptop hotspot, then chooses "
                "\"Connect to a PdaNet Hotspot\" in PdaNet+. This is the "
                "opposite direction of the normal PdaNet connection."
            )
        )
        box.append(
            _hint(
                "For a phone or tablet that only needs ordinary Wi-Fi, use "
                "WiFi Share instead; it needs no PdaNet client mode. Reverse "
                "PdaNet needs its own upstream: a single Wi-Fi adapter "
                "cannot also be connected to the phone's hotspot."
            )
        )

        self.existing = _hint("")
        box.append(self.existing)

        self.ssid_row = Adw.EntryRow(title="Hotspot name (SSID)")
        self.ssid_row.set_text(
            settings.wifi_share_ssid or "PdaNet Laptop"
        )
        box.append(self.ssid_row)
        self.password_row = Adw.PasswordEntryRow(
            title="Hotspot password (leave blank to generate)"
        )
        self.password_row.set_show_apply_button(False)
        box.append(self.password_row)
        self.interface_row = Adw.ComboRow(title="Wi-Fi interface")
        box.append(self.interface_row)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.action_button = Gtk.Button(label="Start Reverse PdaNet")
        self.action_button.add_css_class("suggested-action")
        self.action_button.add_css_class("pill")
        self.action_button.connect("clicked", lambda _b: self._toggle())
        controls.append(self.action_button)
        self.spinner = Gtk.Spinner()
        controls.append(self.spinner)
        self.status = _status_label()
        self.status.set_hexpand(True)
        controls.append(self.status)
        box.append(controls)

        self.details = Gtk.Label(xalign=0)
        self.details.set_wrap(True)
        self.details.set_selectable(True)
        self.details.add_css_class("caption")
        self.details.add_css_class("dim-label")
        box.append(self.details)

        copy = Gtk.Button(label="Copy instructions")
        copy.set_halign(Gtk.Align.START)
        copy.connect("clicked", lambda _b: self._copy())
        box.append(copy)
        box.append(
            _hint(
                "Closing this dialog stops the reverse server and the hotspot "
                "it created. The phone's PdaNet license is still required."
            )
        )
        self._copy_button = copy

        GLib.idle_add(self._load)

    def _load(self) -> bool:
        try:
            self._interfaces = wifi.interfaces()
            info = wifi.share_info()
        except wifi.WifiError as exc:
            self._failed(str(exc))
            return False
        if self._interfaces:
            names = [item.name for item in self._interfaces]
            self.interface_row.set_model(Gtk.StringList.new(names))
            selected = 0
            if self._settings.wifi_share_interface in names:
                selected = names.index(self._settings.wifi_share_interface)
            self.interface_row.set_selected(selected)
        else:
            self.interface_row.set_subtitle("No Wi-Fi interface found")
            self.action_button.set_sensitive(False)
        if info is not None:
            self.existing.set_text(
                f"An existing hotspot is active on {info.interface} "
                f"({info.subnet}). It will be reused."
            )
            self.ssid_row.set_sensitive(False)
            self.password_row.set_sensitive(False)
            self.interface_row.set_sensitive(False)
        return False

    def _selected_interface(self) -> str | None:
        if not self._interfaces:
            return None
        index = min(
            self.interface_row.get_selected(), len(self._interfaces) - 1
        )
        return self._interfaces[index].name

    def _toggle(self) -> None:
        if self._busy:
            return
        if self._active:
            self._begin_stop()
            return
        ssid = self.ssid_row.get_text().strip()
        password = self.password_row.get_text()
        interface = self._selected_interface()
        if not ssid:
            self._failed("Enter a network name (SSID).")
            return
        if password and not 8 <= len(password) <= 63:
            self._failed("The WPA2 password must be 8 to 63 characters.")
            return
        if interface is None and wifi.share_info() is None:
            self._failed("No Wi-Fi interface is available.")
            return
        self._busy = True
        self.action_button.set_sensitive(False)
        self.spinner.start()
        self.status.set_text("Starting hotspot and reverse server...")
        self.status.remove_css_class("error")
        self.details.set_text("")
        _background(
            lambda: self._start_work(ssid, password, interface),
            self._started,
            self._failed,
        )

    def _start_work(self, ssid: str, password: str, interface: str | None):
        info = wifi.share_info()
        started_hotspot = False
        generated = ""
        if info is None:
            generated = password or secrets.token_urlsafe(9)
            if not 8 <= len(generated) <= 63:
                raise wifi.WifiError(
                    "the WPA2 password must be 8 to 63 characters"
                )
            wifi.share_start(ssid, generated, interface=interface)
            started_hotspot = True
        try:
            if info is None:
                info = _wait_for_share_info()
            tun = privileges.open_tun(
                netconfig.make_config(
                    role=netconfig.ROLE_SERVER,
                    mtu=self._settings.mtu,
                    server_nat=True,
                ),
                privilege=self._settings.privilege,
            )
            try:
                srv = pdanet_server.PdaNetServer(
                    tun.fd,
                    tun_name=tun.name,
                    server_ip=info.address,
                    mac=info.mac,
                    name=socket.gethostname() or "PdaNet Linux",
                    allowed_networks=[info.network, "127.0.0.0/8"],
                )
                srv.start()
            except Exception:
                tun.close()
                raise
            return tun, srv, info, generated, started_hotspot
        except Exception:
            if started_hotspot:
                try:
                    wifi.share_stop()
                except wifi.WifiError:
                    pass
            raise

    def _started(self, result: object) -> None:
        tun, srv, info, password, started_hotspot = result  # type: ignore[misc]
        self._tun = tun
        self._server = srv
        self._info = info
        self._password = password
        self._started_hotspot = started_hotspot
        self._ssid = self.ssid_row.get_text().strip() if started_hotspot else ""
        self._cleaned = False
        self._busy = False
        self._active = True
        self.spinner.stop()
        self.action_button.set_sensitive(True)
        self.action_button.set_label("Stop Reverse PdaNet")
        self.action_button.remove_css_class("suggested-action")
        self.action_button.add_css_class("destructive-action")
        self.ssid_row.set_sensitive(False)
        self.password_row.set_sensitive(False)
        self.interface_row.set_sensitive(False)
        self.status.set_text("Reverse PdaNet server is running")
        self.status.remove_css_class("error")
        self._show_instructions()
        if self._closed:
            # The dialog was closed while the pkexec prompt or hotspot was
            # still starting; stop immediately instead of leaving an
            # invisible server behind.
            self._begin_stop()
            return
        if self._on_state is not None:
            self._on_state(True)

    def _show_instructions(self) -> None:
        info = self._info
        if info is None:
            return
        if self._started_hotspot:
            hotspot = f"{self._ssid} (password: {self._password})"
        else:
            hotspot = f"existing hotspot on {info.interface}"
        self.details.set_text(
            f"Hotspot: {hotspot}\n"
            f"Laptop: {info.address} on {info.interface}\n"
            f"Server: TCP 8740, UDP 8000, TUN 10.1.19.1/24\n\n"
            "On the phone:\n"
            "1. Settings > Wi-Fi > connect to the hotspot above.\n"
            "2. Open PdaNet+ and tap \"Connect to a PdaNet Hotspot\".\n"
            "3. Approve the VPN request. Internet traffic now goes through "
            "this computer."
        )

    def _begin_stop(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.action_button.set_sensitive(False)
        self.spinner.start()
        self.status.set_text("Stopping reverse server...")
        _background(self._stop_work, self._stopped, self._failed)

    def _stop_work(self) -> bool:
        with self._stop_lock:
            if self._cleaned:
                return False
            self._cleaned = True
        srv = self._server
        tun = self._tun
        started = self._started_hotspot
        self._server = None
        self._tun = None
        if srv is not None:
            srv.stop()
        if tun is not None:
            tun.close()
        if started:
            try:
                wifi.share_stop()
            except wifi.WifiError:
                pass
        return True

    def _stopped(self, _result: object) -> None:
        self._busy = False
        self._active = False
        self.spinner.stop()
        self.action_button.set_sensitive(True)
        self.action_button.set_label("Start Reverse PdaNet")
        self.action_button.remove_css_class("destructive-action")
        self.action_button.add_css_class("suggested-action")
        self.ssid_row.set_sensitive(True)
        self.password_row.set_sensitive(True)
        self.interface_row.set_sensitive(True)
        self.status.set_text("Reverse PdaNet server stopped")
        self.details.set_text("")
        if self._on_state is not None:
            self._on_state(False)

    def _failed(self, message: str) -> None:
        self._busy = False
        self.spinner.stop()
        self.action_button.set_sensitive(True)
        self.status.set_text(message)
        self.status.add_css_class("error")
        if self._active:
            # The server may still be running; leave the controls usable.
            pass

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self._closed = True
        if self._active and not self._busy:
            # No UI is available anymore; stop quietly in the background.
            threading.Thread(
                target=self._stop_work, name="pdanet-reverse-close", daemon=True
            ).start()
            if self._on_state is not None:
                self._on_state(False)
        # If a start is still in progress, _started() sees _closed and stops
        # the server as soon as it has been created.

    def _copy(self) -> None:
        text = self.details.get_text()
        if not text:
            text = self.status.get_text()
        self.get_clipboard().set(text)
        self.status.set_text("Instructions copied to the clipboard")



class InternetTestDialog(Adw.Dialog):
    """Desktop Internet Test with copyable results."""

    def __init__(self) -> None:
        super().__init__()
        self.set_title("Internet Test")
        self.set_content_width(640)
        self.set_content_height(480)
        self._report = ""
        self._busy = False

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(_dialog_header("Internet Test", self))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        toolbar.set_content(box)
        self.set_child(toolbar)

        box.append(
            _hint(
                "Checks the tunnel interface, DNS, UDP DNS through the tunnel, "
                "TCP and HTTPS. ICMP is not forwarded by PdaNet."
            )
        )
        self.status = _status_label()
        box.append(self.status)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_monospace(True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.buffer = self.view.get_buffer()
        scroller.set_child(self.view)
        box.append(scroller)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.run_button = Gtk.Button(label="Run test")
        self.run_button.add_css_class("suggested-action")
        self.run_button.connect("clicked", lambda _b: self._run())
        controls.append(self.run_button)
        self.copy_button = Gtk.Button(label="Copy result")
        self.copy_button.set_sensitive(False)
        self.copy_button.connect("clicked", lambda _b: self._copy())
        controls.append(self.copy_button)
        self.spinner = Gtk.Spinner()
        controls.append(self.spinner)
        box.append(controls)

        GLib.idle_add(self._run)

    def _run(self) -> bool:
        if self._busy:
            return False
        self._busy = True
        self.run_button.set_sensitive(False)
        self.copy_button.set_sensitive(False)
        self.spinner.start()
        self.status.set_text("Running Internet Test...")
        self.buffer.set_text("")
        _background(selftest.run_checks, self._done, self._failed)
        return False

    def _done(self, result: object) -> None:
        results = list(result) if result else []
        report = selftest.format_report(results)
        ok = selftest.all_ok(results)
        report += "\n\n" + ("All checks passed." if ok else "Some checks failed.")
        self._busy = False
        self.spinner.stop()
        self.run_button.set_sensitive(True)
        self.copy_button.set_sensitive(True)
        self.status.set_text("Internet Test passed" if ok else "Internet Test failed")
        self.buffer.set_text(report)
        self._report = report

    def _failed(self, message: str) -> None:
        self._busy = False
        self.spinner.stop()
        self.run_button.set_sensitive(True)
        self.status.set_text(message)
        self.status.add_css_class("error")

    def _copy(self) -> None:
        if not self._report:
            return
        self.get_clipboard().set(self._report)
        self.status.set_text("Result copied to the clipboard")
