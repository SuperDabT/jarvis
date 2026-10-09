"""Shared test setup: Qt offscreen and reduced motion.

GUI tests must never open real windows (a desktop session sets
QT_QPA_PLATFORM=wayland), and animations are off by default so tests are
deterministic; tests/test_gui_motion.py turns motion on where it checks it.
"""
import os
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["JARVIS_REDUCE_MOTION"] = "1"
# UI settings (auto-listen) go to a scratch file, never the user's ~/.config/jarvis.
os.environ["JARVIS_UI_SETTINGS"] = os.path.join(tempfile.mkdtemp(prefix="jarvis-ui-"),
                                                "ui.ini")
