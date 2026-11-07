"""GTK4 / libadwaita front-end for the unofficial PdaNet Linux client."""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import (
    __app_id__,
    __version__,
    autoconnect,
    bt,
    cellular,
    config,
    dialogs,
    transport,
    tray,
    wifi,
)
from .connection import ConnectOptions, PdaNetConnection  # noqa: E402
from .pump import format_bytes, format_duration  # noqa: E402

TRAY_SHOW = 1
TRAY_CONNECT_USB = 10
TRAY_CONNECT_WIFI = 11
TRAY_CONNECT_BLUETOOTH = 12
TRAY_DISCONNECT = 13
TRAY_SIGNAL = 14
TRAY_WIFI_SHARE = 20
TRAY_REVERSE_TETHER = 25
TRAY_SETTINGS = 21
TRAY_BT_PAIRING = 22
TRAY_WIFI_PAIRING = 23
TRAY_INTERNET_TEST = 24
TRAY_AUTO_CONNECT = 30
TRAY_BLOCK_MICROSOFT = 31
TRAY_THROTTLE = 32
TRAY_QUIT = 40

_WINDOW_CSS = b"""
.pdanet-card {
    background-color: @card_bg_color;
    border: 1px solid @card_shade_color;
    border-radius: 12px;
}
.pdanet-status-title {
    font-size: 19pt;
    font-weight: 800;
}
.pdanet-metric-value {
    font-size: 13pt;
    font-weight: 700;
}
.pdanet-section-title {
    font-size: 11pt;
    font-weight: 700;
}
.pdanet-sidebar {
    background-color: @sidebar_bg_color;
    border-right: 1px solid @sidebar_shade_color;
}
.pdanet-log, .pdanet-log text {
    background-color: transparent;
}
"""


def _hostname() -> str:
    try:
        return socket.gethostname() or "Linux"
    except OSError:
        return "Linux"


