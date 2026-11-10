import csv
import importlib.util
import os
import select
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from encoder_core import calculate_crc, position_request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HAS_GUI = all(importlib.util.find_spec(name) for name in ("PyQt5", "serial", "pyqtgraph"))

if HAS_GUI:
    from PyQt5.QtWidgets import QApplication
    from encoder_dashboard import EncoderDashboard


@unittest.skipUnless(HAS_GUI and hasattr(os, "openpty"), "需要 Qt、pyserial 和伪串口")
class DashboardIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.old_dir = os.getcwd()
        self.directory = tempfile.TemporaryDirectory()
        os.chdir(self.directory.name)
        self.master, self.slave = os.openpty()
        self.stop_simulator = threading.Event()
        self.bad_crc = False
        self.fail_requests = 0
        self.drop_requests = 0
        self.requests = []
        self.simulator = threading.Thread(target=self.respond, daemon=True)
        self.simulator.start()
        self.window = EncoderDashboard()
        self.window.output_dir = Path(self.directory.name)
        self.window.port.addItem(os.ttyname(self.slave), os.ttyname(self.slave))
        self.window.port.setCurrentIndex(self.window.port.count() - 1)
        self.window.confirm_selected_port()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.stop_simulator.set()
        self.simulator.join(timeout=1)
        os.close(self.master)
        os.close(self.slave)
        os.chdir(self.old_dir)
        self.directory.cleanup()

    def respond(self):
        buffered = b""
        count = 100000
        while not self.stop_simulator.is_set():
            if not select.select([self.master], [], [], 0.05)[0]:
                continue
            buffered += os.read(self.master, 1024)
            while len(buffered) >= 8:
                request, buffered = buffered[:8], buffered[8:]
                self.requests.append(request)
                if self.drop_requests:
                    self.drop_requests -= 1
                    continue
                frame = b"\x01\x03\x04" + count.to_bytes(4, "big")
                bad = self.bad_crc or self.fail_requests > 0
                if self.fail_requests:
                    self.fail_requests -= 1
                crc = b"\x00\x00" if bad else calculate_crc(frame)
                os.write(self.master, frame + crc)
                count += 500

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.005)
        self.fail(f"Timed out; status: {self.window.statusBar().currentMessage()}")

    def test_acquisition_csv_and_restart_reset(self):
        self.window.start()
        self.wait_for(lambda: hasattr(self.window, "latest")
                      and self.window.latest.average_speed is not None, timeout=8)
        self.assertEqual(self.requests[0], position_request(1))
        self.assertIsNotNone(self.window.latest.average_speed)
        self.assertGreater(self.window.latest.displacement, 0)
        self.window.stop()
        self.wait_for(lambda: self.window.worker is None)
        csv_files = list(Path(".").glob("encoder_*.csv"))
        self.assertEqual(len(csv_files), 1)
        with csv_files[0].open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertGreater(len(rows), 3)
        self.assertEqual(rows[0]["displacement_m"], "0.0")
        self.assertEqual(rows[0]["speed_m_s"], "")
        self.assertNotEqual(rows[-1]["average_speed_5s_m_s"], "")
        self.assertNotEqual(rows[-1]["run_max_elapsed_s"], "")
        self.assertNotIn("average_speed_3s_m_s", rows[-1])
        samples = [row for row in rows if row["speed_m_s"]]
        minimum = min(samples, key=lambda row: float(row["speed_m_s"]))
        maximum = max(samples, key=lambda row: float(row["speed_m_s"]))
        self.assertEqual(float(rows[-1]["run_min_speed_m_s"]), float(minimum["speed_m_s"]))
        self.assertEqual(float(rows[-1]["run_min_elapsed_s"]), float(minimum["elapsed_s"]))
        self.assertEqual(float(rows[-1]["run_max_speed_m_s"]), float(maximum["speed_m_s"]))
        self.assertEqual(float(rows[-1]["run_max_elapsed_s"]), float(maximum["elapsed_s"]))

        self.window.start()
        self.wait_for(lambda: len(self.window.history["time"]) >= 2)
        self.assertEqual(self.window.history["displacement"][0], 0)
        self.assertLess(self.window.history["time"][0], 0.001)
        self.assertLess(self.window.stats.maximum[0], 1)
        self.window.stop()
        self.wait_for(lambda: self.window.worker is None)
        self.assertEqual(len(list(Path(".").glob("encoder_*.csv"))), 2)

    def test_crc_failure_stops_and_surfaces_error(self):
        self.bad_crc = True
        self.window.start()
        self.wait_for(lambda: self.window.worker is None)
        self.assertIn("CRC", self.window.statusBar().currentMessage())
        self.assertIn("失效", self.window.values.text())
        self.assertIn("连续 5 次", self.window.statusBar().currentMessage())

    def test_temporary_crc_errors_recover_and_mark_gap(self):
        self.window.start()
        self.wait_for(lambda: len(self.window.history["time"]) > 3)
        self.fail_requests = 2
        self.wait_for(lambda: self.window.had_gap)
        self.wait_for(lambda: self.window.had_gap and not self.window.gap_pending)
        self.assertIsNotNone(self.window.worker)
        self.wait_for(lambda: self.window.latest.speed is not None)
        self.window.update_display()
        self.assertIn("累计行程可能不准确", self.window.values.text())
        self.window.stop()
        self.wait_for(lambda: self.window.worker is None)
        with next(Path(".").glob("encoder_*.csv")).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        recovered = [row for row in rows if row["communication_gap"] == "1"]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["speed_m_s"], "")
        self.assertEqual(recovered[0]["acceleration_m_s2"], "")

    def test_timeout_recovers_without_stopping(self):
        self.drop_requests = 2
        self.window.start()
        self.wait_for(lambda: self.window.had_gap and not self.window.gap_pending)
        self.wait_for(lambda: len(self.window.history["time"]) >= 2)
        self.assertIsNotNone(self.window.worker)

    def test_persistent_timeouts_stop_after_five_attempts(self):
        self.drop_requests = 10
        self.window.start()
        self.wait_for(lambda: self.window.worker is None)
        self.assertEqual(len(self.requests), 5)
        self.assertIn("连续 5 次", self.window.statusBar().currentMessage())

    def test_failure_counter_resets_after_recovery(self):
        self.fail_requests = 4
        self.window.start()
        self.wait_for(lambda: self.window.had_gap and not self.window.gap_pending)
        self.fail_requests = 4
        self.wait_for(lambda: self.window.gap_pending)
        self.wait_for(lambda: not self.window.gap_pending)
        self.assertIsNotNone(self.window.worker)

    def test_cp210x_detection_and_confirmation(self):
        def port(device, description, vid=None, pid=None):
            return SimpleNamespace(device=device, description=description, vid=vid, pid=pid)
        candidates = [port("COM4", "Other"), port("COM7", "USB Serial", 0x10c4, 0xea60)]
        with patch("encoder_dashboard.serial.tools.list_ports.comports", return_value=candidates):
            self.window.port.setCurrentIndex(0)
            self.window.refresh_ports()
        self.assertEqual(self.window.port.currentData(), "COM7")
        self.assertIsNone(self.window.confirmed_port)
        self.assertIn("请确认", self.window.port_status.text())
        self.window.confirm_selected_port()
        self.assertEqual(self.window.confirmed_port, "COM7")
        candidates.append(port("COM8", "Silicon Labs CP210x USB to UART Bridge"))
        with patch("encoder_dashboard.serial.tools.list_ports.comports", return_value=candidates):
            self.window.port.setCurrentIndex(0)
            self.window.refresh_ports()
        self.assertIsNone(self.window.port.currentData())
        self.assertIn("多个", self.window.port_status.text())
        with patch("encoder_dashboard.serial.tools.list_ports.comports", return_value=[]):
            self.window.refresh_ports()
        self.assertIsNone(self.window.port.currentData())
        self.assertIn("未找到", self.window.port_status.text())

    def test_dashboard_password_and_relock(self):
        self.window.show()
        self.app.processEvents()
        self.assertFalse(self.window.professional)
        self.assertTrue(self.window.baud.isHidden())
        self.assertTrue(self.window.chart_cards["custom"].isHidden())
        self.assertTrue(self.window.metric_cards["smooth_speed"].isHidden())
        for key in ("travel", "max_speed", "min_speed"):
            self.assertFalse(self.window.metric_cards[key].isHidden())
        with patch("encoder_dashboard.QInputDialog.getText", return_value=("bad", True)), \
                patch("encoder_dashboard.QMessageBox.warning") as warning:
            self.window.toggle_dashboard()
            warning.assert_called_once()
        self.assertFalse(self.window.professional)
        with patch("encoder_dashboard.QInputDialog.getText", return_value=("ENGNB", True)):
            self.window.toggle_dashboard()
        self.assertTrue(self.window.professional)
        self.assertFalse(self.window.baud.isHidden())
        self.assertFalse(self.window.chart_cards["custom"].isHidden())
        self.window.toggle_dashboard()
        self.assertFalse(self.window.professional)
        self.assertTrue(self.window.chart_cards["custom"].isHidden())

    def test_closing_running_and_stopped_window(self):
        self.window.start()
        self.wait_for(lambda: len(self.window.history["time"]) >= 2)
        self.window.close()
        self.app.processEvents()
        self.assertIsNone(self.window.worker)
        self.assertIsNone(self.window.csv_file)
        before = len(list(Path(".").glob("encoder_*.csv")))
        self.window.close()
        self.app.processEvents()
        self.assertEqual(len(list(Path(".").glob("encoder_*.csv"))), before)

    def test_port_selection_and_default_baud(self):
        self.assertEqual(self.window.baud.currentText(), "115200")
        self.assertEqual(self.window.confirmed_port, os.ttyname(self.slave))
        self.window.port.setCurrentIndex(0)
        self.assertIsNone(self.window.confirmed_port)
        self.window.port.setCurrentIndex(1)
        self.assertIsNone(self.window.confirmed_port)
        self.window.confirm_selected_port()
        self.assertEqual(self.window.confirmed_port, os.ttyname(self.slave))

    def test_unconfirmed_port_never_starts_acquisition(self):
        self.window.port.setCurrentIndex(0)
        self.window.port.setCurrentIndex(1)
        with patch("encoder_dashboard.QMessageBox.warning") as warning:
            self.window.start()
        warning.assert_called_once()
        self.assertIsNone(self.window.worker)
        self.assertFalse(self.requests)

    def test_real_worker_uses_confirmed_port_and_115200(self):
        self.window.start()
        self.assertEqual(self.window.worker.port, os.ttyname(self.slave))
        self.assertEqual(self.window.worker.baud, 115200)
        self.wait_for(lambda: len(self.window.history["time"]) > 1)
        self.assertIn("真实编码器", self.window.mode_label.text())
        self.assertIn("已收到真实编码器有效数据", self.window.statusBar().currentMessage())
        self.window.stop()
        self.wait_for(lambda: self.window.worker is None)

    def test_simulation_is_explicit_and_never_uses_serial(self):
        self.window.start_simulation(0.6, 0.3)
        self.wait_for(lambda: len(self.window.history["time"]) > 3)
        self.assertIn("模拟输入", self.window.mode_label.text())
        self.assertFalse(self.requests)
        self.assertGreater(self.window.latest.speed, 0)
        self.window.stop()
        self.wait_for(lambda: self.window.worker is None)
        self.assertTrue(list(Path(".").glob("simulation_*.csv")))

    def test_photo_selection_removed(self):
        self.assertFalse(hasattr(self.window, "photo_button"))
        self.assertFalse(hasattr(self.window, "load_photo"))

    def test_simulation_stops_promptly_with_long_interval(self):
        self.window.interval.setValue(5000)
        self.window.start_simulation(0.6, 0.3)
        self.wait_for(lambda: len(self.window.history["time"]) > 0)
        self.window.stop()
        self.wait_for(lambda: self.window.worker is None, timeout=1)

    def test_select_directory_is_required_and_records_there(self):
        self.window.output_dir = None
        with patch("encoder_dashboard.QMessageBox.warning") as warning:
            self.window.start()
        warning.assert_called_once()
        self.assertIsNone(self.window.worker)
        with patch("encoder_dashboard.QFileDialog.getExistingDirectory",
                   return_value=self.directory.name):
            self.window.select_output_directory()
        self.assertEqual(self.window.output_dir, Path(self.directory.name))
        self.window.start()
        self.wait_for(lambda: len(self.window.history["time"]) > 1)
        self.window.stop()
        self.wait_for(lambda: self.window.worker is None)
        self.assertTrue(list(Path(self.directory.name).glob("encoder_*.csv")))

    def test_removed_directory_blocks_start(self):
        self.window.output_dir = Path(self.directory.name) / "missing"
        with patch("encoder_dashboard.QMessageBox.warning") as warning:
            self.window.start()
        warning.assert_called_once()
        self.assertIsNone(self.window.worker)

    def test_cancelled_directory_selection_preserves_selection(self):
        original = self.window.output_dir
        with patch("encoder_dashboard.QFileDialog.getExistingDirectory", return_value=""):
            self.window.select_output_directory()
        self.assertEqual(self.window.output_dir, original)

    def test_speed_ranges_legends_and_aligned_time_axes(self):
        self.window.resize(1200, 900)
        self.window.show()
        self.app.processEvents()
        self.window.limit.setValue(2.5)
        self.app.processEvents()
        self.assertEqual(set(self.window.plots), {"speed", "custom", "acceleration"})
        self.assertIn("custom", self.window.curves)
        self.assertNotIn("displacement", self.window.curves)
        for key in ("speed", "custom"):
            self.assertAlmostEqual(self.window.plots[key].viewRange()[1][0], -3.0)
            self.assertAlmostEqual(self.window.plots[key].viewRange()[1][1], 3.0)
            self.assertEqual(self.window.scale_labels[key].text(), "纵轴 ±3.0000 m/s")
        self.window.plots["speed"].setXRange(10, 40, padding=0)
        self.app.processEvents()
        self.assertEqual(self.window.plots["acceleration"].viewRange()[0], [10, 40])
        self.assertEqual(self.window.plots["acceleration"].getAxis("left").width(), 90)
        from PyQt5.QtWidgets import QLabel
        texts = [label.text() for label in self.window.findChildren(QLabel)]
        for label in ("实时速度", "平滑速度", "实时加速度", "平滑加速度", "最近最多 5 秒速度波动"):
            self.assertTrue(any(label in text for text in texts), label)
        self.assertFalse(any("3 秒" in text for text in texts))
        self.assertIn("BA ENG INTERNAL USE ONLY", texts)
        self.assertEqual(self.window.curves["custom"].opts["pen"].color().name(), "#1761b0")
        self.assertFalse(hasattr(self.window, "turns"))

    def test_long_chart_sources_and_windows(self):
        from collections import deque
        self.window.history = {
            "time": deque([0., 4., 5., 61., 120.]),
            "speed": deque([float("nan"), 1., 2., 3., 4.]),
            "average_speed": deque([float("nan"), float("nan"), 1.5, 2.5, 3.5]),
        }
        self.window.long_minutes.setValue(1)
        self.window.update_long_plot()
        x, y = self.window.curves["custom"].getData()
        self.assertEqual(list(y), [3., 4.])
        self.assertAlmostEqual(x[0], 61 / 60)
        self.assertEqual(self.window.plots["custom"].viewRange()[0], [1, 2])
        self.window.long_minutes.setValue(2)
        self.window.long_source.setCurrentIndex(1)
        x, y = self.window.curves["custom"].getData()
        self.assertEqual(list(y[2:]), [1.5, 2.5, 3.5])
        self.assertEqual(self.window.plots["custom"].viewRange()[0], [0, 2])
        self.assertEqual(self.window.long_minutes.maximum(), 60)

    def test_stale_data_clears_gauge(self):
        self.window.start_simulation(0.6, 0.3)
        self.wait_for(lambda: len(self.window.history["time"]) > 2)
        self.window.update_display()
        self.assertIsNotNone(self.window.gauge.speed)
        self.window.last_sample_time = time.monotonic() - 10
        self.window.update_display()
        self.assertIsNone(self.window.gauge.speed)
        self.assertIn("过期", self.window.values.text())

    def test_graph_history_retention_and_recent_window(self):
        import io
        from collections import deque
        from encoder_core import MeasurementCalculator
        from encoder_stats import SpeedStats

        self.window.history = {name: deque() for name in
                               ("time", "speed", "smooth_speed", "average_speed",
                                "acceleration", "smooth_acceleration", "displacement", "travel")}
        self.window.calculator = MeasurementCalculator(average_seconds=5)
        self.window.csv_file = io.StringIO()
        self.window.csv_writer = csv.writer(self.window.csv_file)
        self.window.stats = SpeedStats()
        self.window.accepting = True
        self.window.last_sample_time = 1
        now = int(time.monotonic())
        for elapsed in (0, 1, 3599, 3600, 3601):
            self.window.on_sample(now + elapsed, 0)
        self.assertEqual(list(self.window.history["time"]), [1, 3599, 3600, 3601])
        self.window.last_sample_time = time.monotonic()
        self.window.update_display()
        self.assertEqual(self.window.plots["speed"].viewRange()[0], [3541, 3601])
        self.assertEqual(self.window.plots["acceleration"].viewRange()[0], [3541, 3601])
        x, _ = self.window.curves["speed"].getData()
        self.assertEqual(list(x), [3599, 3600, 3601])
        self.window.csv_file.close()
        self.window.csv_file = None


if __name__ == "__main__":
    unittest.main()
