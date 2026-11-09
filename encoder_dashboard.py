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
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
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
        self.resize(1250, 900)
        self.worker = self.calculator = self.csv_file = self.csv_writer = None
        self.accepting = False
        self.failure = None
        self.last_sample_time = None
        self.history = {}
        self.confirmed_port = None
        self.simulation = False
        self.settings = QSettings("BA ENG", "Encoder Monitor")
        self.output_dir = (Path.home() / "Documents" / "EncoderMonitor"
                           if getattr(sys, "frozen", False) else Path.cwd())
        self.init_ui()
        self.refresh_ports()
        self.display_timer = QTimer(self)
        self.display_timer.timeout.connect(self.update_display)
        self.display_timer.start(100)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        notice = QLabel("BA ENG Internal use only")
        notice.setStyleSheet("color: #c62828; font-weight: bold;")
        layout.addWidget(notice)
        heading = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QPixmap(str(resource_path("web/comau-logo.png"))).scaled(
            85, 85, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        heading.addWidget(logo)
        heading.addWidget(QLabel("板链运行监控  |  作者：YD"), 1)
        self.mode_label = QLabel("真实编码器 · 未连接")
        self.mode_label.setStyleSheet("font-weight: bold; color: #174e80;")
        heading.addWidget(self.mode_label)
        layout.addLayout(heading)
        controls = QHBoxLayout()
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
        self.turns = QSpinBox()
        self.turns.setRange(0, 32768)
        self.turns.setSpecialValueText("未知（不自动回绕）")
        self.turns.setToolTip("仅填写厂家确认的硬件总圈数；未知时保持 0。")
        self.limit = QDoubleSpinBox()
        self.limit.setRange(0.001, 1000)
        self.limit.setDecimals(3)
        self.limit.setValue(5.0)
        self.limit.setSuffix(" m/s")
        self.limit.setToolTip("超过此绝对速度时停止采集，防止回绕或复位产生错误数据；不是安全保护。")
        form = QFormLayout()
        for label, widget in (("端口", self.port), ("波特率", self.baud),
                              ("站号", self.address), ("轮询周期", self.interval),
                              ("硬件总圈数", self.turns), ("合理速度上限", self.limit)):
            form.addRow(label, widget)
        controls.addLayout(form)
        buttons = QVBoxLayout()
        buttons.addWidget(self.refresh)
        buttons.addWidget(self.confirm_port)
        buttons.addWidget(self.port_status)
        self.connect_button = QPushButton("连接并开始采集")
        self.connect_button.clicked.connect(self.toggle)
        buttons.addWidget(self.connect_button)
        self.simulation_button = QPushButton("模拟输入设置…")
        self.simulation_button.clicked.connect(self.show_simulation_dialog)
        buttons.addWidget(self.simulation_button)
        self.photo_button = QPushButton("选择编码器照片…")
        self.photo_button.clicked.connect(self.select_photo)
        buttons.addWidget(self.photo_button)
        buttons.addStretch()
        controls.addLayout(buttons)
        self.photo = QLabel()
        self.photo.setAlignment(Qt.AlignCenter)
        self.photo.setFixedSize(130, 180)
        self.photo.setWordWrap(True)
        controls.addWidget(self.photo)
        self.load_photo(self.settings.value("encoder_photo", "", type=str))
        self.values = QLabel("等待采集")
        self.values.setWordWrap(True)
        self.values.setStyleSheet("font-size: 15px;")
        controls.addWidget(self.values, 1)
        layout.addLayout(controls)

        pg.setConfigOptions(antialias=True, background="#ffffff", foreground="#17324a")
        self.plots = {}
        plot_definitions = (
            ("speed", "速度 (m/s)", (("speed", "实时速度", "#b86b0a"),
                                      ("smooth_speed", "平滑速度", "#078461"),
                                      ("average_speed", "3 秒平均速度", "#1761b0"))),
            ("acceleration", "加速度 (m/s²)", (("acceleration", "实时加速度", "#b86b0a"),
                                               ("smooth_acceleration", "平滑速度加速度", "#078461"),
                                               ("average_acceleration", "3 秒平均加速度", "#1761b0"))),
            ("distance", "距离 (m)", (("displacement", "净位移", "#1761b0"),
                                      ("travel", "累计行程", "#b86b0a"))),
        )
        self.curves = {}
        for key, title, series in plot_definitions:
            plot = pg.PlotWidget(title=title)
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setLabel("bottom", "采集时间", units="s")
            plot.addLegend()
            for name, label, color in series:
                self.curves[name] = plot.plot(name=label, pen=pg.mkPen(color, width=2))
            self.plots[key] = plot
            layout.addWidget(plot)
        self.statusBar().showMessage("未连接。硬件圈数未知时不会自动处理回绕。")

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
                       self.address, self.interval, self.turns, self.limit,
                       self.simulation_button):
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
        try:
            calculator = MeasurementCalculator(
                hardware_turns=SimulatorWorker.TURNS if simulation else self.turns.value() or None,
                max_speed=self.limit.value()
            )
            self.output_dir.mkdir(parents=True, exist_ok=True)
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
        self.history = {name: deque(maxlen=1200) for name in ("time", *self.curves)}
        for curve in self.curves.values():
            curve.clear()
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
        for name in self.curves:
            value = getattr(measurement, name)
            self.history[name].append(float("nan") if value is None else value)
        self.latest = measurement

    def on_error(self, message):
        self.failure = message
        self.accepting = False
        self.statusBar().showMessage(f"采集已中断：{message}")
        self.values.setText(f"数据已失效：{message}")

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
            return
        m = self.latest
        def fmt(value, unit):
            return "等待数据" if value is None else f"{value:.4f} {unit}"
        self.values.setText(
            f"实时速度：{fmt(m.speed, 'm/s')}    平滑速度：{fmt(m.smooth_speed, 'm/s')}\n"
            f"3 秒平均速度：{fmt(m.average_speed, 'm/s')}\n"
            f"实时加速度：{fmt(m.acceleration, 'm/s²')}    "
            f"平滑加速度：{fmt(m.smooth_acceleration, 'm/s²')}\n"
            f"3 秒平均加速度：{fmt(m.average_acceleration, 'm/s²')}\n"
            f"净位移：{m.displacement:.4f} m    累计行程：{m.travel:.4f} m"
        )
        x = list(self.history["time"])
        for name, curve in self.curves.items():
            curve.setData(x, list(self.history[name]))

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
