"""Native package metadata helpers.

The application owns this top-level package name, while Celery/Kombu and
other dependencies import the third-party ``packaging`` distribution. Extend
the package search path so both namespaces remain importable in the runtime.
"""

from __future__ import annotations

import sysconfig
from pathlib import Path


for _site in (sysconfig.get_paths().get("purelib"), sysconfig.get_paths().get("platlib")):
    _external = Path(_site or "") / "packaging"
    if _external.is_dir() and str(_external) not in __path__:
        __path__.append(str(_external))
