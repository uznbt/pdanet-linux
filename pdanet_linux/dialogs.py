"""Dialogs for PdaNet Desktop feature parity.

Kept separate from :mod:`pdanet_linux.gui` so the main window stays
readable.  Every long operation runs in a worker thread and reports back
through ``GLib.idle_add``.
"""

from __future__ import annotations

import os
import subprocess
import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import bt, config, selftest, wifi  # noqa: E402


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
             "Turns this computer into a WPA2 hotspot and shares the PdaNet "
             "tunnel with other devices (NetworkManager shared IPv4). The "
             "Wi-Fi adapter must support AP mode."),
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
                "Create a WPA2 hotspot on this computer and share the PdaNet "
                "tunnel with other computers and tablets. NetworkManager "
                "provides NAT and DNS; the Wi-Fi adapter must support AP mode."
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
                "No pdanet* tunnel is up. The hotspot can be created, but "
                "clients will not reach the Internet until PdaNet connects."
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
                "Clients can now join the hotspot. Traffic is shared through "
                "the PdaNet tunnel while it is connected."
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
