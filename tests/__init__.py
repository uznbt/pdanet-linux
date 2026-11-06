import os
import sys

# Ensure the parent directory (pdanet-linux) is in sys.path for test discovery
_pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _pkg_root not in sys.path:
    sys.path.insert(0, _pkg_root)

