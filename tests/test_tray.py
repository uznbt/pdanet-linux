import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux import tray


class TrayMenuTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            tray.MenuItem(1, "Show PdaNet"),
            tray.MenuItem(2, kind="separator"),
            tray.MenuItem(
                30,
                "Auto Connect",
                toggle="checkmark",
                toggle_state=1,
            ),
        ]
        # Build the model without touching the session bus or D-Bus objects.
        self.icon = object.__new__(tray.TrayIcon)
        self.icon._root_items = self.items
        self.icon._items = {
            item.id: item for item in tray._walk(self.items)
        }

    def test_walk_includes_children(self):
        child = tray.MenuItem(4, "Nested")
        parent = tray.MenuItem(3, "Parent", children=[child])
        self.assertEqual(
            [item.id for item in tray._walk([parent, self.items[0]])],
            [3, 4, 1],
        )

    def test_standard_properties(self):
        props = self.icon._props(self.items[0])
        self.assertEqual(props["label"].get_string(), "Show PdaNet")
        self.assertEqual(props["type"].get_string(), "standard")
        self.assertNotIn("toggle-type", props)
        self.assertNotIn("children-display", props)

    def test_separator_properties(self):
        props = self.icon._props(self.items[1])
        self.assertEqual(props["type"].get_string(), "separator")
        self.assertNotIn("label", props)

    def test_toggle_properties(self):
        props = self.icon._props(self.items[2])
        self.assertEqual(props["toggle-type"].get_string(), "checkmark")
        self.assertEqual(props["toggle-state"].get_int32(), 1)

    def test_submenu_property(self):
        parent = tray.MenuItem(5, "Parent", children=[tray.MenuItem(6, "Child")])
        props = self.icon._props(parent)
        self.assertEqual(props["children-display"].get_string(), "submenu")

    def test_layout_skips_invisible_children(self):
        visible = tray.MenuItem(7, "Visible")
        hidden = tray.MenuItem(8, "Hidden", visible=False)
        parent = tray.MenuItem(5, "Parent", children=[visible, hidden])
        self.icon._items = {
            item.id: item for item in tray._walk([parent])
        }
        _item_id, _props, children = self.icon._layout_variant(parent).unpack()
        self.assertEqual([child[0] for child in children], [7])

    def test_missing_item_has_no_properties(self):
        self.assertEqual(self.icon._props(None), {})

    def test_set_item_label_refreshes_layout(self):
        self.icon._refresh_layout = mock.Mock()
        self.icon.set_item_label(30, "Auto Connect (on)")
        self.assertEqual(self.items[2].label, "Auto Connect (on)")
        self.icon._refresh_layout.assert_called_once_with()
        self.icon.set_item_label(30, "Auto Connect (on)")
        self.icon._refresh_layout.assert_called_once_with()

    def test_set_overlay_icon_emits_signal(self):
        self.icon.overlay_icon_name = ""
        self.icon._bus = mock.Mock()
        self.icon._available = True
        self.icon.set_overlay_icon("network-cellular-signal-good-symbolic")
        self.assertEqual(
            self.icon.overlay_icon_name,
            "network-cellular-signal-good-symbolic",
        )
        self.icon._bus.emit_signal.assert_called_once()
        name = self.icon._bus.emit_signal.call_args.args[3]
        self.assertEqual(name, "NewOverlayIcon")


if __name__ == "__main__":
    unittest.main()
