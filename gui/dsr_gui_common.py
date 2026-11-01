"""
dsr_gui_common.py - helpers shared by the DSR encoder and decoder GUIs.

Both GUIs are thin "remote controls": they never touch the signal themselves, they
build a command line for the console programs (dsr_encoder / dsr_decoder), start
them as child processes (QProcess), show their log output and - for the decoder -
send run-time commands to them through standard input.

Qt binding: PyQt5 is used if installed, otherwise PySide6.
    Debian 13:  sudo apt install python3-pyqt5
"""
import os
import shlex
import shutil
import sys

try:                                           # preferred binding
    from PyQt5 import QtCore, QtGui, QtWidgets
    QT_API = "PyQt5"
except ImportError:                            # fallback
    from PySide6 import QtCore, QtGui, QtWidgets
    QT_API = "PySide6"

Qt = QtCore.Qt

# Programme types of the DSR programme offer (identical to the RDS PTY table, spec 3.4.2.1)
PTY_NAMES = ["KEINE", "NACHRICH", "POLITIK", "SPEZWORT", "SPORT", "LERNEN", "HOER+LIT",
             "KULTUR", "WISSEN", "UNTERHAL", "POP", "ROCK", "U-MUSIK", "L-KLASS",
             "E-KLASS", "SPEZ MUS"]
PTY_LONG = ["no programme type", "News", "Politics / current affairs", "Special speech",
            "Sport", "Education", "Drama / literature", "Culture, church, society",
            "Science", "Light speech", "Pop music", "Rock music", "Easy listening",
            "Light classical", "Serious classical", "Special music"]

# Output sample formats of the encoder: name -> (bytes per sample, description)
FORMATS = {
    "cs16":   (4, "I/Q signed 16 bit (default)"),
    "cs8":    (2, "I/Q signed 8 bit (half data rate)"),
    "cf32":   (8, "I/Q float 32 bit"),
    "real16": (2, "real IF at fs/4, signed 16 bit"),
}
SYMBOL_RATE = 10.24e6


def find_binary(name):
    """Look for a console program next to the GUI, one level up, in cwd and in PATH."""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(here, ".."), here, os.getcwd()):
        p = os.path.abspath(os.path.join(d, name))
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which(name) or os.path.abspath(os.path.join(here, "..", name))


def cmdline_text(program, args):
    """Shell-quoted command line, for display and copy & paste."""
    return shlex.join([program] + list(args))


DARK_QSS = """
* { font-family: "Noto Sans", "DejaVu Sans", sans-serif; font-size: 10pt; }
QWidget { background: #14171c; color: #d8dee9; }
QGroupBox { border: 1px solid #2d3440; border-radius: 6px; margin-top: 14px; padding: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #e6a23c; font-weight: bold; }
QPushButton { background: #232a34; border: 1px solid #364051; border-radius: 5px; padding: 6px 14px; }
QPushButton:hover { background: #2c3541; border-color: #e6a23c; }
QPushButton:disabled { color: #5a6270; border-color: #232a34; }
QPushButton#start { background: #1f6f3f; border-color: #2fa05d; font-weight: bold; }
QPushButton#start:hover { background: #26884d; }
QPushButton#stop { background: #7a2b2b; border-color: #b04444; font-weight: bold; }
QPushButton#stop:hover { background: #983636; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #1c2128; border: 1px solid #364051;
                                                 border-radius: 4px; padding: 3px 6px; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #e6a23c; }
QComboBox QAbstractItemView { background: #1c2128; selection-background-color: #3b4658; }
QTableWidget { background: #181c22; gridline-color: #262d38; border: 1px solid #2d3440;
               alternate-background-color: #1b2027; selection-background-color: #3b4658; }
QHeaderView::section { background: #232a34; color: #e6a23c; border: 0; padding: 4px; font-weight: bold; }
QPlainTextEdit { background: #0e1115; color: #9fb4c7; font-family: "DejaVu Sans Mono", monospace;
                 font-size: 9pt; border: 1px solid #2d3440; }
QCheckBox::indicator, QRadioButton::indicator { width: 14px; height: 14px; border: 1px solid #6b7686;
                                                 background: #1c2128; border-radius: 3px; }
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background: #e6a23c; border-color: #e6a23c; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: #e6a23c; }
QProgressBar { border: 1px solid #364051; border-radius: 4px; text-align: center; background: #1c2128; }
QProgressBar::chunk { background: #2fa05d; }
QStatusBar { color: #8b95a5; }
QLabel#hint { color: #8b95a5; }
"""


class Led(QtWidgets.QLabel):
    """Small round status lamp with a text label."""
    COLORS = {"off": "#3a414d", "ok": "#3fb950", "warn": "#e6a23c", "bad": "#e5534b"}

    def __init__(self, text=""):
        super().__init__()
        self._text = text
        self.set_state("off")

    def set_state(self, state, tip=None):
        c = self.COLORS.get(state, self.COLORS["off"])
        self.setText('<span style="color:%s; font-size:15pt;">&#9679;</span> %s' % (c, self._text))
        if tip is not None:
            self.setToolTip(tip)


class LogView(QtWidgets.QPlainTextEdit):
    """Read-only log window with a line limit."""

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(2000)

    def add(self, text):
        for line in text.splitlines():
            if line.strip():
                self.appendPlainText(line)


def make_app(title):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName(title)
    app.setStyleSheet(DARK_QSS)
    return app


def stop_process(proc, quit_text=None, grace_ms=1500):
    """Politely stop a QProcess: optional quit command, then SIGTERM, finally SIGKILL."""
    if proc.state() == QtCore.QProcess.NotRunning:
        return
    if quit_text:
        proc.write(quit_text.encode())
        proc.waitForBytesWritten(200)
        if proc.waitForFinished(grace_ms):
            return
    proc.terminate()
    if not proc.waitForFinished(grace_ms):
        proc.kill()
        proc.waitForFinished(1000)
