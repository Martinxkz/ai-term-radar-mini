"""RS485 measuring-wheel dashboard. Run with: python encoder_dashboard.py."""

import csv
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import pyqtgraph as pg
import serial
import serial.tools.list_ports
from PyQt5.QtCore import QSettings, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from encoder_core import (MeasurementCalculator, RESOLUTION, WHEEL_CIRCUMFERENCE_M,
                          parse_position_response, position_request)


def resource_path(name):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


class EncoderWorker(QThread):
    sample = pyqtSignal(float, int)
    connected = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, port, baud, address, interval_ms):
        super().__init__()
        self.port, self.baud, self.address = port, baud, address
        self.interval = interval_ms / 1000

    def run(self):
        try:
            with serial.Serial(self.port, self.baud, timeout=0.15, write_timeout=0.2) as device:
                self.connected.emit()
                request = position_request(self.address)
                while not self.isInterruptionRequested():
                    started = time.monotonic()
                    device.reset_input_buffer()
                    if device.write(request) != len(request):
                        raise serial.SerialException("请求帧发送不完整")
                    header = device.read(3)
                    if len(header) != 3:
                        raise serial.SerialException("读取超时或响应头不完整")
                    if header[1] == 0x83:
                        remaining = 2
                    elif header[1] == 0x03 and header[2] == 4:
                        remaining = 6
                    else:
                        raise serial.SerialException("响应功能码或数据长度不正确")
                    tail = device.read(remaining)
                    if len(tail) != remaining:
                        raise serial.SerialException("读取超时或响应帧不完整")
                    count = parse_position_response(header + tail, self.address)
                    self.sample.emit(time.monotonic(), count)
                    deadline = started + self.interval
                    while not self.isInterruptionRequested() and time.monotonic() < deadline:
                        self.msleep(min(10, max(1, int((deadline - time.monotonic()) * 1000))))
        except (serial.SerialException, OSError, ValueError) as exc:
            self.error.emit(str(exc))


class SimulatorWorker(QThread):
    sample = pyqtSignal(float, int)
    connected = pyqtSignal()
    error = pyqtSignal(str)
    TURNS = 1000

    def __init__(self, target, acceleration, interval_ms):
        super().__init__()
        self.target, self.acceleration = target, acceleration
        self.interval = interval_ms / 1000

    def run(self):
        modulus = self.TURNS * RESOLUTION
        count = modulus / 2
        speed = 0.0
        last = time.monotonic()
        self.connected.emit()
        self.sample.emit(last, int(count))
        while not self.isInterruptionRequested():
            deadline = last + self.interval
            while not self.isInterruptionRequested() and time.monotonic() < deadline:
                self.msleep(min(10, max(1, int((deadline - time.monotonic()) * 1000))))
            if self.isInterruptionRequested():
                break
            now = time.monotonic()
            dt = now - last
            difference = self.target - speed
            direction = 1 if difference > 0 else -1 if difference < 0 else 0
            ramp = min(dt, abs(difference) / self.acceleration)
            new_speed = speed + direction * self.acceleration * ramp
            distance = (speed + new_speed) * ramp / 2 + new_speed * (dt - ramp)
            count += distance * RESOLUTION / WHEEL_CIRCUMFERENCE_M
            self.sample.emit(now, round(count) % modulus)
            speed, last = new_speed, now


class EncoderDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RS485 编码器板链监控")
        self.setWindowIcon(QIcon(str(resource_path("web/comau-logo.png"))))
        self.resize(1250, 900)
        self.worker = self.calculator = self.csv_file = self.csv_writer = None
        self.accepting = False
        self.failure = None
        self.last_sample_time = None
        self.history = {}
        self.confirmed_port = None
        self.simulation = False
        self.settings = QSettings("BA ENG", "Encoder Monitor")
        self.output_dir = None
        self.init_ui()
        self.refresh_ports()
        self.display_timer = QTimer(self)
        self.display_timer.timeout.connect(self.update_display)
        self.display_timer.start(100)

    def init_ui(self):
        self.setStyleSheet("""
            QMainWindow, QScrollArea { background: #eaf1f8; }
            QScrollArea { border: none; }
            QWidget { color: #17314b; font-family: "Microsoft YaHei", "Segoe UI"; font-size: 13px; }
            QWidget#dashboardCanvas { background: #eaf1f8; }
            QFrame#glassCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(255,255,255,250), stop:1 rgba(244,249,255,245));
                border: 1px solid #d5e4f0; border-radius: 14px;
            }
            QLabel#eyebrow { color: #637f99; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
            QLabel#sectionTitle { color: #1b3853; font-size: 16px; font-weight: 700; }
            QLabel#metricValue { color: #103f6a; font-size: 30px; font-weight: 700; }
            QLabel#detailValue { color: #183c5b; font-size: 17px; font-weight: 700; }
            QLabel#muted { color: #61798e; font-size: 11px; }
            QPushButton { background: #f3f8fc; border: 1px solid #c9dbe9;
                border-radius: 7px; padding: 7px 12px; color: #244b6b; }
            QPushButton:hover { background: #e3f0fa; }
            QPushButton:disabled { color: #9cabb8; background: #edf1f4; }
            QPushButton#primary { background: #105393; color: white; border: 1px solid #105393; font-weight: 700; }
            QPushButton#primary:hover { background: #1b6aac; }
            QPushButton#primary:disabled { background: #a7b9c9; border-color: #a7b9c9; }
            QComboBox, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #c9dbe9;
                border-radius: 6px; padding: 5px; min-height: 22px; }
        """)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.setCentralWidget(scroll)
        central = QWidget()
        central.setObjectName("dashboardCanvas")
        scroll.setWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(26, 20, 26, 24)
        layout.setSpacing(14)
        notice = QLabel("BA ENG INTERNAL USE ONLY")
        notice.setObjectName("internalNotice")
        notice.setStyleSheet("color: #b91c1c; font-size: 16px; font-weight: 900;")
        layout.addWidget(notice)
        heading = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QPixmap(str(resource_path("web/comau-logo.png"))).scaled(
            72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        heading.addWidget(logo)
        heading_text = QVBoxLayout()
        eyebrow = QLabel("MOTION MONITOR / ENCODER")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("板链运行监控")
        title.setStyleSheet("font-size: 27px; font-weight: 800; color: #153956;")
        subtitle = QLabel("RS485 · 200 mm 计米轮    /    作者 YD")
        subtitle.setObjectName("muted")
        heading_text.addWidget(eyebrow)
        heading_text.addWidget(title)
        heading_text.addWidget(subtitle)
        heading.addLayout(heading_text, 1)
        self.mode_label = QLabel("真实编码器 · 未连接")
        self.mode_label.setStyleSheet("font-weight: bold; color: #174e80; padding: 8px;")
        heading.addWidget(self.mode_label)
        layout.addLayout(heading)

        control_card = QFrame()
        control_card.setObjectName("glassCard")
        control_layout = QVBoxLayout(control_card)
        control_layout.setContentsMargins(20, 17, 20, 17)
        control_title = QLabel("01 / 采集设置")
        control_title.setObjectName("sectionTitle")
        control_layout.addWidget(control_title)
        controls = QHBoxLayout()
        controls.setSpacing(18)
        self.port = QComboBox()
        self.port.currentIndexChanged.connect(self.invalidate_port)
        self.refresh = QPushButton("刷新端口")
        self.refresh.clicked.connect(self.refresh_ports)
        self.confirm_port = QPushButton("确认 COM 口")
        self.confirm_port.clicked.connect(self.confirm_selected_port)
        self.port_status = QLabel("未确认")
        self.baud = QComboBox()
        self.baud.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.baud.setCurrentText("115200")
        self.address = QSpinBox()
        self.address.setRange(1, 255)
        self.interval = QSpinBox()
        self.interval.setRange(25, 5000)
        self.interval.setValue(50)
        self.interval.setSuffix(" ms")
        self.limit = QDoubleSpinBox()
        self.limit.setRange(0.001, 1000)
        self.limit.setDecimals(3)
        self.limit.setValue(5.0)
        self.limit.setSuffix(" m/s")
        self.limit.setToolTip("超过此绝对速度时停止采集，防止回绕或复位产生错误数据；不是安全保护。")
        self.limit.valueChanged.connect(self.update_speed_ranges)
        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(4)
        for index, (label, widget) in enumerate((
                ("端口", self.port), ("波特率", self.baud), ("站号", self.address),
                ("轮询周期", self.interval), ("合理速度上限", self.limit))):
            row, column = divmod(index, 3)
            form.addWidget(QLabel(label), row * 2, column)
            form.addWidget(widget, row * 2 + 1, column)
            form.setColumnStretch(column, 1)
        controls.addLayout(form, 3)
        buttons = QGridLayout()
        buttons.addWidget(self.refresh, 0, 0)
        buttons.addWidget(self.confirm_port, 0, 1)
        buttons.addWidget(self.port_status, 1, 0)
        self.connect_button = QPushButton("连接并开始采集")
        self.connect_button.setObjectName("primary")
        self.connect_button.clicked.connect(self.toggle)
        buttons.addWidget(self.connect_button, 1, 1)
        self.simulation_button = QPushButton("模拟输入设置…")
        self.simulation_button.clicked.connect(self.show_simulation_dialog)
        buttons.addWidget(self.simulation_button, 2, 0)
        self.photo_button = QPushButton("选择编码器照片…")
        self.photo_button.clicked.connect(self.select_photo)
        buttons.addWidget(self.photo_button, 2, 1)
        controls.addLayout(buttons, 2)
        photo_layout = QVBoxLayout()
        self.photo = QLabel()
        self.photo.setAlignment(Qt.AlignCenter)
        self.photo.setFixedSize(90, 105)
        self.photo.setWordWrap(True)
        photo_layout.addWidget(self.photo)
        photo_layout.addStretch()
        controls.addLayout(photo_layout)
        self.load_photo(self.settings.value("encoder_photo", "", type=str))
        control_layout.addLayout(controls)
        storage = QHBoxLayout()
        self.output_label = QLabel("未选择数据保存目录")
        self.output_label.setWordWrap(True)
        self.output_label.setObjectName("muted")
        self.output_button = QPushButton("选择保存目录…")
        self.output_button.clicked.connect(self.select_output_directory)
        storage.addWidget(self.output_button)
        storage.addWidget(self.output_label, 1)
        control_layout.addLayout(storage)
        range_warning = QLabel("真实编码器计数存在量程；连续运转跨越边界可能产生错误，请核实设备量程。")
        range_warning.setWordWrap(True)
        range_warning.setStyleSheet("color: #915300; font-size: 11px; font-weight: 600;")
        control_layout.addWidget(range_warning)
        layout.addWidget(control_card)

        metric_title = QLabel("02 / 实时监控")
        metric_title.setObjectName("sectionTitle")
        layout.addWidget(metric_title)
        metric_grid = QGridLayout()
        self.metric_labels = {}
        primary = (
            ("speed", "实时速度", "m/s", "相邻位置差分"),
            ("smooth_speed", "平滑速度", "m/s", "0.25 s 指数平滑"),
            ("average_speed", "3 秒平均速度", "m/s", "3 秒净位移 / 时间"),
        )
        for column, (key, title_text, unit, description) in enumerate(primary):
            metric_grid.addWidget(self.metric_card(key, title_text, unit, description, True), 0, column)
        secondary = (
            ("acceleration", "实时加速度", "m/s²"),
            ("smooth_acceleration", "平滑加速度", "m/s²"),
            ("average_acceleration", "3 秒平均加速度", "m/s²"),
            ("displacement", "净位移", "m"),
            ("travel", "累计行程", "m"),
        )
        secondary_grid = QGridLayout()
        for column, (key, title_text, unit) in enumerate(secondary):
            secondary_grid.addWidget(self.metric_card(key, title_text, unit, "", False), 0, column)
        layout.addLayout(metric_grid)
        layout.addLayout(secondary_grid)
        self.values = QLabel("等待采集")
        self.values.setWordWrap(True)
        self.values.setObjectName("muted")
        layout.addWidget(self.values)

        pg.setConfigOptions(antialias=True, background="#ffffff", foreground="#17324a")
        self.plots = {}
        plot_definitions = (
            ("speed", "03 / 速度趋势", "速度 (m/s)",
             (("speed", "实时速度", "#c06a10"), ("smooth_speed", "平滑速度", "#078461"))),
            ("average_speed", "04 / 3 秒平均速度", "速度 (m/s)",
             (("average_speed", "3 秒平均速度", "#1761b0"),)),
            ("acceleration", "05 / 加速度趋势", "加速度 (m/s²)",
             (("acceleration", "实时加速度", "#c06a10"),
              ("smooth_acceleration", "平滑加速度", "#078461"),
              ("average_acceleration", "3 秒平均加速度", "#1761b0"))),
        )
        self.curves = {}
        self.scale_labels = {}
        for key, title_text, y_label, series in plot_definitions:
            card = QFrame()
            card.setObjectName("glassCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(18, 14, 18, 14)
            card_heading = QHBoxLayout()
            title = QLabel(title_text)
            title.setObjectName("sectionTitle")
            card_heading.addWidget(title)
            card_heading.addStretch()
            if key in ("speed", "average_speed"):
                self.scale_labels[key] = QLabel()
                self.scale_labels[key].setObjectName("muted")
                card_heading.addWidget(self.scale_labels[key])
            legend = QLabel("　".join(
                f'<span style="color:{color};font-weight:bold;">━━</span> {label}'
                for _, label, color in series))
            legend.setStyleSheet("color: #435d74; font-size: 12px;")
            legend.setWordWrap(True)
            card_heading.addWidget(legend)
            card_layout.addLayout(card_heading)
            plot = pg.PlotWidget()
            plot.setFixedHeight(245)
            plot.showGrid(x=True, y=True, alpha=0.18)
            plot.setLabel("left", y_label)
            plot.setLabel("bottom", "采集时间 (s)")
            plot.getAxis("left").setWidth(90)
            plot.getAxis("bottom").setHeight(42)
            plot.getAxis("left").enableAutoSIPrefix(False)
            plot.getAxis("bottom").enableAutoSIPrefix(False)
            plot.getAxis("left").setStyle(maxTickLevel=0, autoExpandTextSpace=False)
            plot.getAxis("bottom").setStyle(maxTickLevel=0, hideOverlappingLabels=False)
            plot.getAxis("bottom").setTickSpacing(levels=[(5.0, 0.0)])
            plot.setMouseEnabled(x=False, y=False)
            plot.hideButtons()
            for name, label, color in series:
                self.curves[name] = plot.plot(pen=pg.mkPen(color, width=2.5), connect="finite")
            self.plots[key] = plot
            if key != "speed":
                plot.setXLink(self.plots["speed"])
            card_layout.addWidget(plot)
            layout.addWidget(card)
        self.plots["speed"].setXRange(0, 30, padding=0)
        self.update_speed_ranges()
        self.statusBar().showMessage("未连接；请确认 COM 口并选择数据保存目录。")

    def metric_card(self, key, title, unit, description, primary):
        card = QFrame()
        card.setObjectName("glassCard")
        content = QVBoxLayout(card)
        content.setContentsMargins(16, 14, 16, 14)
        heading = QLabel(title)
        heading.setObjectName("muted")
        value = QLabel("--")
        value.setObjectName("metricValue" if primary else "detailValue")
        value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        unit_label = QLabel(unit + (f"  ·  {description}" if description else ""))
        unit_label.setObjectName("muted")
        content.addWidget(heading)
        content.addWidget(value)
        content.addWidget(unit_label)
        self.metric_labels[key] = value
        return card

    def update_speed_ranges(self):
        if not hasattr(self, "plots") or "average_speed" not in self.plots:
            return
        bound = 1.2 * self.limit.value()
        for key in ("speed", "average_speed"):
            self.plots[key].setYRange(-bound, bound, padding=0)
            self.plots[key].getAxis("left").setTickSpacing(levels=[(bound / 2, 0.0)])
            self.scale_labels[key].setText(f"纵轴 ±{bound:.4f} m/s")

    def refresh_ports(self):
        current = self.port.currentData()
        self.invalidate_port()
        self.port.clear()
        self.port.addItem("请选择 COM 口", None)
        for port in serial.tools.list_ports.comports():
            self.port.addItem(f"{port.device} — {port.description}", port.device)
        index = self.port.findData(current) if current else 0
        if index >= 0:
            self.port.setCurrentIndex(index)

    def invalidate_port(self):
        self.confirmed_port = None
        self.port_status.setText("未确认")

    def confirm_selected_port(self):
        port = self.port.currentData()
        if not port:
            QMessageBox.warning(self, "未选择串口", "请选择设备的 COM 口。")
            return
        self.confirmed_port = port
        self.port_status.setText(f"已确认：{port}")

    def load_photo(self, path):
        pixmap = QPixmap(path) if path else QPixmap()
        if pixmap.isNull():
            self.photo.setText("照片不可读取\n请重新选择" if path else "尚未选择\n编码器照片")
            if path:
                self.statusBar().showMessage("无法读取编码器照片，请重新选择。")
            return False
        self.photo.setPixmap(pixmap.scaled(
            self.photo.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        return True

    def select_photo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择编码器实物照片", "", "图片 (*.jpg *.jpeg *.png *.bmp)")
        if path:
            if self.load_photo(path):
                self.settings.setValue("encoder_photo", path)
            else:
                QMessageBox.warning(self, "无法读取照片", "请选择可读取的图片文件。")

    def select_output_directory(self):
        initial = str(self.output_dir) if self.output_dir else str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择数据保存目录", initial)
        if path:
            self.output_dir = Path(path)
            self.output_label.setText(str(self.output_dir))
            self.output_label.setToolTip(str(self.output_dir))

    def show_simulation_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("模拟输入设置（不连接设备）")
        form = QFormLayout(dialog)
        target = QDoubleSpinBox()
        target.setRange(-self.limit.value(), self.limit.value())
        target.setDecimals(3)
        target.setValue(min(0.6, self.limit.value()))
        target.setSuffix(" m/s")
        acceleration = QDoubleSpinBox()
        acceleration.setRange(0.01, 10.0)
        acceleration.setValue(0.3)
        acceleration.setSuffix(" m/s²")
        form.addRow("目标速度", target)
        form.addRow("加速度限制", acceleration)
        start = QPushButton("开始模拟（不连接串口）")
        start.clicked.connect(dialog.accept)
        form.addRow(start)
        if dialog.exec_() == QDialog.Accepted:
            self.start_simulation(target.value(), acceleration.value())

    def start_simulation(self, target, acceleration):
        self.start(simulation=True, target=target, acceleration=acceleration)

    def set_controls_enabled(self, enabled):
        for widget in (self.port, self.refresh, self.confirm_port, self.baud,
                       self.address, self.interval, self.limit,
                       self.simulation_button, self.output_button):
            widget.setEnabled(enabled)

    def toggle(self):
        if self.worker is not None:
            self.stop()
        else:
            self.start()

    def start(self, simulation=False, target=0.6, acceleration=0.3):
        if self.worker is not None:
            return
        port = self.port.currentData()
        if not simulation and (not port or port != self.confirmed_port):
            QMessageBox.warning(self, "无法启动", "请先选择并确认设备的 COM 口。")
            return
        if simulation and (abs(target) > self.limit.value() or acceleration <= 0):
            QMessageBox.warning(self, "无法启动", "模拟目标速度或加速度超出允许范围。")
            return
        if self.output_dir is None:
            QMessageBox.warning(self, "无法启动", "请先选择数据保存目录。")
            return
        if not self.output_dir.is_dir():
            QMessageBox.warning(self, "无法启动", "所选保存目录不存在，请重新选择。")
            return
        try:
            calculator = MeasurementCalculator(
                hardware_turns=SimulatorWorker.TURNS if simulation else None,
                max_speed=self.limit.value()
            )
            prefix = "simulation" if simulation else "encoder"
            filename = self.output_dir / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S_%f}.csv"
            csv_file = filename.open("x", encoding="utf-8-sig", newline="")
            try:
                writer = csv.writer(csv_file)
                writer.writerow(["elapsed_s", "count", "displacement_m", "travel_m",
                                 "speed_m_s", "smooth_speed_m_s", "average_speed_3s_m_s",
                                 "acceleration_m_s2", "smooth_acceleration_m_s2",
                                 "average_acceleration_3s_m_s2"])
                csv_file.flush()
            except OSError:
                csv_file.close()
                raise
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "无法启动", str(exc))
            return
        self.calculator, self.csv_file, self.csv_writer = calculator, csv_file, writer
        self.history = {name: deque(maxlen=1200)
                        for name in ("time", *self.curves, "displacement", "travel")}
        for curve in self.curves.values():
            curve.clear()
        for label in self.metric_labels.values():
            label.setText("--")
        self.plots["speed"].setXRange(0, 30, padding=0)
        self.values.setText("正在连接，等待第一个有效位置…")
        self.failure = None
        self.last_sample_time = None
        self.accepting = True
        self.simulation = simulation
        self.mode_label.setText("模拟输入 · 非真实设备" if simulation else f"真实编码器 · {port}")
        self.mode_label.setStyleSheet(
            "font-weight: bold; color: #a63e00;" if simulation else
            "font-weight: bold; color: #174e80;")
        self.worker = (SimulatorWorker(target, acceleration, self.interval.value()) if simulation else
                       EncoderWorker(port, int(self.baud.currentText()),
                                     self.address.value(), self.interval.value()))
        self.worker.connected.connect(self.on_connected)
        self.worker.sample.connect(self.on_sample)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.on_finished)
        self.set_controls_enabled(False)
        self.connect_button.setText("停止并保存")
        self.statusBar().showMessage(f"正在连接；CSV: {filename.resolve()}")
        self.worker.start()

    def on_connected(self):
        if self.accepting:
            kind = "模拟输入" if self.simulation else "串口已打开"
            self.statusBar().showMessage(f"{kind}；等待有效数据。停止采集后 CSV 自动关闭。")

    def on_sample(self, timestamp, count):
        if not self.accepting:
            return
        try:
            measurement = self.calculator.add(timestamp, count)
            row = [getattr(measurement, name) for name in measurement.__dataclass_fields__]
            self.csv_writer.writerow(["" if value is None else value for value in row])
            self.csv_file.flush()
        except (ValueError, OSError, csv.Error) as exc:
            self.on_error(str(exc))
            self.stop()
            return
        if self.last_sample_time is None:
            kind = "模拟数据" if self.simulation else "真实编码器有效数据"
            self.statusBar().showMessage(f"已收到{kind}；CSV：{self.csv_file.name}")
        self.last_sample_time = timestamp
        self.history["time"].append(measurement.elapsed)
        for name in self.history:
            if name == "time":
                continue
            value = getattr(measurement, name)
            self.history[name].append(float("nan") if value is None else value)
        self.latest = measurement

    def on_error(self, message):
        self.failure = message
        self.accepting = False
        self.statusBar().showMessage(f"采集已中断：{message}")
        self.values.setText(f"数据已失效：{message}")
        for label in self.metric_labels.values():
            label.setText("--")

    def stop(self):
        self.accepting = False
        if self.worker is not None:
            self.worker.requestInterruption()
            self.connect_button.setEnabled(False)
            if not self.failure:
                self.statusBar().showMessage("正在停止采集…")

    def on_finished(self):
        self.accepting = False
        if self.csv_file:
            try:
                self.csv_file.close()
            except OSError as exc:
                self.failure = f"关闭 CSV 失败：{exc}"
            self.csv_file = self.csv_writer = None
        self.worker = None
        self.connect_button.setEnabled(True)
        self.connect_button.setText("连接并开始采集")
        self.set_controls_enabled(True)
        if self.failure:
            self.statusBar().showMessage(f"采集已中断：{self.failure}")
            self.values.setText(f"数据已失效：{self.failure}")
        else:
            self.statusBar().showMessage(f"已停止；CSV 已保存至 {self.output_dir.resolve()}")
            self.values.setText("已停止；曲线为上次采集数据。")
        self.mode_label.setText("模拟输入 · 已停止" if self.simulation else "真实编码器 · 已停止")

    def update_display(self):
        if not self.accepting or self.last_sample_time is None:
            return
        if time.monotonic() - self.last_sample_time > max(1.0, self.interval.value() / 1000 * 3):
            self.values.setText("数据已过期：正在等待编码器响应")
            for label in self.metric_labels.values():
                label.setText("--")
            return
        m = self.latest
        self.values.setText("模拟数据，未连接设备" if self.simulation else "正在采集真实编码器数据")
        for name, label in self.metric_labels.items():
            value = getattr(m, name)
            label.setText("--" if value is None else f"{value:.4f}")
        x = list(self.history["time"])
        for name, curve in self.curves.items():
            curve.setData(x, list(self.history[name]))
        end = max(30.0, m.elapsed)
        self.plots["speed"].setXRange(end - 30.0, end, padding=0)

    def closeEvent(self, event):
        if self.worker is not None:
            self.stop()
            if not self.worker.wait(1500):
                event.ignore()
                QMessageBox.warning(self, "正在停止", "串口线程尚未退出，请稍后关闭窗口。")
                return
            self.on_finished()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = EncoderDashboard()
    window.show()
    sys.exit(app.exec_())
