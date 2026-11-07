"""Tests for the application icon lookup used by the welcome dialog."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from pdanet_linux import __app_id__, dialogs


class AppIconTests(unittest.TestCase):
    def test_source_icon_is_found(self) -> None:
        path = dialogs._app_icon_file()
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(path.endswith((".svg", ".png")))

    def test_xdg_icon_is_found_without_a_theme_cache(self) -> None:
        # Simulate a stale/absent icon-theme cache: no display, no source
        # data directory, but a valid icon in XDG_DATA_DIRS.
        real_source = os.path.join(
            os.path.dirname(dialogs.__file__), os.pardir, "data",
            f"{__app_id__}.svg",
        )
        with tempfile.TemporaryDirectory() as tmp:
            icon_dir = os.path.join(
                tmp, "icons", "hicolor", "scalable", "apps"
            )
            os.makedirs(icon_dir)
            shutil.copy(real_source, os.path.join(icon_dir, f"{__app_id__}.svg"))
            fake_module = os.path.join(tmp, "pdanet_linux", "dialogs.py")
            with mock.patch.object(dialogs, "__file__", fake_module), \
                    mock.patch.object(
                        dialogs.Gdk.Display, "get_default", return_value=None
                    ), \
                    mock.patch.dict(
                        os.environ,
                        {"XDG_DATA_DIRS": tmp, "XDG_DATA_HOME": tmp},
                        clear=False,
                    ):
                path = dialogs._app_icon_file()
            self.assertIsNotNone(path)
            assert path is not None
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(path.startswith(tmp))


if __name__ == "__main__":
    unittest.main()
