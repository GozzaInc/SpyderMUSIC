# -*- coding: utf-8 -*-
"""PyInstaller Windows GUI runtime hook for multiprocessing child processes.

With console=False, sys.stdout/sys.stderr may be None. ChromaDB's separate
spawned worker prints startup messages and Python's multiprocessing bootstrap
also writes tracebacks to stderr. Provide valid writable sinks without adding
a visible console window.
"""
import os
import sys

for _name in ("stdout", "stderr"):
    if getattr(sys, _name, None) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8", buffering=1))
