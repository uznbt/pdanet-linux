"""System tray icon for PdaNet Linux.

GTK4 has no portable system-tray API.  Instead of pulling GTK3 into the
process, this module implements the KDE ``StatusNotifierItem`` and
``com.canonical.dbusmenu`` protocols directly on the session bus.  It works
with waybar, QuickShell, KDE Plasma, the GNOME AppIndicator extension and
any other StatusNotifier host.

The tray is optional: if no session bus or no StatusNotifierWatcher is
available, the GUI falls back to normal window behaviour instead of hiding
an unreachable window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from gi.repository import Gio, GLib

SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
WATCHER_IFACE = "org.kde.StatusNotifierWatcher"

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <method name="ContextMenu">
      <arg type="i" direction="in"/><arg type="i" direction="in"/>
    </method>
    <method name="Activate">
      <arg type="i" direction="in"/><arg type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg type="i" direction="in"/><arg type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg type="i" direction="in"/><arg type="s" direction="in"/>
    </method>
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="u" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s"/></signal>
    <signal name="NewMenu"/>
  </interface>
</node>
"""

_MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <method name="GetLayout">
      <arg type="i" direction="in"/>
      <arg type="i" direction="in"/>
      <arg type="as" direction="in"/>
      <arg type="u" direction="out"/>
      <arg type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" direction="in"/>
      <arg type="as" direction="in"/>
      <arg type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" direction="in"/>
      <arg type="s" direction="in"/>
      <arg type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" direction="in"/>
      <arg type="s" direction="in"/>
      <arg type="v" direction="in"/>
      <arg type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" direction="in"/>
      <arg type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" direction="in"/>
      <arg type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" direction="in"/>
      <arg type="ai" direction="out"/>
      <arg type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})"/>
      <arg type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u"/>
      <arg type="i"/>
    </signal>
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
  </interface>
