#!/usr/bin/env python3
"""Auto-generated stub — loads native binary for current platform."""
import importlib.util as _ilu
import platform as _pm
import sys as _sys
import sysconfig
from pathlib import Path as _Path

_plat = {"Darwin": "darwin", "Windows": "win32", "Linux": "linux"}.get(
    _pm.system(), _pm.system().lower()
)
# macOS: architecture-specific dir (darwin-arm64, darwin-x86_64)
if _plat == "darwin":
    _plat_name = f"darwin-{_pm.machine()}"
else:
    _plat_name = _plat
_native_dir = _Path(__file__).resolve().parent / "lib/_native" / _plat_name
# Fallback to generic platform dir if arch-specific not found
if not _native_dir.is_dir():
    _native_dir = _Path(__file__).resolve().parent / "lib/_native" / _plat
_ext_suffix = sysconfig.get_config_var("EXT_SUFFIX")
_binary = None
# Try exact EXT_SUFFIX first (e.g., .cpython-312-darwin.so)
_f = _native_dir / ("cloud_probe" + _ext_suffix)
if _f.exists():
    _binary = _f
else:
    # Fallback: search for ABI-compatible file only
    _py_tag = f"cpython-{_sys.version_info.major}{_sys.version_info.minor}"
    _bare_fallback = None
    for _p in _native_dir.glob("cloud_probe.*"):
        if _p.suffix not in (".so", ".pyd"):
            continue
        _name = _p.name
        if _py_tag in _name:
            _binary = _p
            break
        # Bare .so/.pyd with no cpython tag — last resort
        if "cpython" not in _name and _bare_fallback is None:
            _bare_fallback = _p
    if _binary is None and _bare_fallback is not None:
        _binary = _bare_fallback

if _binary:
    _spec = _ilu.spec_from_file_location(__name__, str(_binary))
    if _spec and _spec.loader:
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        for _n in dir(_mod):
            if not _n.startswith("__"):
                globals()[_n] = getattr(_mod, _n)
else:
    raise ImportError(f"No native binary for cloud_probe on {_plat_name}")
