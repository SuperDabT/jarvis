"""Shared test setup: Qt offscreen and reduced motion.

GUI tests must never open real windows (a desktop session sets
QT_QPA_PLATFORM=wayland), and animations are off by default so tests are
deterministic; tests/test_gui_motion.py turns motion on where it checks it.
"""
import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["JARVIS_REDUCE_MOTION"] = "1"
