# PyInstaller's built-in GTK hook collects GTK 3 by default.  This project
# uses GTK 4, so collect the GTK 4 typelib and let its dependencies pull in
# Gdk, Gsk, Pango, Graphene and the rest.  Icons, themes and translations are
# handled explicitly by tools/build-appimage.sh instead of bundling every
# theme installed on the build machine.
from PyInstaller.utils.hooks.gi import GiModuleInfo

module_info = GiModuleInfo("Gtk", "4.0")
if module_info.available:
    binaries, datas, hiddenimports = module_info.collect_typelib_data()