class PdaNetWindow(Adw.ApplicationWindow):
    def __init__(self, app: "PdaNetApp") -> None:
        super().__init__(application=app, title="PdaNet Linux")
        self.set_default_size(1040, 700)
        self.set_size_request(640, 560)
        self.conn: PdaNetConnection | None = None
        self.worker: threading.Thread | None = None
        self.devices: list[transport.AdbDevice] = []
        self.bt_devices: list[bt.BluetoothDevice] = []
        self.settings = config.load()
        self.autoconnect = autoconnect.UsbAutoConnect(self.settings.auto_connect_usb)
        self._closing = False
        self._quitting = False
        self._cleaned_up = False
        self._pending_mode: str | None = None
        self._usb_poll_busy = False
        self._autoconnect_primed = False
        self._share_started = False
        self._target_bt: str | None = None
        self._signal: cellular.CellularSignal | None = None
        self._signal_busy = False
        self._signal_error = False
        self._signal_generation = 0
        self._active_serial: str | None = None
        self.connect("close-request", self._on_close_request)

        self._build_ui()
        self._apply_settings()
        self._refresh_devices()
        GLib.timeout_add_seconds(4, self._poll_usb)
        GLib.timeout_add_seconds(15, self._poll_signal)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._install_css()
        self._install_actions()
        self.header = Adw.HeaderBar()
        title = Adw.WindowTitle(
            title="PdaNet Linux",
            subtitle="USB / Wi-Fi / Bluetooth tethering",
        )
        self.header.set_title_widget(title)

        self.sidebar_button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic")
        self.sidebar_button.set_tooltip_text("Show or hide connection setup")
        self.sidebar_button.set_active(True)
        self.header.pack_start(self.sidebar_button)

        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        self.refresh_button.set_tooltip_text("Refresh device list")
        self.refresh_button.connect("clicked", lambda _b: self._refresh_devices())
        self.header.pack_start(self.refresh_button)

        self.menu_button = Gtk.MenuButton()
        self.menu_button.set_icon_name("open-menu-symbolic")
        self.menu_button.set_tooltip_text("Main menu")
        self.menu_button.set_menu_model(self._build_menu())
        self.header.pack_end(self.menu_button)

        self.toast_overlay = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self.header)
        toolbar.set_content(self.toast_overlay)
        self.set_content(toolbar)

        split = Adw.OverlaySplitView()
        split.set_pin_sidebar(False)
        split.set_show_sidebar(True)
        split.set_min_sidebar_width(350)
        split.set_max_sidebar_width(430)
        split.set_sidebar_width_fraction(0.36)
        self.split = split
        self.sidebar_button.connect(
            "toggled",
            lambda button: split.set_show_sidebar(button.get_active()),
        )
        split.connect("notify::show-sidebar", self._sidebar_visibility_changed)
        breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 900sp")
        )
        breakpoint.add_setter(
            split,
            "collapsed",
            GObject.Value(GObject.TYPE_BOOLEAN, True),
        )
        breakpoint.add_setter(
            split,
            "show-sidebar",
            GObject.Value(GObject.TYPE_BOOLEAN, False),
        )
        self.add_breakpoint(breakpoint)
        self.toast_overlay.set_child(split)

        # -- connection setup sidebar -----------------------------------
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        side_header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        side_header.set_margin_top(18)
        side_header.set_margin_bottom(4)
        side_header.set_margin_start(18)
        side_header.set_margin_end(18)
        side_title = Gtk.Label(label="Connection setup", xalign=0)
        side_title.add_css_class("title-4")
        side_caption = Gtk.Label(label="Choose a transport and tunnel options", xalign=0)
        side_caption.add_css_class("caption")
        side_caption.add_css_class("dim-label")
        side_header.append(side_title)
        side_header.append(side_caption)
        sidebar.append(side_header)

        page = Adw.PreferencesPage()
        page.set_vexpand(True)
        sidebar.append(page)

        # -- connection group -------------------------------------------
        conn_group = Adw.PreferencesGroup(title="Transport")
        page.add(conn_group)

        self.mode_row = Adw.ComboRow(title="Mode")
        self.mode_row.set_model(
            Gtk.StringList.new(["USB (ADB)", "Wi-Fi", "Bluetooth"])
        )
        self.mode_row.set_selected(0)
        self.mode_row.connect("notify::selected", self._on_mode_changed)
        conn_group.add(self.mode_row)

        self.device_row = Adw.ComboRow(title="Android device")
        self.device_row.set_subtitle("Searching for adb devices...")
        self.device_row.set_model(Gtk.StringList.new(["(no device)"]))
        self.device_row.set_selected(0)
        conn_group.add(self.device_row)

        self.bt_row = Adw.ComboRow(title="Bluetooth device")
        self.bt_row.set_subtitle("Switch to Bluetooth mode to load devices")
        self.bt_row.set_model(Gtk.StringList.new(["(no paired device)"]))
        self.bt_row.set_selected(0)
        self.bt_row.set_visible(False)
        conn_group.add(self.bt_row)

        self.bt_scan_button = Gtk.Button(label="Scan")
        self.bt_scan_button.add_css_class("pill")
        self.bt_scan_button.connect("clicked", lambda _b: self._scan_bt_devices())
        self.bt_scan_row = Adw.ActionRow(title="Bluetooth scan")
        self.bt_scan_row.set_subtitle(
            "Find nearby phones; open the phone's Bluetooth settings to make it discoverable"
        )
        self.bt_scan_row.add_suffix(self.bt_scan_button)
        self.bt_scan_row.set_visible(False)
        conn_group.add(self.bt_scan_row)

        self.host_row = Adw.EntryRow(title="Phone IP")
        self.host_row.set_text("192.168.49.1")
        self.host_row.set_visible(False)
        conn_group.add(self.host_row)

        self.port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.port_row.set_title("Wi-Fi port")
        self.port_row.set_value(transport.WIFI_PORT)
        self.port_row.set_visible(False)
        conn_group.add(self.port_row)

        discover_button = Gtk.Button(label="Discover hotspot")
        discover_button.add_css_class("pill")
        discover_button.connect("clicked", lambda _b: self._discover())
        self.discover_row = Adw.ActionRow(title="Wi-Fi discovery")
        self.discover_row.set_subtitle("Find the phone with a UDP broadcast")
        self.discover_row.add_suffix(discover_button)
        self.discover_row.set_visible(False)
        conn_group.add(self.discover_row)

        # -- tunnel group ------------------------------------------------
        tunnel_group = Adw.PreferencesGroup(
            title="Tunnel",
            description="The phone accepts only the address 10.1.19.2; "
            "ICMP is not forwarded by PdaNet.",
        )
        page.add(tunnel_group)

        self.route_row = Adw.SwitchRow(title="Route all IPv4 traffic")
        self.route_row.set_subtitle(
            "Use a dedicated policy-routing table; the normal default route is kept"
        )
        self.route_row.set_active(True)
        tunnel_group.add(self.route_row)

        self.dns_row = Adw.EntryRow(title="DNS servers (comma separated)")
        self.dns_row.set_text("8.8.8.8")
        tunnel_group.add(self.dns_row)

        self.mtu_row = Adw.SpinRow.new_with_range(576, 9000, 1)
        self.mtu_row.set_title("MTU")
        self.mtu_row.set_value(1500)
        tunnel_group.add(self.mtu_row)

        self.tun_row = Adw.EntryRow(title="TUN interface name")
        self.tun_row.set_text("pdanet0")
        tunnel_group.add(self.tun_row)

        self.privilege_row = Adw.ComboRow(title="Privilege method")
        self.privilege_row.set_model(
            Gtk.StringList.new(["Auto (pkexec)", "sudo", "pkexec"])
        )
        self.privilege_row.set_selected(0)
        tunnel_group.add(self.privilege_row)

        sidebar_scroller = Gtk.ScrolledWindow()
        sidebar_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        sidebar_scroller.add_css_class("pdanet-sidebar")
        sidebar_scroller.set_child(sidebar)
        split.set_sidebar(sidebar_scroller)

        # -- desktop dashboard ------------------------------------------
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_hexpand(True)
        content.set_vexpand(True)
        content.set_margin_top(20)
        content.set_margin_bottom(18)
        content.set_margin_start(22)
        content.set_margin_end(22)

        status_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        status_card.add_css_class("pdanet-card")
        status_inner = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18
        )
        status_inner.set_margin_top(20)
        status_inner.set_margin_bottom(20)
        status_inner.set_margin_start(22)
        status_inner.set_margin_end(22)
        status_card.append(status_inner)

        status_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.status_icon = Gtk.Image.new_from_icon_name(
            "network-wired-disconnected-symbolic"
        )
        self.status_icon.set_pixel_size(52)
        self.status_icon.set_valign(Gtk.Align.START)
        status_header.append(self.status_icon)

        status_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        status_text.set_hexpand(True)
        self.status_title = Gtk.Label(label="Disconnected", xalign=0)
        self.status_title.add_css_class("pdanet-status-title")
        self.status_detail = Gtk.Label(label="No active tunnel", xalign=0)
        self.status_detail.set_wrap(True)
        self.status_detail.add_css_class("dim-label")
        status_text.append(self.status_title)
        status_text.append(self.status_detail)
        status_header.append(status_text)
        status_inner.append(status_header)

        metrics = Gtk.Grid()
        metrics.set_column_spacing(36)
        metrics.set_row_spacing(14)

        def metric(caption: str, hint: str):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            label = Gtk.Label(label=caption, xalign=0)
            label.add_css_class("caption")
            label.add_css_class("dim-label")
            value = Gtk.Label(label="\u2014", xalign=0)
            value.add_css_class("pdanet-metric-value")
            detail = Gtk.Label(label=hint, xalign=0)
            detail.add_css_class("caption")
            detail.add_css_class("dim-label")
            box.append(label)
            box.append(value)
            box.append(detail)
            return box, value, detail

        recv_box, self.recv_value, self.recv_detail = metric(
            "Downloaded", "0 packets"
        )
        sent_box, self.sent_value, self.sent_detail = metric(
            "Uploaded", "0 packets"
        )
        uptime_box, self.uptime_value, _uptime_hint = metric(
            "Uptime", "session time"
        )
        interface_box, self.interface_value, _interface_hint = metric(
            "Interface", "TUN device"
        )
        signal_box, self.signal_value, self.signal_detail = metric(
            "Phone signal", "cellular radio"
        )
        metrics.attach(recv_box, 0, 0, 1, 1)
        metrics.attach(sent_box, 1, 0, 1, 1)
        metrics.attach(uptime_box, 0, 1, 1, 1)
        metrics.attach(interface_box, 1, 1, 1, 1)
        metrics.attach(signal_box, 0, 2, 2, 1)
        status_inner.append(metrics)
        content.append(status_card)

        # -- primary and quick actions ----------------------------------
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.connect_button = Gtk.Button(label="Connect")
        self.connect_button.add_css_class("suggested-action")
        self.connect_button.set_size_request(170, 42)
        self.connect_button.connect("clicked", lambda _b: self._toggle_connect())
        actions.append(self.connect_button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        actions.append(spacer)

        self.test_button = Gtk.Button(label="Internet Test")
        self.test_button.connect("clicked", lambda _b: self._show_internet_test())
        actions.append(self.test_button)

        self.share_button = Gtk.Button(label="Wi-Fi Share")
        self.share_button.connect("clicked", lambda _b: self._show_wifi_share())
        actions.append(self.share_button)

        self.settings_button = Gtk.Button(label="Settings")
        self.settings_button.connect("clicked", lambda _b: self._show_settings())
        actions.append(self.settings_button)
        content.append(actions)

        # -- activity log -----------------------------------------------
        log_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        log_title = Gtk.Label(label="Activity", xalign=0)
        log_title.add_css_class("pdanet-section-title")
        log_header.append(log_title)
        log_spacer = Gtk.Box()
        log_spacer.set_hexpand(True)
        log_header.append(log_spacer)

        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_left_margin(12)
        self.log_view.set_right_margin(12)
        self.log_view.set_top_margin(10)
        self.log_view.set_bottom_margin(10)
        self.log_view.add_css_class("caption")
        self.log_view.add_css_class("pdanet-log")
        self.log_buffer = self.log_view.get_buffer()

        clear_button = Gtk.Button(icon_name="edit-clear-symbolic")
        clear_button.add_css_class("flat")
        clear_button.set_tooltip_text("Clear activity log")
        clear_button.connect("clicked", lambda _b: self._clear_log())
        log_header.append(clear_button)
        content.append(log_header)

        log_frame = Gtk.Frame()
        log_frame.add_css_class("pdanet-card")
        log_frame.set_vexpand(True)
        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_min_content_height(180)
        log_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC
        )
        log_scroller.set_child(self.log_view)

        log_overlay = Gtk.Overlay()
        log_overlay.set_child(log_scroller)
        self.log_placeholder = Gtk.Label(
            label="Connection activity will appear here"
        )
        self.log_placeholder.add_css_class("dim-label")
        self.log_placeholder.set_can_target(False)
        log_overlay.add_overlay(self.log_placeholder)
        log_frame.set_child(log_overlay)
        content.append(log_frame)

        content_scroller = Gtk.ScrolledWindow()
        content_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        content_scroller.set_child(content)
        split.set_content(content_scroller)
        self._update_log_placeholder()
        self._on_mode_changed(self.mode_row, None)

    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(_WINDOW_CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _sidebar_visibility_changed(
        self, split: Adw.OverlaySplitView, _pspec: object
    ) -> None:
        active = split.get_show_sidebar()
        if self.sidebar_button.get_active() != active:
            self.sidebar_button.set_active(active)

    # ------------------------------------------------------------------
    # Menu, settings and desktop feature parity
    # ------------------------------------------------------------------
    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        self.insert_action_group("win", group)

        def add(name: str, callback) -> Gio.SimpleAction:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            group.add_action(action)
            return action

        add("connect-usb", lambda *_: self._begin_connect("usb"))
        add("connect-wifi", lambda *_: self._begin_connect("wifi"))
        add("connect-bluetooth", lambda *_: self._begin_connect("bluetooth"))
        add("disconnect", lambda *_: self._disconnect())
        add("wifi-share", lambda *_: self._show_wifi_share())
        add("reverse-tether", lambda *_: self._show_reverse_tether())
        add("windows-hotspot", lambda *_: self._show_windows_hotspot())
        add("settings", lambda *_: self._show_settings())
        add("bt-pairing", lambda *_: self._show_bt_pairing())
        add("wifi-pairing", lambda *_: self._show_wifi_pairing())
        add("internet-test", lambda *_: self._show_internet_test())
        add("help", lambda *_: self._show_help())
        add("about", lambda *_: self._show_about())
        add("hide-to-tray", lambda *_: self._hide_to_tray())
        add("quit", lambda *_: self._quit())

        self.auto_action = Gio.SimpleAction.new_stateful(
            "auto-connect",
            None,
            GLib.Variant.new_boolean(self.settings.auto_connect_usb),
        )
        self.auto_action.connect("change-state", self._on_auto_connect_toggled)
        group.add_action(self.auto_action)

        self.block_action = Gio.SimpleAction.new_stateful(
            "block-microsoft",
            None,
            GLib.Variant.new_boolean(self.settings.block_microsoft),
        )
        self.block_action.connect("change-state", self._on_block_toggled)
        group.add_action(self.block_action)

        self.throttle_action = Gio.SimpleAction.new_stateful(
            "throttle",
            None,
            GLib.Variant.new_boolean(self.settings.throttle_kbps > 0),
        )
        self.throttle_action.connect("change-state", self._on_throttle_toggled)
        group.add_action(self.throttle_action)

    def _build_menu(self) -> Gio.Menu:
        menu = Gio.Menu()

        connect = Gio.Menu()
        connect.append("Connect USB", "win.connect-usb")
        connect.append("Connect WiFi...", "win.connect-wifi")
        connect.append("Connect Bluetooth", "win.connect-bluetooth")
        connect.append("Disconnect", "win.disconnect")
        menu.append_section(None, connect)

        tools = Gio.Menu()
        tools.append("WiFi Share (Beta)...", "win.wifi-share")
        tools.append("Reverse PdaNet (phone uses laptop)...", "win.reverse-tether")
        tools.append("Settings", "win.settings")
        tools.append("Bluetooth Pairing...", "win.bt-pairing")
        tools.append("WiFi Pairing...", "win.wifi-pairing")
        tools.append("Windows Hotspot Settings...", "win.windows-hotspot")
        tools.append("Internet Test", "win.internet-test")
        menu.append_section(None, tools)

        options = Gio.Menu()
        options.append("Auto Connect on USB Plug-in", "win.auto-connect")
        options.append("Block Microsoft Connections", "win.block-microsoft")
        options.append("Throttle Connection", "win.throttle")
        menu.append_section(None, options)

        help_menu = Gio.Menu()
        help_menu.append("Hide to Tray", "win.hide-to-tray")
        help_menu.append("Online Help", "win.help")
        help_menu.append("About PdaNet for Android", "win.about")
        help_menu.append("Exit", "win.quit")
        menu.append_section(None, help_menu)
        return menu

    def _apply_settings(self) -> None:
        settings = self.settings
        self.mode_row.set_selected(
            {"usb": 0, "wifi": 1, "bluetooth": 2}.get(settings.mode, 0)
        )
        self.host_row.set_text(settings.wifi_host or "192.168.49.1")
        self.port_row.set_value(settings.wifi_port)
        self.dns_row.set_text(settings.dns)
        self.mtu_row.set_value(settings.mtu)
        self.route_row.set_active(settings.route_all)
        self.privilege_row.set_selected(
            {"auto": 0, "sudo": 1, "pkexec": 2, "root": 0}.get(
                settings.privilege, 0
            )
        )

    def _save_settings(self) -> None:
        try:
            config.save(self.settings)
        except OSError as exc:
            self._append_log(f"could not save settings: {exc}")

    def _settings_saved(self, updated: config.Settings) -> None:
        self.settings = updated
        self._save_settings()
        self.autoconnect.set_enabled(updated.auto_connect_usb)
        self.auto_action.set_state(GLib.Variant.new_boolean(updated.auto_connect_usb))
        self.block_action.set_state(
            GLib.Variant.new_boolean(updated.block_microsoft)
        )
        self.throttle_action.set_state(
            GLib.Variant.new_boolean(updated.throttle_kbps > 0)
        )
        if self.conn is not None:
            self.conn.set_throttle(updated.throttle_kbps)
        self._sync_tray()
        self._toast("Settings saved")

    def _on_auto_connect_toggled(
        self, action: Gio.SimpleAction, value: GLib.Variant
    ) -> None:
        enabled = value.get_boolean()
        action.set_state(value)
        self.settings.auto_connect_usb = enabled
        self.autoconnect.set_enabled(enabled)
        self._save_settings()
        self._sync_tray()
        self._toast(
            "Auto Connect enabled: the next USB plug-in will connect"
            if enabled
            else "Auto Connect disabled"
        )

    def _on_block_toggled(
        self, action: Gio.SimpleAction, value: GLib.Variant
    ) -> None:
        enabled = value.get_boolean()
        action.set_state(value)
        self.settings.block_microsoft = enabled
        self._save_settings()
        self._sync_tray()
        self._toast(
            "Microsoft update names will be blocked on the next connection"
            if enabled
            else "Microsoft blocking disabled for the next connection"
        )

    def _on_throttle_toggled(
        self, action: Gio.SimpleAction, value: GLib.Variant
    ) -> None:
        enabled = value.get_boolean()
        action.set_state(value)
        if enabled and self.settings.throttle_kbps <= 0:
            self.settings.throttle_kbps = 512
        elif not enabled:
            self.settings.throttle_kbps = 0
        self._save_settings()
        self._toast(
            f"Throttle enabled at {self.settings.throttle_kbps} kbit/s"
            if enabled
            else "Throttle disabled"
        )
        if self.conn is not None:
            self.conn.set_throttle(self.settings.throttle_kbps)
        self._sync_tray()

    def _set_mode(self, mode: str) -> None:
        index = {"usb": 0, "wifi": 1, "bluetooth": 2}[mode]
        if self.mode_row.get_selected() != index:
            self.mode_row.set_selected(index)

    def _begin_connect(self, mode: str) -> None:
        if self.conn is not None:
            self._pending_mode = mode
            self._append_log(f"switching to {mode} after disconnect...")
            self.connect_button.set_sensitive(False)
            self.conn.stop()
            return
        self._set_mode(mode)
        if mode == "usb":
            if not [d for d in self.devices if d.state == "device"]:
                self._refresh_devices()
                self._toast(
                    "No authorized USB device. Connect the phone and approve "
                    "USB debugging."
                )
                return
        elif mode == "bluetooth" and not self.bt_devices:
            self._toast("Pair a Bluetooth device first")
            self._show_bt_pairing()
            return
        self._toggle_connect()

    def _disconnect(self) -> None:
        if self.conn is None:
            self._toast("Not connected")
            return
        self._pending_mode = None
        self._append_log("disconnecting...")
        self.connect_button.set_sensitive(False)
        self.conn.stop()

    def _show_settings(self) -> None:
        dialogs.SettingsDialog(self.settings, self._settings_saved).present(self)

    def _show_bt_pairing(self) -> None:
        dialogs.BluetoothPairingDialog(self._bt_paired).present(self)

    def _bt_paired(self, address: str) -> None:
        self._target_bt = address
        self.settings.bt_device = address
        self._save_settings()
        self._set_mode("bluetooth")
        self._refresh_bt_devices()
        self._toast("Bluetooth device paired and trusted")

    def _show_wifi_pairing(self) -> None:
        dialogs.WifiPairingDialog(self._wifi_connected).present(self)

    def _wifi_connected(self, address: str | None) -> None:
        if address:
            self.host_row.set_text(address)
            self.settings.wifi_host = address
            self._save_settings()
        self._toast("Wi-Fi connected")
        self._set_mode("wifi")

    def _show_wifi_share(self) -> None:
        dialogs.WifiShareDialog(self.settings, self._share_state).present(self)

    def _share_state(self, active: bool) -> None:
        self._share_started = active
        self._toast("WiFi Share active" if active else "WiFi Share stopped")

    def _show_reverse_tether(self) -> None:
        dialogs.ReverseTetherDialog(self.settings, self._reverse_state).present(
            self
        )

    def _reverse_state(self, active: bool) -> None:
        self._toast(
            "Reverse PdaNet server active"
            if active
            else "Reverse PdaNet server stopped"
        )

    def _show_windows_hotspot(self) -> None:
        self._toast(
            "Windows Hotspot Settings is replaced by WiFi Share on Linux"
        )
        self._show_wifi_share()

    def _show_internet_test(self) -> None:
        dialogs.InternetTestDialog().present(self)

    def _show_help(self) -> None:
        dialogs.HelpDialog().present(self)

    def _notify(self, title: str, body: str) -> None:
        if not self.settings.show_notifications or self._closing:
            return
        try:
            subprocess.Popen(
                ["notify-send", "-a", "PdaNet Linux", "-i", __app_id__, title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    # ------------------------------------------------------------------
    # System tray and close-to-background
    # ------------------------------------------------------------------
    def _tray_status_text(self) -> str:
        if self.conn is None:
            return "Disconnected"
        detail = self.status_detail.get_text() or "Connected"
        if self._signal is not None:
            return f"{detail}\n{self._signal.description}"
        if self._signal_error:
            return f"{detail}\nPhone signal unavailable (ADB)"
        return detail

    def _signal_icon(self) -> str:
        if self._signal is None:
            return ""
        level = max(0, min(4, int(self._signal.level)))
        names = ("none", "weak", "ok", "good", "excellent")
        return f"network-cellular-signal-{names[level]}-symbolic"

    def _sync_tray(self) -> None:
        app = self.get_application()
        icon = getattr(app, "tray", None)
        if icon is None:
            return
        icon.update_toggle(TRAY_AUTO_CONNECT, self.settings.auto_connect_usb)
        icon.update_toggle(TRAY_BLOCK_MICROSOFT, self.settings.block_microsoft)
        icon.update_toggle(TRAY_THROTTLE, self.settings.throttle_kbps > 0)
        icon.set_item_enabled(TRAY_DISCONNECT, self.conn is not None)
        if self._signal is not None:
            signal_label = f"Phone signal: {self._signal.compact}"
        elif self._signal_error:
            signal_label = "Phone signal: unavailable"
        else:
            signal_label = "Phone signal: \u2014"
        icon.set_item_label(TRAY_SIGNAL, signal_label)
        icon.set_overlay_icon(
            self._signal_icon() if self.conn is not None else ""
        )
        icon.set_status(self._tray_status_text())

    # ------------------------------------------------------------------
    # Phone cellular signal (best effort, via adb)
    # ------------------------------------------------------------------
    def _update_signal_display(self) -> None:
        if self._signal is not None:
            self.signal_value.set_text(self._signal.compact)
            detail = self._signal.detail or self._signal.data_state
            self.signal_detail.set_text(detail or "cellular radio")
            tooltip = self._signal.description or self._signal.compact
            self.signal_value.set_tooltip_text(tooltip)
            self.signal_detail.set_tooltip_text(tooltip)
        elif self._signal_error:
            self.signal_value.set_text("\u2014")
            self.signal_detail.set_text("ADB telemetry unavailable")
            self.signal_value.set_tooltip_text(
                "Connect the phone with USB debugging enabled to read the "
                "cellular signal. The tunnel is not affected."
            )
            self.signal_detail.set_tooltip_text(
                self.signal_value.get_tooltip_text()
            )
        else:
            self.signal_value.set_text("\u2014")
            self.signal_detail.set_text("cellular radio")
            self.signal_value.set_tooltip_text("")
            self.signal_detail.set_tooltip_text("")

    def _reset_signal(self) -> None:
        self._signal_generation += 1
        self._signal_busy = False
        self._signal = None
        self._signal_error = False
        self._active_serial = None
        self._update_signal_display()

    def _clear_signal(self) -> None:
        self._reset_signal()

    def _poll_signal(self) -> bool:
        if self._closing:
            return False
        if self.conn is not None and not self._signal_busy:
            self._request_signal()
        return True

    def _request_signal(self) -> bool:
        if self.conn is None or self._signal_busy:
            return False
        self._signal_busy = True
        generation = self._signal_generation
        serial = self._active_serial
        threading.Thread(
            target=self._signal_worker,
            args=(generation, serial),
            daemon=True,
        ).start()
        return False

    def _signal_worker(
        self, generation: int, serial: str | None
    ) -> None:
        try:
            signal = cellular.read_signal(serial)
        except Exception:  # noqa: BLE001 - telemetry is optional
            signal = None
        GLib.idle_add(self._signal_done, generation, signal)

    def _signal_done(
        self,
        generation: int,
        signal: cellular.CellularSignal | None,
    ) -> bool:
        if generation != self._signal_generation:
            return False
        self._signal_busy = False
        self._signal_error = signal is None
        if signal is not None:
            self._signal = signal
            self.signal_detail.set_text(
                self._signal.detail or "cellular radio"
            )
        self._update_signal_display()
        self._sync_tray()
        return False

    def _hide_to_tray(self) -> None:
        app = self.get_application()
        icon = getattr(app, "tray", None)
        if icon is not None and icon.available:
            self.hide()
            self._sync_tray()
            self._notify(
                "PdaNet is still running",
                "Use the tray icon to show the window, disconnect or quit.",
            )
        else:
            self.minimize()

    def _quit(self) -> None:
        self._quitting = True
        self._pending_mode = None
        self.get_application().quit()

    # ------------------------------------------------------------------
    # USB plug-in auto-connect
    # ------------------------------------------------------------------
    def _poll_usb(self) -> bool:
        if self._closing:
            return False
        if self._usb_poll_busy:
            return True
        self._usb_poll_busy = True
        threading.Thread(target=self._poll_usb_worker, daemon=True).start()
        return True

    def _poll_usb_worker(self) -> None:
        try:
            devices = transport.list_devices()
        except Exception:  # noqa: BLE001 - polling must never crash the GUI
            GLib.idle_add(self._poll_usb_failed)
            return
        ready = [d.serial for d in devices if d.state == "device"]
        new = self.autoconnect.update(ready)
        GLib.idle_add(self._poll_usb_done, devices, new)

    def _poll_usb_failed(self) -> bool:
        self._usb_poll_busy = False
        return False

    def _poll_usb_done(
        self, devices: list[transport.AdbDevice], new_serial: str | None
    ) -> bool:
        self._usb_poll_busy = False
        if self._closing:
            return False
        current = {d.serial for d in self.devices if d.state == "device"}
        ready = {d.serial for d in devices if d.state == "device"}
        if current != ready and self.conn is None and self.worker is None:
            self.devices = devices
            self._devices_loaded(devices)
        if (
            new_serial
            and self.settings.auto_connect_usb
            and self.conn is None
            and self.worker is None
        ):
            self._append_log(f"auto-connect: new USB device {new_serial}")
            self._set_mode("usb")
            index = 0
            for position, device in enumerate(
                [d for d in devices if d.state == "device"]
            ):
                if device.serial == new_serial:
                    index = position
                    break
            self.device_row.set_selected(index)
            GLib.timeout_add(250, self._auto_connect_now)
        return False

    def _auto_connect_now(self) -> bool:
        if self.conn is None and not self._closing:
            self._begin_connect("usb")
        return False


    def _toast(self, text: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=text, timeout=4))

    def _append_log(self, text: str) -> bool:
        stamp = time.strftime("%H:%M:%S")
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, f"[{stamp}] {text}\n")
        mark = self.log_buffer.get_insert()
        self.log_view.scroll_to_mark(mark, 0.0, False, 0.0, 0.0)
        self._update_log_placeholder()
        if self.settings.log_to_file:
            try:
                config.append_log(f"[{stamp}] {text}")
            except OSError:
                pass
        return False

    def _clear_log(self) -> None:
        self.log_buffer.set_text("")
        self._update_log_placeholder()

    def _update_log_placeholder(self) -> None:
        self.log_placeholder.set_visible(self.log_buffer.get_char_count() == 0)

    def _log_threadsafe(self, text: str) -> None:
        GLib.idle_add(self._append_log, text)

    def _on_mode_changed(self, row: Adw.ComboRow, _pspec: object) -> None:
        selected = row.get_selected()
        wifi = selected == 1
        bluetooth = selected == 2
        self.device_row.set_visible(not wifi and not bluetooth)
        self.host_row.set_visible(wifi)
        self.port_row.set_visible(wifi)
        self.discover_row.set_visible(wifi)
        self.bt_row.set_visible(bluetooth)
        self.bt_scan_row.set_visible(bluetooth)
        if self.conn is None and self.worker is None:
            self.status_icon.set_from_icon_name(self._mode_icon(False))
        if bluetooth:
            self._refresh_bt_devices()

    def _mode_icon(self, active: bool) -> str:
        mode = {0: "usb", 1: "wifi", 2: "bluetooth"}.get(
            self.mode_row.get_selected(), "usb"
        )
        if mode == "wifi":
            return (
                "network-wireless-symbolic"
                if active
                else "network-wireless-offline-symbolic"
            )
        if mode == "bluetooth":
            return (
                "bluetooth-symbolic" if active else "bluetooth-disabled-symbolic"
            )
        return (
            "network-wired-symbolic"
            if active
            else "network-wired-disconnected-symbolic"
        )

    def _show_about(self) -> None:
        dialog = Adw.AboutDialog(
            application_name="PdaNet Linux",
            application_icon=__app_id__,
            version=__version__,
            developer_name="Unofficial community client",
            comments=(
                "TUN client for PdaNet USB, Wi-Fi and Bluetooth tethering.\n\n"
                "Protocol reverse engineered from the PdaNet+ 5.23 Android "
                "package. This project is not affiliated with PdaNet/FoxFi "
                "and does not bypass registration or licensing."
            ),
            license_type=Gtk.License.GPL_3_0,
        )
        dialog.present(self)

    # ------------------------------------------------------------------
    # Device discovery
    # ------------------------------------------------------------------
    def _refresh_devices(self) -> None:
        self.refresh_button.set_sensitive(False)
        if self.mode_row.get_selected() == 2:
            self._refresh_bt_devices()
            return
        self.device_row.set_subtitle("Searching for adb devices...")
        threading.Thread(target=self._load_devices, daemon=True).start()

    def _refresh_bt_devices(self, *, scan: bool = False) -> None:
        self.refresh_button.set_sensitive(False)
        self.bt_scan_button.set_sensitive(False)
        self.bt_row.set_subtitle(
            "Scanning for nearby Bluetooth devices..." if scan
            else "Searching for paired Bluetooth devices..."
        )
        threading.Thread(target=self._load_bt_devices, args=(scan,), daemon=True).start()

    def _scan_bt_devices(self) -> None:
        self._refresh_bt_devices(scan=True)

    def _load_bt_devices(self, scan: bool) -> None:
        try:
            if scan:
                devices = transport.scan_bluetooth_devices(
                    duration=8.0, log=self._log_threadsafe
                )
            else:
                self._log_threadsafe("listing Bluetooth devices paired with BlueZ")
                devices = transport.list_bluetooth_devices(paired_only=True)
                if not devices:
                    # Be helpful on first use: a scan can find the phone
                    # before it has ever been paired with this PC.
                    scan = True
                    self._log_threadsafe(
                        "no paired Bluetooth device; scanning for nearby phones"
                    )
                    GLib.idle_add(
                        self.bt_row.set_subtitle,
                        "No paired device; scanning for nearby phones...",
                    )
                    devices = transport.scan_bluetooth_devices(
                        duration=8.0, log=self._log_threadsafe
                    )
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self._log_threadsafe(f"Bluetooth discovery failed: {exc}")
            GLib.idle_add(self._bt_devices_failed, str(exc))
            return
        GLib.idle_add(self._bt_devices_loaded, devices, scan)

    def _bt_devices_failed(self, message: str) -> bool:
        self.refresh_button.set_sensitive(True)
        self.bt_scan_button.set_sensitive(True)
        self.bt_row.set_subtitle(message)
        self._toast(f"Bluetooth discovery failed: {message}")
        return False

    def _bt_devices_loaded(
        self, devices: list[bt.BluetoothDevice], scanned: bool = False
    ) -> bool:
        self.refresh_button.set_sensitive(True)
        self.bt_scan_button.set_sensitive(True)
        self.bt_devices = devices
        if not scanned:
            if devices:
                self._append_log(f"paired Bluetooth device(s): {len(devices)}")
                for device in devices:
                    self._append_log(f"paired Bluetooth device: {device.label}")
            else:
                self._append_log("no paired Bluetooth device found")
        labels = [
            f"{d.label}{'' if d.paired else ' (unpaired)'}" for d in devices
        ] or ["(no Bluetooth device)"]
        self.bt_model = Gtk.StringList.new(labels)
        self.bt_row.set_model(self.bt_model)
        self.bt_row.set_selected(0)
        if self._target_bt:
            target = self._target_bt.lower()
            for index, device in enumerate(devices):
                if device.address.lower() == target:
                    self.bt_row.set_selected(index)
                    break
            self._target_bt = None
        if devices:
            paired = sum(1 for d in devices if d.paired)
            if scanned:
                self.bt_row.set_subtitle(
                    f"{len(devices)} device(s) found, {paired} paired"
                )
            else:
                self.bt_row.set_subtitle(f"{paired} paired device(s)")
        else:
            self.bt_row.set_subtitle(
                "No phone found - pair it with bluetoothctl or open the "
                "phone's Bluetooth settings and press Scan"
            )
        return False

    def _load_devices(self) -> None:
        try:
            devices = transport.list_devices()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            GLib.idle_add(self._devices_failed, str(exc))
            return
        GLib.idle_add(self._devices_loaded, devices)

    def _devices_failed(self, message: str) -> bool:
        self.refresh_button.set_sensitive(True)
        self.device_row.set_subtitle(message)
        return False

    def _devices_loaded(self, devices: list[transport.AdbDevice]) -> bool:
        self.refresh_button.set_sensitive(True)
        self.devices = devices
        ready = [d for d in devices if d.state == "device"]
        if not self._autoconnect_primed:
            self.autoconnect.update([d.serial for d in ready])
            self._autoconnect_primed = True
        labels = [d.label for d in ready] or ["(no device)"]
        self.device_model = Gtk.StringList.new(labels)
        self.device_row.set_model(self.device_model)
        self.device_row.set_selected(0)
        if ready:
            self.device_row.set_subtitle(f"{len(ready)} authorized device(s)")
        elif devices:
            self.device_row.set_subtitle(
                "Device is not authorized - approve the USB debugging prompt"
            )
        else:
            self.device_row.set_subtitle("No adb device found")
        return False

    def _discover(self) -> None:
        self._append_log("sending PdaNet Wi-Fi discovery probe...")
        threading.Thread(target=self._run_discovery, daemon=True).start()

    def _run_discovery(self) -> None:
        try:
            results = transport.discover_wifi(model=_hostname(), timeout=3.0)
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._append_log, f"discovery failed: {exc}")
            return
        GLib.idle_add(self._discovery_done, results)

    def _discovery_done(self, results: list[transport.DiscoveryResult]) -> bool:
        if not results:
            self._toast("No PdaNet hotspot answered")
            self._append_log("discovery: no answer")
            return False
        result = results[0]
        self.host_row.set_text(result.host)
        self._toast(f"Found {result.name or 'PdaNet'} at {result.host}")
        self._append_log(
            f"discovery: {result.name or '(unnamed)'} at {result.host} "
            f"(client {result.client_ip or '?'})"
        )
        return False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def _selected_serial(self) -> str | None:
        ready = [d for d in self.devices if d.state == "device"]
        if not ready:
            return None
        index = min(self.device_row.get_selected(), len(ready) - 1)
        return ready[index].serial

    def _selected_bt_device(self) -> str | None:
        if not self.bt_devices:
            return None
        index = min(self.bt_row.get_selected(), len(self.bt_devices) - 1)
        return self.bt_devices[index].address

    def _parse_dns(self) -> tuple[str, ...]:
        text = self.dns_row.get_text().strip()
        if not text:
            return ()
        parts = [p for p in re.split(r"[,;\s]+", text) if p]
        for part in parts:
            ipaddress.IPv4Address(part)
        return tuple(parts)

    def _tunnel_options(self) -> ConnectOptions:
        selected = self.mode_row.get_selected()
        bt_device = None
        serial = None
        host = None
        port = transport.WIFI_PORT
        if selected == 1:
            host = self.host_row.get_text().strip()
            if not host:
                raise ValueError("Enter the phone's Wi-Fi IP address")
            ipaddress.IPv4Address(host)
            mode = "wifi"
            port = int(self.port_row.get_value())
        elif selected == 2:
            bt_device = self._selected_bt_device()
            if not bt_device:
                raise ValueError(
                    "No Bluetooth device selected. Open the phone's "
                    "Bluetooth settings, press Scan, then choose the phone."
                )
            mode = "bluetooth"
        else:
            serial = self._selected_serial()
            if not serial:
                raise ValueError("No authorized ADB device. Connect the phone by USB.")
            mode = "usb"
        tun_name = self.tun_row.get_text().strip() or "pdanet0"
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", tun_name):
            raise ValueError("Invalid TUN interface name")
        privilege = {0: "auto", 1: "sudo", 2: "pkexec"}[
            self.privilege_row.get_selected()
        ]
        return ConnectOptions(
            mode=mode,
            serial=serial,
            host=host,
            port=port,
            bt_device=bt_device,
            bt_channel=0,
            model=_hostname(),
            tun_name=tun_name,
            mtu=int(self.mtu_row.get_value()),
            route_all=self.route_row.get_active(),
            dns=self._parse_dns(),
            throttle_kbps=self.settings.throttle_kbps,
            block_microsoft=self.settings.block_microsoft,
            privilege=privilege,
            log=self._log_threadsafe,
            verbose=False,
        )

    def _toggle_connect(self) -> None:
        if self.conn is not None:
            self._append_log("disconnecting...")
            self.connect_button.set_sensitive(False)
            self.conn.stop()
            return
        try:
            options = self._tunnel_options()
        except (ValueError, ipaddress.AddressValueError) as exc:
            self._toast(str(exc))
            return
        self._start_connection(options)

    def _start_connection(self, options: ConnectOptions) -> None:
        self._reset_signal()
        self._active_serial = options.serial or self._selected_serial()
        self.settings.mode = options.mode
        self.settings.serial = options.serial or ""
        self.settings.bt_device = options.bt_device or ""
        if options.host:
            self.settings.wifi_host = options.host
        self.settings.wifi_port = options.port
        self.settings.dns = ", ".join(options.dns)
        self.settings.mtu = options.mtu
        self.settings.route_all = options.route_all
        self.settings.privilege = options.privilege
        self._save_settings()
        self.log_buffer.set_text("")
        self._update_log_placeholder()
        self._set_busy(True)
        self.connect_button.set_label("Connecting...")
        self._set_status(
            "Connecting...", "Opening the PdaNet link", self._mode_icon(True)
        )
        self.status_icon.remove_css_class("accent")
        self.recv_value.set_text("\u2014")
        self.recv_detail.set_text("0 packets")
        self.sent_value.set_text("\u2014")
        self.sent_detail.set_text("0 packets")
        self.uptime_value.set_text("\u2014")
        self.interface_value.set_text("\u2014")
        self._sync_tray()
        conn = PdaNetConnection(options)
        self.conn = conn
        self.worker = threading.Thread(
            target=self._connection_worker, args=(conn,), daemon=True
        )
        self.worker.start()

    def _set_busy(self, busy: bool) -> None:
        self.connect_button.set_sensitive(True)
        self.mode_row.set_sensitive(not busy)
        self.device_row.set_sensitive(not busy)
        self.bt_row.set_sensitive(not busy)
        self.bt_scan_button.set_sensitive(not busy)
        self.host_row.set_sensitive(not busy)
        self.port_row.set_sensitive(not busy)
        self.route_row.set_sensitive(not busy)
        self.dns_row.set_sensitive(not busy)
        self.mtu_row.set_sensitive(not busy)
        self.tun_row.set_sensitive(not busy)
        self.privilege_row.set_sensitive(not busy)
        self.refresh_button.set_sensitive(not busy)

    def _connection_worker(self, conn: PdaNetConnection) -> None:
        try:
            conn.open()
            GLib.idle_add(self._connected, conn.description, conn.tun_name)
            reason = conn.run(
                on_stats=lambda stats: GLib.idle_add(
                    self._stats,
                    stats.sent_bytes,
                    stats.sent_packets,
                    stats.received_bytes,
                    stats.received_packets,
                    stats.elapsed,
                )
            )
            GLib.idle_add(self._disconnected, reason)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            GLib.idle_add(self._failed, str(exc))
        finally:
            conn.close()
            GLib.idle_add(self._finished)

    def _set_status(self, title: str, detail: str, icon: str) -> None:
        self.status_title.set_text(title)
        self.status_detail.set_text(detail)
        self.status_icon.set_from_icon_name(icon)

    def _connected(self, description: str, tun_name: str) -> bool:
        self._set_status(
            "Connected",
            f"{tun_name} \u2014 {description}",
            self._mode_icon(True),
        )
        self.status_icon.add_css_class("accent")
        self.interface_value.set_text(tun_name)
        self.connect_button.set_label("Disconnect")
        self.connect_button.remove_css_class("suggested-action")
        self.connect_button.add_css_class("destructive-action")
        self._set_busy(True)
        self._notify("PdaNet connected", f"{tun_name} - {description}")
        self.signal_value.set_text("\u2014")
        self.signal_detail.set_text("reading from ADB\u2026")
        self._sync_tray()
        GLib.timeout_add(500, self._request_signal)
        return False

    def _stats(
        self,
        sent_bytes: int,
        sent_packets: int,
        recv_bytes: int,
        recv_packets: int,
        elapsed: float,
    ) -> bool:
        self.sent_value.set_text(format_bytes(sent_bytes))
        self.sent_detail.set_text(f"{sent_packets} packets")
        self.recv_value.set_text(format_bytes(recv_bytes))
        self.recv_detail.set_text(f"{recv_packets} packets")
        self.uptime_value.set_text(format_duration(elapsed))
        return False

    def _disconnected(self, reason: str) -> bool:
        self._clear_signal()
        self._set_status("Disconnected", reason, self._mode_icon(False))
        self.status_icon.remove_css_class("accent")
        self.interface_value.set_text("\u2014")
        self._notify("PdaNet disconnected", reason)
        self._sync_tray()
        return False

    def _failed(self, message: str) -> bool:
        self._clear_signal()
        self._set_status("Connection failed", message, "dialog-error-symbolic")
        self.status_icon.remove_css_class("accent")
        self.interface_value.set_text("\u2014")
        self._toast(message)
        self._append_log(f"error: {message}")
        self._notify("PdaNet connection failed", message)
        self._sync_tray()
        return False

    def _finished(self) -> bool:
        self._clear_signal()
        self.conn = None
        self.worker = None
        self.connect_button.set_label("Connect")
        self.connect_button.remove_css_class("destructive-action")
        self.connect_button.add_css_class("suggested-action")
        self._set_busy(False)
        self._sync_tray()
        pending = self._pending_mode
        self._pending_mode = None
        if pending and not self._closing:
            GLib.timeout_add(350, self._resume_connect, pending)
        return False

    def _resume_connect(self, mode: str) -> bool:
        if not self._closing and self.conn is None:
            self._append_log(f"reconnecting via {mode}...")
            self._begin_connect(mode)
        return False

    def _cleanup_before_exit(self) -> None:
        self._closing = True
        self._pending_mode = None
        if self._cleaned_up:
            return
        self._cleaned_up = True
        if self._share_started:
            # Let NetworkManager clean up even though this process is about
            # to exit; nmcli keeps running in its own session.
            try:
                subprocess.Popen(
                    [
                        "/bin/sh",
                        "-c",
                        "nmcli connection down pdanet-share >/dev/null 2>&1; "
                        "nmcli connection delete pdanet-share >/dev/null 2>&1",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError:
                pass
            self._share_started = False
        if self.conn is not None:
            self.conn.stop()

    def _on_close_request(self, _window: "PdaNetWindow") -> bool:
        if self._quitting:
            self._cleanup_before_exit()
            return False
        icon = getattr(self.get_application(), "tray", None)
        if (
            self.settings.close_to_tray
            and icon is not None
            and icon.available
        ):
            self._hide_to_tray()
            return True
        self._cleanup_before_exit()
        return False


class PdaNetApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=__app_id__,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.window: PdaNetWindow | None = None
        self.tray: tray.TrayIcon | None = None

    def do_activate(self) -> None:  # noqa: N802 (GTK override)
        if self.window is None:
            self.window = PdaNetWindow(self)
            self._create_tray()
        self._show_window()

    def _show_window(self) -> None:
        if self.window is None:
            return
        self.window.present()
        if hasattr(self.window, "unminimize"):
            self.window.unminimize()

    def _present_and(self, callback) -> None:
        self._show_window()

        def run() -> bool:
            callback()
            return False

        GLib.idle_add(run)

    def _create_tray(self) -> None:
        window = self.window
        if window is None:
            return
        items = [
            tray.MenuItem(TRAY_SHOW, "Show PdaNet", action=self._show_window),
            tray.MenuItem(2, kind="separator"),
            tray.MenuItem(
                TRAY_CONNECT_USB,
                "Connect USB",
                action=lambda: window._begin_connect("usb"),
            ),
            tray.MenuItem(
                TRAY_CONNECT_WIFI,
                "Connect WiFi...",
                action=lambda: self._present_and(
                    lambda: window._begin_connect("wifi")
                ),
            ),
            tray.MenuItem(
                TRAY_CONNECT_BLUETOOTH,
                "Connect Bluetooth",
                action=lambda: self._present_and(
                    lambda: window._begin_connect("bluetooth")
                ),
            ),
            tray.MenuItem(
                TRAY_DISCONNECT, "Disconnect", action=window._disconnect
            ),
            tray.MenuItem(
                TRAY_SIGNAL, "Phone signal: \u2014", enabled=False
            ),
            tray.MenuItem(3, kind="separator"),
            tray.MenuItem(
                TRAY_WIFI_SHARE,
                "WiFi Share (Beta)...",
                action=lambda: self._present_and(window._show_wifi_share),
            ),
            tray.MenuItem(
                TRAY_REVERSE_TETHER,
                "Reverse PdaNet...",
                action=lambda: self._present_and(window._show_reverse_tether),
            ),
            tray.MenuItem(
                TRAY_SETTINGS,
                "Settings",
                action=lambda: self._present_and(window._show_settings),
            ),
            tray.MenuItem(
                TRAY_BT_PAIRING,
                "Bluetooth Pairing...",
                action=lambda: self._present_and(window._show_bt_pairing),
            ),
            tray.MenuItem(
                TRAY_WIFI_PAIRING,
                "WiFi Pairing...",
                action=lambda: self._present_and(window._show_wifi_pairing),
            ),
            tray.MenuItem(
                TRAY_INTERNET_TEST,
                "Internet Test",
                action=lambda: self._present_and(window._show_internet_test),
            ),
            tray.MenuItem(4, kind="separator"),
            tray.MenuItem(
                TRAY_AUTO_CONNECT,
                "Auto Connect on USB Plug-in",
                toggle="checkmark",
                toggle_state=1 if window.settings.auto_connect_usb else 0,
                action=lambda: window.activate_action("win.auto-connect", None),
            ),
            tray.MenuItem(
                TRAY_BLOCK_MICROSOFT,
                "Block Microsoft Connections",
                toggle="checkmark",
                toggle_state=1 if window.settings.block_microsoft else 0,
                action=lambda: window.activate_action("win.block-microsoft", None),
            ),
            tray.MenuItem(
                TRAY_THROTTLE,
                "Throttle Connection",
                toggle="checkmark",
                toggle_state=1 if window.settings.throttle_kbps > 0 else 0,
                action=lambda: window.activate_action("win.throttle", None),
            ),
            tray.MenuItem(5, kind="separator"),
            tray.MenuItem(TRAY_QUIT, "Quit PdaNet", action=window._quit),
        ]
        self.tray = tray.TrayIcon(
            __app_id__,
            __app_id__,
            "PdaNet Linux",
            items,
            on_activate=self._show_window,
        )
        window._sync_tray()

    def do_shutdown(self) -> None:  # noqa: N802
        if self.window is not None:
            self.window._cleanup_before_exit()
        if self.tray is not None:
            self.tray.close()
            self.tray = None
        # Chain up to the default implementation.
        Adw.Application.do_shutdown(self)


def main(argv: list[str] | None = None) -> int:
    app = PdaNetApp()
    return int(app.run(argv if argv is not None else []))
