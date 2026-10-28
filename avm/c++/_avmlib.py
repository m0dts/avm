"""The one AVM C++ library (avm.dll / libavm.so beside this file, built by
build.py): every wrapper here loads it through lib(), so there is a single
copy in the process."""
import ctypes
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "avm.dll" if sys.platform == "win32" else "libavm.so"
_lib = None


def lib():
    global _lib
    if _lib is None:
        path = os.path.join(_HERE, NAME)
        if not os.path.exists(path):
            raise OSError(f"{NAME} not found beside {__file__}: build it with build.py")
        _lib = ctypes.CDLL(path)
    return _lib
