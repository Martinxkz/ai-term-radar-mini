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
from PyQt5.QtCore import QRectF, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from encoder_core import (MeasurementCalculator, RESOLUTION, WHEEL_CIRCUMFERENCE_M,
                          parse_position_response, position_request)
from encoder_stats import SpeedStats


MAX_CHART_MINUTES = 60


class SpeedGauge(QWidget):
    def __init__(self):
        super().__init__()
        self.speed = None
        self.limit = 5.0
        self.setMinimumSize(250, 190)

    def set_value(self, speed, limit):
        self.speed, self.limit = speed, limit
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        side = min(self.width() - 36, self.height() - 48, 300)
        rect = QRectF((self.width() - side) / 2, max(8, (self.height() - side - 30) / 2), side, side)
        pen = QPen(QColor("#dce8f1"), 12, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, 225 * 16, -270 * 16)
        if self.speed is not None:
            fraction = max(0.0, min(1.0, (self.speed / self.limit + 1) / 2))
            pen.setColor(QColor("#0878a0"))
            painter.setPen(pen)
            painter.drawArc(rect, 225 * 16, int(-270 * 16 * fraction))
        painter.setPen(QColor("#103f6a"))
        font = painter.font()
        font.setPointSize(24)
        font.setBold(True)
        painter.setFont(font)
        text = "--" if self.speed is None else f"{self.speed:.3f}"
        painter.drawText(QRectF(0, rect.top() + side * .30, self.width(), 55),
                         Qt.AlignCenter, text)
        font.setPointSize(11)
        font.setBold(False)
        painter.setFont(font)
        painter.drawText(QRectF(0, rect.top() + side * .30 + 50, self.width(), 28),
                         Qt.AlignCenter, "实时速度  m/s")
        painter.setPen(QColor("#536d83"))
        painter.drawText(QRectF(0, rect.bottom() + 4, self.width(), 22),
                         Qt.AlignCenter, f"量程  −{self.limit:.3f} 至 +{self.limit:.3f} m/s")


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
        self.stats = SpeedStats()
        self.last_long_redraw = 0.0
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
            QWidget { color: #17314b; font-family: "Microsoft YaHei", "Segoe UI"; font-size: 11pt; }
            QWidget#dashboardCanvas { background: #eaf1f8; }
            QFrame#glassCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(255,255,255,250), stop:1 rgba(244,249,255,245));
                border: 1px solid #d5e4f0; border-radius: 14px;
            }
            QLabel#eyebrow { color: #526e87; font-size: 10pt; font-weight: 700; }
            QLabel#sectionTitle { color: #1b3853; font-size: 14pt; font-weight: 700; }
            QLabel#metricValue { color: #103f6a; font-size: 24pt; font-weight: 700; }
            QLabel#detailValue { color: #183c5b; font-size: 17pt; font-weight: 700; }
            QLabel#muted { color: #536e84; font-size: 10pt; }
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
        notice.setStyleSheet("color: #b91c1c; font-size: 12pt; font-weight: 900;")
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
        title.setStyleSheet("font-size: 22pt; font-weight: 800; color: #153956;")
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
        controls.addLayout(buttons, 2)
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
        range_warning.setStyleSheet("color: #915300; font-size: 10pt; font-weight: 600;")
        control_layout.addWidget(range_warning)
        layout.addWidget(control_card)

        metric_title = QLabel("02 / 实时监控")
        metric_title.setObjectName("sectionTitle")
        layout.addWidget(metric_title)
        monitor = QHBoxLayout()
        self.metric_labels = {}
        gauge_card = QFrame()
        gauge_card.setObjectName("glassCard")
        gauge_layout = QVBoxLayout(gauge_card)
        gauge_title = QLabel("实时速度仪表")
        gauge_title.setObjectName("sectionTitle")
        gauge_layout.addWidget(gauge_title, 0, Qt.AlignTop)
        self.gauge = SpeedGauge()
        gauge_layout.addWidget(self.gauge, 1)
        monitor.addWidget(gauge_card, 2)
        metric_grid = QGridLayout()
        secondary = (
            ("smooth_speed", "平滑速度", "m/s"),
            ("acceleration", "实时加速度", "m/s²"),
            ("smooth_acceleration", "平滑加速度", "m/s²"),
            ("displacement", "净位移", "m"),
            ("travel", "累计行程", "m"),
        )
        for i, (key, title_text, unit) in enumerate(secondary):
            metric_grid.addWidget(self.metric_card(key, title_text, unit, "", False), i // 3, i % 3)
        for i, (key, label) in enumerate((("max_speed", "本次最大速度"),
                                          ("min_speed", "本次最小速度"),
                                          ("fluctuation", "最近最多 5 秒速度波动"))):
            card = self.metric_card(key, label, "m/s", "", False)
            self.metric_labels[key].setWordWrap(True)
            metric_grid.addWidget(card, 2, i)
        monitor.addLayout(metric_grid, 5)
        layout.addLayout(monitor)
        self.values = QLabel("等待采集")
        self.values.setWordWrap(True)
        self.values.setObjectName("muted")
        layout.addWidget(self.values)

        pg.setConfigOptions(antialias=True, background="#ffffff", foreground="#17324a")
        self.plots = {}
        self.long_minutes = QSpinBox()
        self.long_minutes.setRange(1, MAX_CHART_MINUTES)
        self.long_minutes.setValue(5)
        self.long_minutes.setSuffix(" 分钟")
        self.long_source = QComboBox()
        self.long_source.addItem("实时运行速度", "speed")
        self.long_source.addItem("5 秒平均速度", "average_speed")
        self.long_minutes.valueChanged.connect(self.update_long_plot)
        self.long_source.currentIndexChanged.connect(self.update_long_plot)
        plot_definitions = (
            ("speed", "03 / 速度趋势 · 最近 60 秒", "速度 (m/s)",
             (("speed", "实时速度", "#c06a10"), ("smooth_speed", "平滑速度", "#078461"))),
            ("acceleration", "04 / 加速度趋势 · 最近 60 秒", "加速度 (m/s²)",
             (("acceleration", "实时加速度", "#c06a10"),
              ("smooth_acceleration", "平滑加速度", "#078461"))),
            ("custom", "05 / 自定义速度趋势", "速度 (m/s)",
             (("custom", "所选速度", "#1761b0"),)),
        )
        self.curves = {}
        self.scale_labels = {}
        charts = QGridLayout()
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
            if key in ("speed", "custom"):
                self.scale_labels[key] = QLabel()
                self.scale_labels[key].setObjectName("muted")
                card_heading.addWidget(self.scale_labels[key])
            legend = QLabel("　".join(
                f'<span style="color:{color};font-weight:bold;">━━</span> {label}'
                for _, label, color in series))
            legend.setStyleSheet("color: #435d74; font-size: 10pt;")
            legend.setWordWrap(True)
            card_layout.addLayout(card_heading)
            card_layout.addWidget(legend)
            if key == "custom":
                options = QHBoxLayout()
                options.addWidget(QLabel("显示时间"))
                options.addWidget(self.long_minutes)
                options.addWidget(QLabel("数据来源"))
                options.addWidget(self.long_source)
                options.addStretch()
                card_layout.addLayout(options)
            plot = pg.PlotWidget()
            plot.setMinimumHeight(260)
            plot.setMaximumHeight(300)
            plot.showGrid(x=True, y=True, alpha=0.18)
            plot.setLabel("left", y_label)
            plot.setLabel("bottom", "采集时间 (min)" if key == "custom" else "采集时间 (s)")
            plot.getAxis("left").setWidth(90)
            plot.getAxis("bottom").setHeight(42)
            plot.getAxis("left").enableAutoSIPrefix(False)
            plot.getAxis("bottom").enableAutoSIPrefix(False)
            plot.getAxis("left").setStyle(maxTickLevel=0, autoExpandTextSpace=False)
            plot.getAxis("bottom").setStyle(maxTickLevel=0, hideOverlappingLabels=False)
            if key != "custom":
                plot.getAxis("bottom").setTickSpacing(levels=[(10.0, 0.0)])
            plot.setMouseEnabled(x=False, y=False)
            plot.hideButtons()
            for name, label, color in series:
                self.curves[name] = plot.plot(pen=pg.mkPen(color, width=2.5), connect="finite",
                                              autoDownsample=True, clipToView=True)
            self.plots[key] = plot
            if key == "acceleration":
                plot.setXLink(self.plots["speed"])
            card_layout.addWidget(plot)
            if key == "custom":
                charts.addWidget(card, 1, 0)
            else:
                charts.addWidget(card, 0, 0 if key == "speed" else 1)
        layout.addLayout(charts)
        self.plots["speed"].setXRange(0, 60, padding=0)
        self.plots["custom"].setXRange(0, self.long_minutes.value(), padding=0)
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
        if not hasattr(self, "plots") or "custom" not in self.plots:
            return
        bound = 1.2 * self.limit.value()
        for key in ("speed", "custom"):
            self.plots[key].setYRange(-bound, bound, padding=0)
            self.plots[key].getAxis("left").setTickSpacing(levels=[(bound / 2, 0.0)])
            self.scale_labels[key].setText(f"纵轴 ±{bound:.4f} m/s")
        self.gauge.set_value(self.gauge.speed, self.limit.value())

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
                max_speed=self.limit.value(), average_seconds=5.0
            )
            prefix = "simulation" if simulation else "encoder"
            filename = self.output_dir / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S_%f}.csv"
            csv_file = filename.open("x", encoding="utf-8-sig", newline="")
            try:
                writer = csv.writer(csv_file)
                writer.writerow(["elapsed_s", "count", "displacement_m", "travel_m",
                                 "speed_m_s", "smooth_speed_m_s", "average_speed_5s_m_s",
                                 "acceleration_m_s2", "smooth_acceleration_m_s2",
                                 "run_min_speed_m_s", "run_min_elapsed_s",
                                 "run_max_speed_m_s", "run_max_elapsed_s"])
                csv_file.flush()
            except OSError:
                csv_file.close()
                raise
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "无法启动", str(exc))
            return
        self.calculator, self.csv_file, self.csv_writer = calculator, csv_file, writer
        self.stats = SpeedStats()
        self.history = {name: deque() for name in
                        ("time", "speed", "smooth_speed", "average_speed",
                         "acceleration", "smooth_acceleration", "displacement", "travel")}
        for curve in self.curves.values():
            curve.clear()
        for label in self.metric_labels.values():
            label.setText("--")
        self.gauge.set_value(None, self.limit.value())
        self.plots["speed"].setXRange(0, 60, padding=0)
        self.plots["custom"].setXRange(0, self.long_minutes.value(), padding=0)
        self.last_long_redraw = 0.0
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
            self.stats.add(measurement.elapsed, measurement.speed)
            minimum, maximum = self.stats.minimum, self.stats.maximum
            row = [measurement.elapsed, measurement.count, measurement.displacement,
                   measurement.travel, measurement.speed, measurement.smooth_speed,
                   measurement.average_speed, measurement.acceleration,
                   measurement.smooth_acceleration,
                   minimum[1] if minimum else None, minimum[0] if minimum else None,
                   maximum[1] if maximum else None, maximum[0] if maximum else None]
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
        while self.history["time"] and self.history["time"][0] < measurement.elapsed - MAX_CHART_MINUTES * 60:
            for samples in self.history.values():
                samples.popleft()
        self.latest = measurement

    def on_error(self, message):
        self.failure = message
        self.accepting = False
        self.statusBar().showMessage(f"采集已中断：{message}")
        self.values.setText(f"数据已失效：{message}")
        for label in self.metric_labels.values():
            label.setText("--")
        self.gauge.set_value(None, self.limit.value())

    def stop(self):
        self.accepting = False
        self.gauge.set_value(None, self.limit.value())
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
            self.gauge.set_value(None, self.limit.value())
            return
        m = self.latest
        self.values.setText("模拟数据，未连接设备" if self.simulation else "正在采集真实编码器数据")
        self.gauge.set_value(m.speed, self.limit.value())
        for name in ("smooth_speed", "acceleration", "smooth_acceleration", "displacement", "travel"):
            label = self.metric_labels[name]
            value = getattr(m, name)
            label.setText("--" if value is None else f"{value:.4f}")
        for name, extreme in (("min_speed", self.stats.minimum), ("max_speed", self.stats.maximum)):
            self.metric_labels[name].setText(
                "--" if extreme is None else f"{extreme[1]:.4f}\n发生于 +{extreme[0]:.1f} s")
        fluctuation = self.stats.fluctuation()
        self.metric_labels["fluctuation"].setText(
            "--" if fluctuation is None else f"{fluctuation[0]:.4f} ~ {fluctuation[1]:.4f}")
        names = ("time", "speed", "smooth_speed", "acceleration", "smooth_acceleration")
        recent = []
        for row in zip(*(reversed(self.history[name]) for name in names)):
            if row[0] < m.elapsed - 60.0:
                break
            recent.append(row)
        recent.reverse()
        columns = list(zip(*recent)) if recent else [()] * len(names)
        for index, name in enumerate(names[1:], 1):
            self.curves[name].setData(columns[0], columns[index])
        end = max(60.0, m.elapsed)
        self.plots["speed"].setXRange(end - 60.0, end, padding=0)
        if time.monotonic() - self.last_long_redraw >= 1.0:
            self.update_long_plot()

    def update_long_plot(self, *_):
        if "custom" not in self.plots:
            return
        if not self.history or not self.history["time"]:
            self.plots["custom"].setXRange(0, self.long_minutes.value(), padding=0)
            return
        source = self.long_source.currentData()
        end = self.history["time"][-1]
        cutoff = end - self.long_minutes.value() * 60
        rows = []
        for timestamp, value in zip(reversed(self.history["time"]), reversed(self.history[source])):
            if timestamp < cutoff:
                break
            rows.append((timestamp / 60, value))
        rows.reverse()
        x, y = zip(*rows) if rows else ((), ())
        self.curves["custom"].setData(x, y)
        end_minutes = max(float(self.long_minutes.value()), end / 60)
        self.plots["custom"].setXRange(end_minutes - self.long_minutes.value(), end_minutes, padding=0)
        self.last_long_redraw = time.monotonic()

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
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    window = EncoderDashboard()
    window.show()
    sys.exit(app.exec_())
