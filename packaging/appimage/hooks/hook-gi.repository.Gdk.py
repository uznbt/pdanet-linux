# The built-in Gdk hook defaults to Gdk 3.0 because it follows the built-in
# Gtk hook.  This project is GTK 4 only, so collect Gdk 4.0 explicitly.
from PyInstaller.utils.hooks.gi import GiModuleInfo

module_info = GiModuleInfo("Gdk", "4.0")
if module_info.available:
    binaries, datas, hiddenimports = module_info.collect_typelib_data()
    hiddenimports += ["gi._gi_cairo", "gi.repository.cairo"]
