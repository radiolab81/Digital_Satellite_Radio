#!/usr/bin/env python3
"""
dsr_encoder_gui.py - Qt front end ("virtual DSR transmitter") for ./dsr_encoder

* up to 16 stations: name (SK), programme type, sub type, speech/music, audio source
  (file, internet stream URL, test tone)
* output format (cs16 / cs8 / cf32 / real16), destination (local TCP port or file)
* start / stop / apply (restart), live log, connected-decoder counter
* station list import/export as CSV, settings are remembered between sessions

The GUI only builds the command line, starts ./dsr_encoder and shows its output.
Changes to the stations need a restart of the encoder ("Apply"); decoders connected
over TCP reconnect automatically.
"""
import csv
import json
import os
import re
import sys
import time

from dsr_gui_common import (FORMATS, PTY_LONG, PTY_NAMES, SYMBOL_RATE, QT_API, LogView, Led,
                            QtCore, QtGui, QtWidgets, Qt, cmdline_text, find_binary, make_app,
                            stop_process)

N_STATIONS = 16
CSV_HEADER = ["nr", "active", "name", "pty", "sub", "kind", "source"]


# ---------------------------------------------------------------------------
# One table row = one station slot
# ---------------------------------------------------------------------------
class StationRow:
    def __init__(self, table, row):
        self.row = row
        self.chk = QtWidgets.QCheckBox()
        box = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(box)
        lay.addWidget(self.chk)
        lay.setAlignment(Qt.AlignCenter)
        lay.setContentsMargins(0, 0, 0, 0)
        table.setCellWidget(row, 0, box)

        item = QtWidgets.QTableWidgetItem(str(row + 1))
        item.setFlags(Qt.ItemIsEnabled)
        item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, 1, item)

        self.name = QtWidgets.QLineEdit()
        self.name.setMaxLength(8)
        self.name.setPlaceholderText("STATION %d" % (row + 1))
        self.name.setToolTip("Station name (SK): 8 characters shown on the receiver display")
        table.setCellWidget(row, 2, self.name)

        self.pty = QtWidgets.QComboBox()
        for i, (s, l) in enumerate(zip(PTY_NAMES, PTY_LONG)):
            self.pty.addItem("%2d  %s" % (i, s))
            self.pty.setItemData(i, l, Qt.ToolTipRole)
        table.setCellWidget(row, 3, self.pty)

        self.sub = QtWidgets.QComboBox()
        self.sub.addItem("= main")
        for i, s in enumerate(PTY_NAMES):
            self.sub.addItem("%2d  %s" % (i, s))
        self.sub.setToolTip("Second programme type, e.g. Sport/Pop. '= main' repeats the main type.")
        table.setCellWidget(row, 4, self.sub)

        self.kind = QtWidgets.QComboBox()
        self.kind.addItems(["music", "speech"])
        table.setCellWidget(row, 5, self.kind)

        self.src = QtWidgets.QLineEdit()
        self.src.setPlaceholderText("file, http(s)://stream, tone:440,660 ...")
        self.src.setToolTip("Anything ffmpeg can read (wav, mp3, aac, URLs), or tone:FL[,FR], or raw:PATH")
        btn = QtWidgets.QToolButton()
        btn.setText("...")
        btn.clicked.connect(self.browse)
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.src, 1)
        h.addWidget(btn)
        table.setCellWidget(row, 6, w)

    def browse(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(
            None, "Audio file", os.path.dirname(self.src.text()) or "",
            "Audio (*.wav *.mp3 *.flac *.ogg *.opus *.m4a *.aac);;All files (*)")
        if p:
            self.src.setText(p)
            self.chk.setChecked(True)
            if not self.name.text():
                self.name.setText(os.path.splitext(os.path.basename(p))[0][:8])

    def is_free(self):
        return not self.chk.isChecked() and not self.src.text().strip()

    def clear(self):
        self.chk.setChecked(False)
        self.name.clear()
        self.pty.setCurrentIndex(0)
        self.sub.setCurrentIndex(0)
        self.kind.setCurrentIndex(0)
        self.src.clear()

    def to_dict(self):
        return {"nr": self.row + 1, "active": self.chk.isChecked(), "name": self.name.text(),
                "pty": self.pty.currentIndex(), "sub": self.sub.currentIndex() - 1,
                "kind": self.kind.currentText(), "source": self.src.text().strip()}

    def from_dict(self, d):
        self.chk.setChecked(bool(d.get("active")))
        self.name.setText(str(d.get("name", ""))[:8])
        self.pty.setCurrentIndex(max(0, min(15, int(d.get("pty") or 0))))
        sub = d.get("sub", -1)
        sub = -1 if sub in ("", None) else int(sub)
        self.sub.setCurrentIndex(max(0, min(16, sub + 1)))
        self.kind.setCurrentIndex(1 if d.get("kind") == "speech" else 0)
        self.src.setText(str(d.get("source", "")))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class EncoderWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DSR Encoder - virtual transmitter")
        self.resize(1180, 980)
        self.settings = QtCore.QSettings("dsr-tools", "encoder_gui")
        self.proc = QtCore.QProcess(self)
        self.proc.readyReadStandardError.connect(self.on_output)
        self.proc.readyReadStandardOutput.connect(self.on_output)
        self.proc.started.connect(self.on_started)
        self.proc.finished.connect(self.on_finished)
        self.proc.errorOccurred.connect(self.on_error)
        self.t_start = 0.0
        self.restart_pending = False

        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        v = QtWidgets.QVBoxLayout(root)

        # ---- station list ----
        gb = QtWidgets.QGroupBox("Stations (up to 16 stereo programmes)")
        vl = QtWidgets.QVBoxLayout(gb)
        bar = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("Add station  \u25be")
        menu = QtWidgets.QMenu(self)
        menu.addAction("Audio file ...", self.add_file)
        menu.addAction("Internet stream URL ...", self.add_url)
        menu.addAction("Test tone ...", self.add_tone)
        self.preset_menu = menu.addMenu("From presets")
        self.btn_add.setMenu(menu)
        btn_rm = QtWidgets.QPushButton("Remove selected")
        btn_rm.clicked.connect(self.remove_selected)
        btn_clr = QtWidgets.QPushButton("Clear all")
        btn_clr.clicked.connect(self.clear_all)
        btn_imp = QtWidgets.QPushButton("Import CSV ...")
        btn_imp.clicked.connect(self.import_csv)
        btn_exp = QtWidgets.QPushButton("Export CSV ...")
        btn_exp.clicked.connect(self.export_csv)
        for b in (self.btn_add, btn_rm, btn_clr):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(btn_imp)
        bar.addWidget(btn_exp)
        vl.addLayout(bar)

        self.table = QtWidgets.QTableWidget(N_STATIONS, 7)
        self.table.setHorizontalHeaderLabels(["On", "#", "Name (SK)", "Type", "Sub type", "Kind", "Source"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(6, QtWidgets.QHeaderView.Stretch)
        for c, w in ((0, 40), (1, 34), (2, 120), (3, 135), (4, 135), (5, 80)):
            self.table.setColumnWidth(c, w)
        self.rows = [StationRow(self.table, r) for r in range(N_STATIONS)]
        for r in range(N_STATIONS):
            self.table.setRowHeight(r, 30)
        self.table.setMinimumHeight(360)
        vl.addWidget(self.table)
        v.addWidget(gb, 3)

        # ---- output settings ----
        og = QtWidgets.QGroupBox("Output signal")
        g = QtWidgets.QGridLayout(og)
        self.fmt = QtWidgets.QComboBox()
        for k, (_, d) in FORMATS.items():
            self.fmt.addItem("%s  -  %s" % (k, d), k)
        self.sps = QtWidgets.QComboBox()
        self.sps.addItems(["auto", "2", "4", "8"])
        self.sps.setToolTip("Samples per symbol. auto = 2 (I/Q) or 4 (real16). 2 means 20.48 MS/s.")
        self.rt = QtWidgets.QComboBox()
        self.rt.addItems(["auto", "real time", "as fast as possible"])
        self.rt.setToolTip("auto: real time for TCP, as fast as possible for files")
        self.amp = QtWidgets.QDoubleSpinBox()
        self.amp.setRange(0.01, 0.9)
        self.amp.setSingleStep(0.01)
        self.amp.setValue(0.18)
        self.amp.setToolTip("rms of one I/Q component relative to full scale. 8 bit: keep 0.08 ... 0.5")
        self.loop = QtWidgets.QCheckBox("loop file sources")
        self.conj = QtWidgets.QCheckBox("mirror spectrum (--conj)")
        self.prefill = QtWidgets.QSpinBox()
        self.prefill.setRange(0, 5000)
        self.prefill.setValue(300)
        self.prefill.setSuffix(" ms")
        self.duration = QtWidgets.QSpinBox()
        self.duration.setRange(0, 86400)
        self.duration.setSpecialValueText("unlimited")
        self.duration.setSuffix(" s")

        self.rb_tcp = QtWidgets.QRadioButton("TCP server on 127.0.0.1, port")
        self.rb_file = QtWidgets.QRadioButton("File")
        self.rb_tcp.setChecked(True)
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(5000)
        self.outfile = QtWidgets.QLineEdit("dsr.cs16")
        bo = QtWidgets.QToolButton()
        bo.setText("...")
        bo.clicked.connect(self.browse_out)

        g.addWidget(QtWidgets.QLabel("Format"), 0, 0)
        g.addWidget(self.fmt, 0, 1, 1, 3)
        g.addWidget(QtWidgets.QLabel("Samples/symbol"), 0, 4)
        g.addWidget(self.sps, 0, 5)
        g.addWidget(self.rb_tcp, 1, 0, 1, 2)
        g.addWidget(self.port, 1, 2)
        g.addWidget(self.rb_file, 2, 0)
        g.addWidget(self.outfile, 2, 1, 1, 4)
        g.addWidget(bo, 2, 5)
        g.addWidget(QtWidgets.QLabel("Clock"), 3, 0)
        g.addWidget(self.rt, 3, 1, 1, 2)
        g.addWidget(QtWidgets.QLabel("Level (--amp)"), 3, 3)
        g.addWidget(self.amp, 3, 4)
        g.addWidget(self.loop, 4, 0, 1, 2)
        g.addWidget(self.conj, 4, 2, 1, 2)
        g.addWidget(QtWidgets.QLabel("Prefill"), 4, 4)
        g.addWidget(self.prefill, 4, 5)
        g.addWidget(QtWidgets.QLabel("Duration"), 5, 0)
        g.addWidget(self.duration, 5, 1)
        self.rate_lbl = QtWidgets.QLabel()
        self.rate_lbl.setObjectName("hint")
        g.addWidget(self.rate_lbl, 5, 2, 1, 4)
        g.setColumnStretch(1, 1)
        v.addWidget(og)

        # ---- program + run controls ----
        rg = QtWidgets.QGroupBox("Run")
        rl = QtWidgets.QGridLayout(rg)
        self.bin_edit = QtWidgets.QLineEdit(find_binary("dsr_encoder"))
        bb = QtWidgets.QToolButton()
        bb.setText("...")
        bb.clicked.connect(self.browse_bin)
        self.ffmpeg_edit = QtWidgets.QLineEdit("ffmpeg")
        self.btn_start = QtWidgets.QPushButton("\u25b6  Start encoder")
        self.btn_start.setObjectName("start")
        self.btn_stop = QtWidgets.QPushButton("\u25a0  Stop")
        self.btn_stop.setObjectName("stop")
        self.btn_apply = QtWidgets.QPushButton("\u21bb  Apply changes (restart)")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_apply.clicked.connect(self.apply)
        self.led_run = Led("encoder")
        self.led_cli = Led("decoders: 0")
        self.uptime = QtWidgets.QLabel("")
        self.cmd_view = QtWidgets.QLineEdit()
        self.cmd_view.setReadOnly(True)
        bcp = QtWidgets.QToolButton()
        bcp.setText("copy")
        bcp.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self.cmd_view.text()))

        rl.addWidget(QtWidgets.QLabel("dsr_encoder"), 0, 0)
        rl.addWidget(self.bin_edit, 0, 1, 1, 4)
        rl.addWidget(bb, 0, 5)
        rl.addWidget(QtWidgets.QLabel("ffmpeg"), 1, 0)
        rl.addWidget(self.ffmpeg_edit, 1, 1, 1, 2)
        rl.addWidget(self.btn_start, 2, 0, 1, 2)
        rl.addWidget(self.btn_stop, 2, 2)
        rl.addWidget(self.btn_apply, 2, 3, 1, 2)
        rl.addWidget(self.led_run, 3, 0)
        rl.addWidget(self.led_cli, 3, 1)
        rl.addWidget(self.uptime, 3, 2)
        rl.addWidget(QtWidgets.QLabel("Command"), 4, 0)
        rl.addWidget(self.cmd_view, 4, 1, 1, 4)
        rl.addWidget(bcp, 4, 5)
        rl.setColumnStretch(1, 1)
        v.addWidget(rg)

        self.log = LogView()
        self.log.setMinimumHeight(110)
        v.addWidget(self.log, 1)
        self.statusBar().showMessage("Qt binding: %s" % QT_API)

        # ---- wiring ----
        for w in (self.fmt, self.sps):
            w.currentIndexChanged.connect(self.refresh_info)
        self.rb_tcp.toggled.connect(self.refresh_info)
        self.fmt.currentIndexChanged.connect(self.suggest_filename)
        self.ticker = QtCore.QTimer(self)
        self.ticker.timeout.connect(self.tick)
        self.ticker.start(1000)

        self.load_presets()
        self.load_settings()
        self.refresh_info()
        self.set_running(False)

    # ------------------------------------------------------------------ stations
    def free_row(self):
        for r in self.rows:
            if r.is_free():
                return r
        QtWidgets.QMessageBox.information(self, "Add station", "All 16 slots are in use.")
        return None

    def fill(self, row, source, name="", pty=0, kind="music", sub=-1):
        row.from_dict({"active": True, "name": name, "pty": pty, "sub": sub, "kind": kind, "source": source})

    def add_file(self):
        row = self.free_row()
        if row:
            row.browse()

    def add_url(self):
        row = self.free_row()
        if not row:
            return
        url, ok = QtWidgets.QInputDialog.getText(self, "Internet stream", "Stream URL (http/https):")
        if ok and url.strip():
            host = re.sub(r"^\w+://", "", url.strip()).split("/")[0].split(".")
            nm = (host[1] if len(host) > 2 else host[0])[:8]
            self.fill(row, url.strip(), nm, 0, "music")

    def add_tone(self):
        row = self.free_row()
        if not row:
            return
        t, ok = QtWidgets.QInputDialog.getText(self, "Test tone", "Frequencies left,right in Hz:", text="440,660")
        if ok and t.strip():
            self.fill(row, "tone:" + t.strip().replace(" ", ""), "TONE %d" % (row.row + 1))

    def remove_selected(self):
        for idx in self.table.selectionModel().selectedRows():
            self.rows[idx.row()].clear()

    def clear_all(self):
        for r in self.rows:
            r.clear()

    def load_presets(self):
        """Optional list of favourite stations: gui/stations_presets.csv (name,pty,kind,source)."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stations_presets.csv")
        self.preset_menu.clear()
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for p in csv.DictReader(l for l in f if not l.startswith("#")):
                    act = self.preset_menu.addAction(p["name"])
                    act.triggered.connect(lambda _=False, p=p: self.add_preset(p))
        except OSError:
            self.preset_menu.setEnabled(False)

    def add_preset(self, p):
        row = self.free_row()
        if row:
            self.fill(row, p["source"], p["name"][:8], int(p.get("pty") or 0), p.get("kind", "music"))

    def export_csv(self):
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export stations", "dsr_stations.csv", "CSV (*.csv)")
        if not p:
            return
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, CSV_HEADER)
            w.writeheader()
            for r in self.rows:
                d = r.to_dict()
                if d["active"] or d["source"]:
                    d["sub"] = "" if d["sub"] < 0 else d["sub"]
                    w.writerow(d)

    def import_csv(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import stations", "", "CSV (*.csv)")
        if not p:
            return
        self.clear_all()
        with open(p, newline="", encoding="utf-8") as f:
            for d in csv.DictReader(f):
                try:
                    n = int(d["nr"]) - 1
                    self.rows[n].from_dict({**d, "active": str(d["active"]).lower() in ("1", "true", "yes")})
                except (KeyError, ValueError, IndexError):
                    self.log.add("CSV: skipped line %r" % d)

    # ------------------------------------------------------------------ output
    def cur_fmt(self):
        return self.fmt.currentData()

    def eff_sps(self):
        if self.sps.currentText() != "auto":
            return int(self.sps.currentText())
        return 4 if self.cur_fmt() == "real16" else 2

    def refresh_info(self):
        bps = FORMATS[self.cur_fmt()][0]
        sps = self.eff_sps()
        msps = sps * SYMBOL_RATE / 1e6
        mbs = msps * bps
        warn = ""
        if self.cur_fmt() == "real16" and sps < 4:
            warn = "   (real16 needs 4 or 8 samples/symbol!)"
        self.rate_lbl.setText("%.2f MS/s  -  %.0f MB/s  -  %.1f GB/min%s" % (msps, mbs, mbs * 60 / 1000, warn))

    def suggest_filename(self):
        cur = self.outfile.text()
        if re.fullmatch(r"dsr\.(cs16|cs8|cf32|real16)", cur):
            self.outfile.setText("dsr.%s" % self.cur_fmt())

    def browse_out(self):
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Output file", self.outfile.text())
        if p:
            self.outfile.setText(p)
            self.rb_file.setChecked(True)

    def browse_bin(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "dsr_encoder binary", self.bin_edit.text())
        if p:
            self.bin_edit.setText(p)

    # ------------------------------------------------------------------ command line
    def build_args(self):
        """Returns (args, error)."""
        args = []
        n_active = 0
        for r in self.rows:
            d = r.to_dict()
            if not d["active"]:
                continue
            if not d["source"]:
                return None, "Station %d is switched on but has no source." % d["nr"]
            n_active += 1
            nr = d["nr"]
            args += ["-i", "%d=%s" % (nr, d["source"])]
            if d["name"]:
                args += ["-n", "%d=%s" % (nr, d["name"])]
            pty = "%d" % d["pty"] + ("/%d" % d["sub"] if d["sub"] >= 0 else "")
            args += ["-p", "%d=%s" % (nr, pty), "-m", "%d=%s" % (nr, d["kind"])]
        if not n_active:
            return None, "Switch on at least one station."
        args += ["-f", self.cur_fmt()]
        if self.sps.currentText() != "auto":
            args += ["--sps", self.sps.currentText()]
        if self.cur_fmt() == "real16" and self.eff_sps() < 4:
            return None, "real16 needs 4 or 8 samples/symbol."
        if self.rb_tcp.isChecked():
            args += ["-o", "tcp://127.0.0.1:%d" % self.port.value()]
        else:
            if not self.outfile.text().strip():
                return None, "Please choose an output file."
            args += ["-o", self.outfile.text().strip()]
        if self.rt.currentIndex() == 1:
            args.append("--realtime")
        elif self.rt.currentIndex() == 2:
            args.append("--no-realtime")
        args += ["--amp", "%.3f" % self.amp.value(), "--prefill", str(self.prefill.value())]
        if self.loop.isChecked():
            args.append("--loop")
        if self.conj.isChecked():
            args.append("--conj")
        if self.duration.value() > 0:
            args += ["-t", str(self.duration.value())]
        if self.ffmpeg_edit.text().strip() not in ("", "ffmpeg"):
            args += ["--ffmpeg", self.ffmpeg_edit.text().strip()]
        return args, None

    # ------------------------------------------------------------------ process control
    def start(self):
        prog = self.bin_edit.text().strip()
        if not (os.path.isfile(prog) and os.access(prog, os.X_OK)):
            QtWidgets.QMessageBox.warning(self, "dsr_encoder", "Cannot execute %r.\nBuild it first with 'make'." % prog)
            return
        args, err = self.build_args()
        if err:
            QtWidgets.QMessageBox.warning(self, "Cannot start", err)
            return
        self.save_settings()
        self.cmd_view.setText(cmdline_text(prog, args))
        self.log.add("$ " + self.cmd_view.text())
        self.n_clients = 0
        self.proc.start(prog, args)

    def stop(self):
        self.restart_pending = False
        stop_process(self.proc)

    def apply(self):
        if self.proc.state() == QtCore.QProcess.NotRunning:
            self.start()
        else:
            self.restart_pending = True
            stop_process(self.proc)

    def on_started(self):
        self.t_start = time.time()
        self.set_running(True)

    def on_finished(self, code, status):
        self.log.add("encoder finished (exit code %s)" % code)
        self.set_running(False)
        if self.restart_pending:
            self.restart_pending = False
            QtCore.QTimer.singleShot(300, self.start)

    def on_error(self, err):
        if err == QtCore.QProcess.FailedToStart:
            QtWidgets.QMessageBox.critical(self, "dsr_encoder", "The program could not be started.")

    def on_output(self):
        for ch in (self.proc.readAllStandardError(), self.proc.readAllStandardOutput()):
            txt = bytes(ch).decode("utf-8", "replace")
            self.log.add(txt)
            for line in txt.splitlines():
                m = re.search(r"client connected \((\d+) now\)", line) or re.search(r"client disconnected \((\d+) left\)", line)
                if m:
                    self.n_clients = int(m.group(1))
                    self.update_clients()

    def update_clients(self):
        n = getattr(self, "n_clients", 0)
        self.led_cli._text = "decoders connected: %d" % n
        self.led_cli.set_state("ok" if n else "warn")

    def set_running(self, running):
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_apply.setEnabled(True)
        self.led_run.set_state("ok" if running else "off")
        if running:
            self.update_clients()
        else:
            self.led_cli._text = "decoders connected: -"
            self.led_cli.set_state("off")
            self.uptime.setText("")
        self.statusBar().showMessage(("Running" if running else "Stopped") + "  |  Qt binding: " + QT_API)

    def tick(self):
        if self.proc.state() == QtCore.QProcess.Running:
            s = int(time.time() - self.t_start)
            self.uptime.setText("running %d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60))

    # ------------------------------------------------------------------ persistence
    def save_settings(self):
        cfg = {"stations": [r.to_dict() for r in self.rows], "fmt": self.cur_fmt(),
               "sps": self.sps.currentText(), "tcp": self.rb_tcp.isChecked(), "port": self.port.value(),
               "file": self.outfile.text(), "rt": self.rt.currentIndex(), "amp": self.amp.value(),
               "loop": self.loop.isChecked(), "conj": self.conj.isChecked(), "prefill": self.prefill.value(),
               "bin": self.bin_edit.text(), "ffmpeg": self.ffmpeg_edit.text()}
        self.settings.setValue("config", json.dumps(cfg))

    def load_settings(self):
        raw = self.settings.value("config")
        if not raw:
            self.rows[0].from_dict({"active": True, "name": "TONE 1", "pty": 10, "kind": "music", "source": "tone:440,660"})
            return
        try:
            cfg = json.loads(raw)
            for d in cfg.get("stations", []):
                self.rows[int(d["nr"]) - 1].from_dict(d)
            self.fmt.setCurrentIndex(max(0, self.fmt.findData(cfg.get("fmt", "cs16"))))
            self.sps.setCurrentText(cfg.get("sps", "auto"))
            (self.rb_tcp if cfg.get("tcp", True) else self.rb_file).setChecked(True)
            self.port.setValue(cfg.get("port", 5000))
            self.outfile.setText(cfg.get("file", "dsr.cs16"))
            self.rt.setCurrentIndex(cfg.get("rt", 0))
            self.amp.setValue(cfg.get("amp", 0.18))
            self.loop.setChecked(cfg.get("loop", False))
            self.conj.setChecked(cfg.get("conj", False))
            self.prefill.setValue(cfg.get("prefill", 300))
            if cfg.get("bin") and os.path.isfile(cfg["bin"]):
                self.bin_edit.setText(cfg["bin"])
            self.ffmpeg_edit.setText(cfg.get("ffmpeg", "ffmpeg"))
        except (ValueError, KeyError, TypeError, IndexError):
            pass

    def closeEvent(self, ev):
        self.save_settings()
        self.restart_pending = False
        stop_process(self.proc)
        ev.accept()


def main():
    app = make_app("DSR Encoder")
    w = EncoderWindow()
    w.show()
    sys.exit(app.exec_() if hasattr(app, "exec_") else app.exec())


if __name__ == "__main__":
    main()
