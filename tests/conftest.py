import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for sub in ("shared", "backend", "worker"):
    path = os.path.join(ROOT, sub)
    if path not in sys.path:
        sys.path.insert(0, path)
