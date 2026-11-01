#!/usr/bin/env python3
"""
dsr_decoder_gui.py - Qt front end ("virtual DSR radio") for ./dsr_decoder

* connects the decoder to the encoder's TCP port (or to a recorded baseband file)
* shows the programme offer (station name, programme type, music/speech) as it is
  received over the air, and lets you tune with a click or with Prev / Next
* plays through the sound card (aplay or any player command) and/or records a WAV
* signal-quality indicators from the decoder's BCH statistics

How it works: the GUI starts ./dsr_decoder with -v and sends the decoder's run-time
commands ("l" = list, "5" = tune station 5, "+" / "-" = next / previous) through the
process' standard input; the answers are read from its log output.
"""
import json
import os
import re
import sys

from dsr_gui_common import (PTY_NAMES, QT_API, LogView, Led, QtCore, QtGui, QtWidgets, Qt,
                            cmdline_text, find_binary, make_app, stop_process)

DEFAULT_PLAYER = "aplay -q -t raw -f S16_LE -r 32000 -c 2 -"
PTY_ALT = "|".join(re.escape(p) for p in sorted(PTY_NAMES, key=len, reverse=True))
RE_HEADER = re.compile(r"^Programme offer \(frame lock (yes|no)(, A/B swapped)?, grid lock (yes|no)\)")
RE_STATION = re.compile(r"^\s*(\d+) ([ *]) (.*?)  (%s) +(music|speech)(.*)$" % PTY_ALT)
RE_STATS = re.compile(r"\[frames (\d+)\s+bch-corrected (\d+)\s+uncorrectable blocks (\d+)\s+audio blocks (\d+)\s+pi packets (\d+)\]")
RE_TUNE = re.compile(r"^station (\d+)(?:: (.*)| \(not announced.*)?$")


class DecoderWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DSR Decoder - virtual DSR radio")
        self.resize(1080, 820)
        self.settings = QtCore.QSettings("dsr-tools", "decoder_gui")

        self.proc = QtCore.QProcess(self)
        self.proc.readyReadStandardError.connect(self.on_output)
        self.proc.readyReadStandardOutput.connect(self.on_output)
        self.proc.started.connect(self.on_started)
        self.proc.finished.connect(self.on_finished)
        self.proc.errorOccurred.connect(self.on_error)
        self.buf = ""
        self.pending = None          # stations of the offer block currently being received
        self.stations = {}           # nr -> (name, pty, kind, mono)
        self.current = 1
        self.last_stats = None

        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        v = QtWidgets.QVBoxLayout(root)

        # ================= "radio front panel" =================
        panel = QtWidgets.QGroupBox("Receiver")
        pl = QtWidgets.QGridLayout(panel)
        self.disp_name = QtWidgets.QLabel("- - - - - - - -")
        self.disp_name.setAlignment(Qt.AlignCenter)
        self.disp_name.setMinimumHeight(86)
        self.disp_name.setStyleSheet(
            "background:#050805; color:#6bff7a; border:2px solid #2d3440; border-radius:8px;"
            "font-family:'DejaVu Sans Mono',monospace; font-size:44pt; font-weight:bold; letter-spacing:4px;")
        self.disp_info = QtWidgets.QLabel("not running")
        self.disp_info.setAlignment(Qt.AlignCenter)
        self.disp_info.setStyleSheet("color:#e6a23c; font-size:13pt;")
        self.btn_prev = QtWidgets.QPushButton("\u25c0  Prev")
        self.btn_next = QtWidgets.QPushButton("Next  \u25b6")
        self.btn_list = QtWidgets.QPushButton("\u21bb  Refresh list")
        self.btn_prev.clicked.connect(lambda: self.send("-"))
        self.btn_next.clicked.connect(lambda: self.send("+"))
        self.btn_list.clicked.connect(lambda: self.send("l"))
        pl.addWidget(self.disp_name, 0, 0, 1, 5)
        pl.addWidget(self.disp_info, 1, 0, 1, 5)
        pl.addWidget(self.btn_prev, 2, 0)
        pl.addWidget(self.btn_next, 2, 1)
        pl.addWidget(self.btn_list, 2, 2)
        pl.setColumnStretch(4, 1)
        v.addWidget(panel)

        # ================= programme list + status =================
        mid = QtWidgets.QHBoxLayout()
        gl = QtWidgets.QGroupBox("Programme offer (click to tune)")
        gll = QtWidgets.QVBoxLayout(gl)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Nr", "Name", "Programme type", "Kind"])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 150)
        self.table.cellClicked.connect(self.on_row_clicked)
        gll.addWidget(self.table)
        mid.addWidget(gl, 3)

        sg = QtWidgets.QGroupBox("Signal")
        sl = QtWidgets.QVBoxLayout(sg)
        self.led_proc = Led("decoder process")
        self.led_frame = Led("frame lock")
        self.led_grid = Led("block lock (2 ms grid)")
        self.led_swap = Led("A/B swapped (mirrored spectrum)")
        self.led_conn = Led("stream")
        self.quality = QtWidgets.QProgressBar()
        self.quality.setRange(0, 100)
        self.quality.setValue(0)
        self.quality.setFormat("signal quality %p %")
        self.lbl_stats = QtWidgets.QLabel("")
        self.lbl_stats.setObjectName("hint")
        self.lbl_stats.setWordWrap(True)
        for w in (self.led_proc, self.led_conn, self.led_frame, self.led_grid, self.led_swap):
            sl.addWidget(w)
        sl.addSpacing(6)
        sl.addWidget(self.quality)
        sl.addSpacing(4)
        sl.addWidget(self.lbl_stats)
        sl.addStretch(1)
        mid.addWidget(sg, 2)
        v.addLayout(mid, 3)

        # ================= settings =================
        st = QtWidgets.QGroupBox("Source and audio")
        g = QtWidgets.QGridLayout(st)
        self.rb_tcp = QtWidgets.QRadioButton("Encoder (TCP)")
        self.rb_file = QtWidgets.QRadioButton("Baseband file")
        self.rb_tcp.setChecked(True)
        self.host = QtWidgets.QLineEdit("127.0.0.1")
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(5000)
        self.infile = QtWidgets.QLineEdit()
        bi = QtWidgets.QToolButton()
        bi.setText("...")
        bi.clicked.connect(self.browse_in)
        self.fmt = QtWidgets.QComboBox()
        self.fmt.addItems(["cs16", "cs8", "cf32", "real16"])
        self.sps = QtWidgets.QComboBox()
        self.sps.addItems(["auto", "2", "4", "8"])
        self.start_st = QtWidgets.QSpinBox()
        self.start_st.setRange(1, 16)
        self.chk_play = QtWidgets.QCheckBox("Play on sound card:")
        self.chk_play.setChecked(True)
        self.player = QtWidgets.QLineEdit(DEFAULT_PLAYER)
        self.player.setToolTip("Any command that reads raw S16_LE / 32 kHz / stereo from stdin")
        self.chk_rec = QtWidgets.QCheckBox("Record WAV:")
        self.recfile = QtWidgets.QLineEdit("dsr_record.wav")
        br = QtWidgets.QToolButton()
        br.setText("...")
        br.clicked.connect(self.browse_rec)
        self.bin_edit = QtWidgets.QLineEdit(find_binary("dsr_decoder"))
        bb = QtWidgets.QToolButton()
        bb.setText("...")
        bb.clicked.connect(self.browse_bin)

        g.addWidget(self.rb_tcp, 0, 0)
        g.addWidget(self.host, 0, 1)
        g.addWidget(QtWidgets.QLabel("port"), 0, 2)
        g.addWidget(self.port, 0, 3)
        g.addWidget(QtWidgets.QLabel("format"), 0, 4)
        g.addWidget(self.fmt, 0, 5)
        g.addWidget(self.rb_file, 1, 0)
        g.addWidget(self.infile, 1, 1, 1, 3)
        g.addWidget(bi, 1, 3 + 1)
        g.addWidget(QtWidgets.QLabel("samples/symbol"), 1, 5)
        g.addWidget(self.sps, 1, 6)
        g.addWidget(QtWidgets.QLabel("start station"), 0, 6)
        g.addWidget(self.start_st, 0, 7)
        g.addWidget(self.chk_play, 2, 0)
        g.addWidget(self.player, 2, 1, 1, 6)
        g.addWidget(self.chk_rec, 3, 0)
        g.addWidget(self.recfile, 3, 1, 1, 5)
        g.addWidget(br, 3, 6)
        g.addWidget(QtWidgets.QLabel("dsr_decoder"), 4, 0)
        g.addWidget(self.bin_edit, 4, 1, 1, 5)
        g.addWidget(bb, 4, 6)
        g.setColumnStretch(1, 1)
        v.addWidget(st)

        run = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton("\u25b6  Power on / connect")
        self.btn_start.setObjectName("start")
        self.btn_stop = QtWidgets.QPushButton("\u25a0  Power off")
        self.btn_stop.setObjectName("stop")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.stop)
        self.cmd_view = QtWidgets.QLineEdit()
        self.cmd_view.setReadOnly(True)
        run.addWidget(self.btn_start)
        run.addWidget(self.btn_stop)
        run.addWidget(self.cmd_view, 1)
        v.addLayout(run)

        self.log = LogView()
        self.log.setMinimumHeight(90)
        v.addWidget(self.log, 1)
        self.statusBar().showMessage("Qt binding: " + QT_API)

        self.poll = QtCore.QTimer(self)                 # ask the decoder for the programme list
        self.poll.timeout.connect(lambda: self.send("l", quiet=True))
        self.commit_timer = QtCore.QTimer(self)
        self.commit_timer.setSingleShot(True)
        self.commit_timer.timeout.connect(self.commit_offer)

        self.load_settings()
        self.set_running(False)

    # ------------------------------------------------------------------ settings dialogs
    def browse_in(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Baseband file", self.infile.text())
        if p:
            self.infile.setText(p)
            self.rb_file.setChecked(True)

    def browse_rec(self):
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "WAV file", self.recfile.text(), "WAV (*.wav)")
        if p:
            self.recfile.setText(p)
            self.chk_rec.setChecked(True)

    def browse_bin(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "dsr_decoder binary", self.bin_edit.text())
        if p:
            self.bin_edit.setText(p)

    # ------------------------------------------------------------------ process control
    def build_args(self):
        if self.rb_tcp.isChecked():
            src = "tcp://%s:%d" % (self.host.text().strip() or "127.0.0.1", self.port.value())
        else:
            if not os.path.isfile(self.infile.text().strip()):
                return None, "Please choose an existing baseband file."
            src = self.infile.text().strip()
        if not (self.chk_play.isChecked() or self.chk_rec.isChecked()):
            return None, "Enable sound output and/or WAV recording."
        fmt = self.fmt.currentText()
        args = ["-i", src, "-f", fmt, "-s", str(self.start_st.value()), "-v"]
        if self.sps.currentText() != "auto":
            args += ["--sps", self.sps.currentText()]
        if self.chk_play.isChecked():
            args.append("--play=" + (self.player.text().strip() or DEFAULT_PLAYER))   # optional argument needs '='
        if self.chk_rec.isChecked():
            args += ["-o", self.recfile.text().strip()]
        return args, None

    def start(self):
        prog = self.bin_edit.text().strip()
        if not (os.path.isfile(prog) and os.access(prog, os.X_OK)):
            QtWidgets.QMessageBox.warning(self, "dsr_decoder", "Cannot execute %r.\nBuild it first with 'make'." % prog)
            return
        args, err = self.build_args()
        if err:
            QtWidgets.QMessageBox.warning(self, "Cannot start", err)
            return
        self.save_settings()
        self.cmd_view.setText(cmdline_text(prog, args))
        self.log.add("$ " + self.cmd_view.text())
        self.buf = ""
        self.stations.clear()
        self.fill_table()
        self.current = self.start_st.value()
        self.last_stats = None
        self.proc.start(prog, args)

    def stop(self):
        self.poll.stop()
        stop_process(self.proc, "q\n")

    def on_started(self):
        self.set_running(True)
        self.disp_name.setText("- - - - - - - -")
        self.disp_info.setText("connecting / searching for signal ...")
        self.led_conn.set_state("warn", "waiting for the encoder / signal")
        self.poll.start(1500)
        QtCore.QTimer.singleShot(700, lambda: self.send("l", quiet=True))

    def on_finished(self, code, status):
        self.poll.stop()
        self.log.add("decoder finished (exit code %s)" % code)
        self.set_running(False)

    def on_error(self, err):
        if err == QtCore.QProcess.FailedToStart:
            QtWidgets.QMessageBox.critical(self, "dsr_decoder", "The program could not be started.")

    def send(self, text, quiet=False):
        if self.proc.state() != QtCore.QProcess.Running:
            return
        self.proc.write((text + "\n").encode())
        if not quiet and text.isdigit():                 # optimistic display while the decoder confirms
            self.current = int(text)
            self.update_display()

    def set_running(self, running):
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        for b in (self.btn_prev, self.btn_next, self.btn_list):
            b.setEnabled(running)
        self.led_proc.set_state("ok" if running else "off")
        if not running:
            for led in (self.led_conn, self.led_frame, self.led_grid, self.led_swap):
                led.set_state("off")
            self.quality.setValue(0)
            self.disp_name.setText("- - - - - - - -")
            self.disp_info.setText("not running")
        self.statusBar().showMessage(("Running" if running else "Stopped") + "  |  Qt binding: " + QT_API)

    # ------------------------------------------------------------------ decoder output
    def on_output(self):
        for ch in (self.proc.readAllStandardError(), self.proc.readAllStandardOutput()):
            self.buf += bytes(ch).decode("utf-8", "replace")
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.handle_line(line.rstrip("\r"))

    def handle_line(self, line):
        m = RE_HEADER.match(line)
        if m:
            self.pending = []
            frame, swap, grid = m.group(1) == "yes", bool(m.group(2)), m.group(3) == "yes"
            self.led_frame.set_state("ok" if frame else "warn")
            self.led_grid.set_state("ok" if grid else "warn")
            self.led_swap.set_state("warn" if swap else "off",
                                    "Spectrum is mirrored; the decoder corrects this automatically" if swap else "")
            if frame:
                self.led_conn.set_state("ok", "signal present")
            self.commit_timer.start(250)
            return
        m = RE_STATION.match(line)
        if m and self.pending is not None:
            nr = int(m.group(1))
            self.pending.append((nr, m.group(3).strip(), m.group(4), m.group(5), bool(m.group(6).strip())))
            if m.group(2) == "*":
                self.current = nr
            self.commit_timer.start(250)
            return
        if line.startswith("  (no programme announced"):
            self.commit_timer.start(250)
            return
        m = RE_TUNE.match(line)
        if m:
            self.current = int(m.group(1))
            self.update_display()
            self.log.add(line)
            return
        m = RE_STATS.search(line)
        if m:
            self.on_stats(*[int(x) for x in m.groups()])
            return
        if "connection lost" in line:
            self.led_conn.set_state("bad", "connection to the encoder lost - reconnecting")
        elif "reconnected" in line:
            self.led_conn.set_state("ok")
        if "PI packet" in line or not line.strip():
            return
        self.log.add(line)

    def commit_offer(self):
        if self.pending is None:
            return
        self.stations = {nr: (name, pty, kind, mono) for nr, name, pty, kind, mono in self.pending}
        self.pending = None
        self.fill_table()
        self.update_display()

    def on_stats(self, frames, corrected, uncorr, audio, pi):
        txt = "frames %d   BCH corrected %d   uncorrectable %d   audio blocks %d" % (frames, corrected, uncorr, audio)
        q = 100
        if self.last_stats:
            df, du = frames - self.last_stats[0], uncorr - self.last_stats[1]
            if df > 0:
                q = int(100 * (1 - min(1.0, (du / (16.0 * df)) * 50)))
        self.last_stats = (frames, uncorr)
        self.quality.setValue(max(0, min(100, q)))
        self.lbl_stats.setText(txt)

    # ------------------------------------------------------------------ list / display
    def fill_table(self):
        self.table.setRowCount(len(self.stations))
        for i, nr in enumerate(sorted(self.stations)):
            name, pty, kind, mono = self.stations[nr]
            vals = ["%d" % nr, name + ("  (2x mono)" if mono else ""), pty, kind]
            for c, t in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(t)
                it.setData(Qt.UserRole, nr)
                if nr == self.current:
                    it.setForeground(QtGui.QBrush(QtGui.QColor("#6bff7a")))
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                self.table.setItem(i, c, it)
            if nr == self.current:
                self.table.selectRow(i)

    def update_display(self):
        if self.proc.state() != QtCore.QProcess.Running:
            return
        info = self.stations.get(self.current)
        if info:
            name, pty, kind, mono = info
            self.disp_name.setText(name.ljust(8)[:8].replace(" ", "\u00a0"))
            self.disp_info.setText("Station %02d   |   %s   |   %s%s" % (self.current, pty, kind, "   |   2 x mono" if mono else ""))
        else:
            self.disp_name.setText("- - - - - - - -")
            self.disp_info.setText("Station %02d   |   not announced (yet)" % self.current)
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.data(Qt.UserRole) == self.current:
                self.table.selectRow(r)
        self.fill_table()

    def on_row_clicked(self, row, col):
        it = self.table.item(row, 0)
        if it:
            self.send(str(it.data(Qt.UserRole)))

    # ------------------------------------------------------------------ persistence
    def save_settings(self):
        cfg = {"tcp": self.rb_tcp.isChecked(), "host": self.host.text(), "port": self.port.value(),
               "file": self.infile.text(), "fmt": self.fmt.currentText(), "sps": self.sps.currentText(),
               "start": self.start_st.value(), "play": self.chk_play.isChecked(), "player": self.player.text(),
               "rec": self.chk_rec.isChecked(), "recfile": self.recfile.text(), "bin": self.bin_edit.text()}
        self.settings.setValue("config", json.dumps(cfg))

    def load_settings(self):
        raw = self.settings.value("config")
        if not raw:
            return
        try:
            c = json.loads(raw)
            (self.rb_tcp if c.get("tcp", True) else self.rb_file).setChecked(True)
            self.host.setText(c.get("host", "127.0.0.1"))
            self.port.setValue(c.get("port", 5000))
            self.infile.setText(c.get("file", ""))
            self.fmt.setCurrentText(c.get("fmt", "cs16"))
            self.sps.setCurrentText(c.get("sps", "auto"))
            self.start_st.setValue(c.get("start", 1))
            self.chk_play.setChecked(c.get("play", True))
            self.player.setText(c.get("player", DEFAULT_PLAYER))
            self.chk_rec.setChecked(c.get("rec", False))
            self.recfile.setText(c.get("recfile", "dsr_record.wav"))
            if c.get("bin") and os.path.isfile(c["bin"]):
                self.bin_edit.setText(c["bin"])
        except (ValueError, TypeError):
            pass

    def closeEvent(self, ev):
        self.save_settings()
        self.poll.stop()
        stop_process(self.proc, "q\n")
        ev.accept()


def main():
    app = make_app("DSR Decoder")
    w = DecoderWindow()
    w.show()
    sys.exit(app.exec_() if hasattr(app, "exec_") else app.exec())


if __name__ == "__main__":
    main()
