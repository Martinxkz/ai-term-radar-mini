import csv
import importlib.util
import os
import select
import tempfile
import threading
import time
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
                frame = b"\x01\x03\x04" + count.to_bytes(4, "big")
                crc = b"\x00\x00" if self.bad_crc else calculate_crc(frame)
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
                      and self.window.latest.average_acceleration is not None)
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
        self.assertNotEqual(rows[-1]["average_speed_3s_m_s"], "")

        self.window.start()
        self.wait_for(lambda: len(self.window.history["time"]) >= 2)
        self.assertEqual(self.window.history["displacement"][0], 0)
        self.assertLess(self.window.history["time"][0], 0.001)
        self.window.stop()
        self.wait_for(lambda: self.window.worker is None)
        self.assertEqual(len(list(Path(".").glob("encoder_*.csv"))), 2)

    def test_crc_failure_stops_and_surfaces_error(self):
        self.bad_crc = True
        self.window.start()
        self.wait_for(lambda: self.window.worker is None)
        self.assertIn("CRC", self.window.statusBar().currentMessage())
        self.assertIn("失效", self.window.values.text())

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

    def test_photo_can_be_loaded(self):
        from encoder_dashboard import resource_path
        self.assertTrue(self.window.load_photo(str(resource_path("web/comau-logo.png"))))
        self.assertFalse(self.window.photo.pixmap().isNull())
        self.assertFalse(self.window.load_photo("missing-photo.jpg"))

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
        self.assertEqual(set(self.window.plots), {"speed", "average_speed", "acceleration"})
        self.assertIn("average_speed", self.window.curves)
        self.assertNotIn("displacement", self.window.curves)
        for key in ("speed", "average_speed"):
            self.assertAlmostEqual(self.window.plots[key].viewRange()[1][0], -3.0)
            self.assertAlmostEqual(self.window.plots[key].viewRange()[1][1], 3.0)
            self.assertEqual(self.window.scale_labels[key].text(), "纵轴 ±3.0000 m/s")
        self.window.plots["speed"].setXRange(10, 40, padding=0)
        self.app.processEvents()
        for key in ("average_speed", "acceleration"):
            self.assertEqual(self.window.plots[key].viewRange()[0], [10, 40])
            self.assertEqual(self.window.plots[key].getAxis("left").width(), 90)
            actual = self.window.plots[key].getPlotItem().getViewBox().sceneBoundingRect()
            reference = self.window.plots["speed"].getPlotItem().getViewBox().sceneBoundingRect()
            self.assertAlmostEqual(actual.left(), reference.left(), places=1)
            self.assertAlmostEqual(actual.right(), reference.right(), places=1)
        from PyQt5.QtWidgets import QLabel
        texts = [label.text() for label in self.window.findChildren(QLabel)]
        for label in ("实时速度", "平滑速度", "3 秒平均速度", "实时加速度", "平滑加速度"):
            self.assertTrue(any(label in text for text in texts), label)
        self.assertIn("BA ENG INTERNAL USE ONLY", texts)
        self.assertEqual(self.window.curves["average_speed"].opts["pen"].color().name(), "#1761b0")
        self.assertFalse(hasattr(self.window, "turns"))


if __name__ == "__main__":
    unittest.main()
