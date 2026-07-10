#!/usr/bin/env python3
"""Launch PDF Preflight Viewer"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from viewer.main_window import PreflightWindow, main

if __name__ == "__main__":
    sys.exit(main())