</node>
"""


@dataclass
class MenuItem:
    """One DBusMenu entry.

    ``kind`` is ``standard`` or ``separator``.  ``toggle`` may be
    ``checkmark`` or ``radio``; ``0`` means unchecked and ``1`` checked.
    """

    id: int
    label: str = ""
    action: Callable[[], None] | None = None
    enabled: bool = True
    visible: bool = True
    kind: str = "standard"
    toggle: str = ""
    toggle_state: int = 0
    children: list["MenuItem"] = field(default_factory=list)


def _walk(items: Iterable[MenuItem]) -> Iterable[MenuItem]:
    for item in items:
        yield item
        yield from _walk(item.children)


class TrayIcon:
    """A StatusNotifierItem with a DBusMenu."""

    def __init__(
        self,
        app_id: str,
        icon_name: str,
        title: str,
        items: list[MenuItem],
        *,
        on_activate: Callable[[], None] | None = None,
    ) -> None:
        self.app_id = app_id
        self.icon_name = icon_name
        self.overlay_icon_name = ""
        self.title = title
        self._root_items = items
        self._items = {item.id: item for item in _walk(items)}
        self._on_activate = on_activate
        self._revision = 1
        self._status_text = "Disconnected"
        self._available = False
        self._bus: Gio.DBusConnection | None = None
        self._watch_id = 0
        self._sni_registration = 0
        self._menu_registration = 0

        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return

        try:
            sni_info = Gio.DBusNodeInfo.new_for_xml(_SNI_XML).interfaces[0]
            menu_info = Gio.DBusNodeInfo.new_for_xml(_MENU_XML).interfaces[0]
            self._sni_registration = self._bus.register_object(
                SNI_PATH, sni_info, self._on_sni_call, self._on_sni_property, None
            )
            self._menu_registration = self._bus.register_object(
                MENU_PATH, menu_info, self._on_menu_call, self._on_menu_property, None
            )
        except GLib.Error:
            self._available = False
            return

        # Register synchronously so the first close request already knows
        # whether hiding the window is safe.  The watcher accepts either a
        # service name or an object path; the object-path form ties the
        # item to this connection and needs no extra well-known name.
        self._register_now()
        self._watch_id = Gio.bus_watch_name(
            Gio.BusType.SESSION,
            WATCHER_NAME,
            Gio.BusNameWatcherFlags.NONE,
            self._watcher_appeared,
            self._watcher_vanished,
        )

    @property
    def available(self) -> bool:
        return self._available

    # ------------------------------------------------------------------
    # StatusNotifierItem
    # ------------------------------------------------------------------
    def _on_sni_property(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _iface: str,
        name: str,
    ) -> GLib.Variant | None:
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", self.app_id),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("u", 0),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconPixmap": GLib.Variant("a(iiay)", []),
            "OverlayIconName": GLib.Variant("s", self.overlay_icon_name),
            "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionMovieName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant(
                "(sa(iiay)ss)", ("", [], self.title, self._status_text)
            ),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name)

    def _on_sni_call(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _iface: str,
        method: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        if method in ("Activate", "SecondaryActivate"):
            if self._on_activate is not None:
                self._on_activate()
            invocation.return_value(None)
            return
        if method in ("ContextMenu", "Scroll"):
            # Hosts use the Menu property for the context menu; a
            # ContextMenu call is only a hint that it was requested.
            invocation.return_value(None)
            return
        invocation.return_dbus_error(
            "org.freedesktop.DBus.Error.UnknownMethod",
            f"unknown method {method}",
        )

    # ------------------------------------------------------------------
    # DBusMenu
    # ------------------------------------------------------------------
    def _on_menu_property(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _iface: str,
        name: str,
    ) -> GLib.Variant | None:
        values = {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }
        return values.get(name)

    def _props(self, item: MenuItem | None) -> dict[str, GLib.Variant]:
        if item is None:
            return {}
        props: dict[str, GLib.Variant] = {
            "visible": GLib.Variant("b", item.visible),
            "enabled": GLib.Variant("b", item.enabled),
            "type": GLib.Variant("s", item.kind),
        }
        if item.kind == "standard":
            if item.label:
                props["label"] = GLib.Variant("s", item.label)
            if item.children:
                props["children-display"] = GLib.Variant("s", "submenu")
            if item.toggle:
                props["toggle-type"] = GLib.Variant("s", item.toggle)
                props["toggle-state"] = GLib.Variant("i", item.toggle_state)
        return props

    def _layout_variant(self, item: MenuItem) -> GLib.Variant:
        children = [
            self._layout_variant(child)
            for child in item.children
            if child.visible
        ]
        return GLib.Variant(
            "(ia{sv}av)", (item.id, self._props(item), children)
        )

    def _on_menu_call(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _iface: str,
        method: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        if method == "GetLayout":
            _parent_id, _depth, _names = parameters.unpack()
            root_children = [
                self._layout_variant(item)
                for item in self._root_items
                if item.visible
            ]
            layout = (0, {}, root_children)
            invocation.return_value(
                GLib.Variant("(u(ia{sv}av))", (self._revision, layout))
            )
            return
        if method == "GetGroupProperties":
            ids, _names = parameters.unpack()
            if not ids:
                ids = list(self._items)
            entries = [
                (item_id, self._props(self._items.get(item_id)))
                for item_id in ids
                if item_id in self._items
            ]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (entries,)))
            return
        if method == "GetProperty":
            item_id, name = parameters.unpack()
            props = self._props(self._items.get(item_id))
            value = props.get(name, GLib.Variant("s", ""))
            invocation.return_value(GLib.Variant("(v)", (value,)))
            return
        if method == "Event":
            item_id, event_id, _data, _timestamp = parameters.unpack()
            if event_id == "clicked":
                item = self._items.get(item_id)
                if item is not None and item.enabled and item.action is not None:
                    item.action()
            invocation.return_value(None)
            return
        if method == "EventGroup":
            events = parameters.unpack()[0]
            for event in events:
                item_id, event_id = event[0], event[1]
                if event_id == "clicked":
                    item = self._items.get(item_id)
                    if (
                        item is not None
                        and item.enabled
                        and item.action is not None
                    ):
                        item.action()
            invocation.return_value(GLib.Variant("(ai)", ([],)))
            return
        if method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (True,)))
            return
        if method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
            return
        invocation.return_dbus_error(
            "org.freedesktop.DBus.Error.UnknownMethod",
            f"unknown method {method}",
        )

    # ------------------------------------------------------------------
    # Public API used by the GUI
    # ------------------------------------------------------------------
    def set_status(self, text: str) -> None:
        self._status_text = text
        if self._bus is None or not self._available:
            return
        self._bus.emit_signal(
            None,
            SNI_PATH,
            SNI_IFACE,
            "NewToolTip",
            GLib.Variant(
                "(sa(iiay)ss)", ("", [], self.title, self._status_text)
            ),
        )

    def set_overlay_icon(self, icon_name: str) -> None:
        """Set an optional small overlay, e.g. cellular signal bars."""
        if self.overlay_icon_name == icon_name:
            return
        self.overlay_icon_name = icon_name
        if self._bus is None or not self._available:
            return
        self._bus.emit_signal(
            None,
            SNI_PATH,
            SNI_IFACE,
            "NewOverlayIcon",
            None,
        )

    def set_item_label(self, item_id: int, label: str) -> None:
        item = self._items.get(item_id)
        if item is None or item.label == label:
            return
        item.label = label
        self._refresh_layout()

    def update_toggle(self, item_id: int, checked: bool) -> None:
        item = self._items.get(item_id)
        if item is None or not item.toggle:
            return
        item.toggle_state = 1 if checked else 0
        self._refresh_layout()

    def set_item_enabled(self, item_id: int, enabled: bool) -> None:
        item = self._items.get(item_id)
        if item is None or item.enabled == enabled:
            return
        item.enabled = enabled
        self._refresh_layout()

    def _refresh_layout(self) -> None:
        self._revision += 1
        if self._bus is None or not self._available:
            return
        self._bus.emit_signal(
            None,
            MENU_PATH,
            MENU_IFACE,
            "LayoutUpdated",
            GLib.Variant("(ui)", (self._revision, 0)),
        )

    # ------------------------------------------------------------------
    # Watcher registration
    # ------------------------------------------------------------------
    def _register_now(self) -> None:
        if self._bus is None:
            return
        try:
            self._bus.call_sync(
                WATCHER_NAME,
                WATCHER_PATH,
                WATCHER_IFACE,
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (SNI_PATH,)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
        except GLib.Error:
            self._available = False
        else:
            self._available = True

    def _watcher_appeared(
        self, connection: Gio.DBusConnection, _name: str, _owner: str
    ) -> None:
        connection.call(
            WATCHER_NAME,
            WATCHER_PATH,
            WATCHER_IFACE,
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (SNI_PATH,)),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
            self._registered,
        )

    def _watcher_vanished(
        self, _connection: Gio.DBusConnection, _name: str
    ) -> None:
        self._available = False

    def _registered(
        self, connection: Gio.DBusConnection, result: Gio.AsyncResult
    ) -> None:
        try:
            connection.call_finish(result)
        except GLib.Error:
            self._available = False
        else:
            self._available = True
            self.set_status(self._status_text)

    def close(self) -> None:
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._bus is None:
            return
        if self._sni_registration:
            self._bus.unregister_object(self._sni_registration)
            self._sni_registration = 0
        if self._menu_registration:
            self._bus.unregister_object(self._menu_registration)
            self._menu_registration = 0
        self._available = False
