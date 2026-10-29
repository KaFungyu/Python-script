import sys
import time
import socket
import threading
import urllib.request
from typing import Optional, Tuple

# Qt 框架自适应 (PySide6 / PyQt6)
try:
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QLineEdit, QFrame
    )
    from PySide6.QtCore import Qt, QTimer, Signal, QObject, QSettings
    from PySide6.QtGui import QPixmap, QImage, QPainter, QColor, QFont, QPen, QShortcut, QKeySequence
except ImportError:
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QLineEdit, QFrame
    )
    from PyQt6.QtCore import Qt, QTimer, pyqtSignal as Signal, QObject, QSettings
    from PyQt6.QtGui import QPixmap, QImage, QPainter, QColor, QFont, QPen, QShortcut, QKeySequence


# ==============================================================================
# 1. 纯以太网双擎流引擎 (SCPI BMP + Web HTTP 自动互备)
# ==============================================================================

class HighSpeedLanEngine(QObject):
    frame_decoded = Signal(QImage)
    connection_changed = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self.ip = "192.168.31.173"
        self.port = 5025
        self.sock: Optional[socket.socket] = None
        self.is_connected = False

        self._running = False
        self._io_lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        
        self.use_web_fallback = False
        self.scpi_fail_count = 0
        self.web_endpoint: Optional[str] = None

    def connect_device(self, ip: str, port: int = 5025) -> Tuple[bool, str]:
        with self._io_lock:
            self._disconnect_internal()
            self.ip = ip.strip()
            self.port = int(port)
            self.use_web_fallback = False
            self.scpi_fail_count = 0
            self.web_endpoint = None

            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
                self.sock.settimeout(1.5)
                self.sock.connect((self.ip, self.port))

                self.is_connected = True
                idn = self._query_fast("*IDN?")
                status_text = f"已连接: {idn if idn else 'ZDS3024'}"
                self.connection_changed.emit(True, status_text)

                self._running = True
                self._worker_thread = threading.Thread(target=self._stream_pipeline, daemon=True)
                self._worker_thread.start()
                return True, status_text

            except Exception as e:
                self._disconnect_internal()
                err_info = f"连接失败: {str(e)}"
                self.connection_changed.emit(False, err_info)
                return False, err_info

    def _disconnect_internal(self):
        self._running = False
        self.is_connected = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def disconnect(self):
        with self._io_lock:
            self._disconnect_internal()
            self.connection_changed.emit(False, "连接已断开")

    def _query_fast(self, cmd: str) -> str:
        try:
            if self.sock:
                self.sock.sendall((cmd.strip() + "\n").encode('utf-8'))
                return self.sock.recv(512).decode('utf-8', errors='ignore').strip()
        except Exception:
            pass
        return ""

    def _stream_pipeline(self):
        while self._running and self.is_connected:
            image = None
            if not self.use_web_fallback:
                image = self._fetch_frame_via_scpi()
                if image is None:
                    self.scpi_fail_count += 1
                    if self.scpi_fail_count >= 3:
                        self.use_web_fallback = True
                else:
                    self.scpi_fail_count = 0
            else:
                image = self._fetch_frame_via_http()

            if image is not None:
                self.frame_decoded.emit(image)
                # 动态微让步，显著降低延迟并释放物理最高帧率
                time.sleep(0.005)
            else:
                time.sleep(0.05)

    def _drain_sock(self):
        if not self.sock:
            return
        self.sock.setblocking(False)
        try:
            while self.sock.recv(4096):
                pass
        except Exception:
            pass
        self.sock.setblocking(True)
        self.sock.settimeout(1.5)

    def _fetch_frame_via_scpi(self) -> Optional[QImage]:
        with self._io_lock:
            if not self.is_connected or not self.sock:
                return None
            try:
                self._drain_sock()
                self.sock.sendall(b":DISPlay:DATA?\n")

                found_hash = False
                for _ in range(128):
                    ch = self.sock.recv(1)
                    if not ch:
                        return None
                    if ch == b'#':
                        found_hash = True
                        break

                if not found_hash:
                    return None

                dig_char = self.sock.recv(1)
                if not dig_char or not dig_char.isdigit():
                    return None
                dig_len = int(dig_char)

                len_bytes = bytearray()
                while len(len_bytes) < dig_len:
                    chunk = self.sock.recv(dig_len - len(len_bytes))
                    if not chunk:
                        return None
                    len_bytes.extend(chunk)
                total_len = int(len_bytes.decode('ascii'))

                payload = bytearray(total_len)
                view = memoryview(payload)
                bytes_read = 0
                while bytes_read < total_len:
                    n = self.sock.recv_into(view[bytes_read:], total_len - bytes_read)
                    if n == 0:
                        return None
                    bytes_read += n

                self.sock.settimeout(0.05)
                try:
                    self.sock.recv(1)
                except Exception:
                    pass
                self.sock.settimeout(1.5)

                image = QImage()
                if image.loadFromData(payload):
                    return image
            except Exception:
                pass
        return None

    def _fetch_frame_via_http(self) -> Optional[QImage]:
        endpoints = [
            self.web_endpoint,
            f"http://{self.ip}/display.bmp",
            f"http://{self.ip}/screen.bmp",
            f"http://{self.ip}/waveform.png",
            f"http://{self.ip}/display.png",
            f"http://{self.ip}/screenshot.bmp",
            f"http://{self.ip}:80/display.bmp",
        ]
        
        for url in endpoints:
            if not url:
                continue
            try:
                req = urllib.request.Request(
                    f"{url}?t={int(time.time()*1000)}",
                    headers={'User-Agent': 'Mozilla/5.0'}
                )
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        raw_data = resp.read()
                        img = QImage()
                        if img.loadFromData(raw_data):
                            self.web_endpoint = url
                            return img
            except Exception:
                continue
        return None


# ==============================================================================
# 2. iOS 26 Liquid White 纯净样式表
# ==============================================================================

IOS26_LIQUID_WHITE_QSS = """
QWidget#CentralWidget {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #F7F9FC, stop:0.5 #EFF3F8, stop:1 #E7EDF5);
    color: #1D1D1F;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
}

QFrame.GlassCard {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255, 255, 255, 0.94),
        stop:1 rgba(255, 255, 255, 0.74));
    border: 1px solid rgba(255, 255, 255, 0.95);
    border-bottom: 1px solid rgba(0, 0, 0, 0.08);
    border-radius: 16px;
}

QLineEdit {
    background: rgba(255, 255, 255, 0.95);
    border: 1px solid rgba(0, 0, 0, 0.12);
    border-radius: 10px;
    color: #1D1D1F;
    padding: 5px 8px;
    font-size: 12px;
}
QLineEdit:focus {
    border: 1.5px solid #0071E3;
}

QPushButton.ConnectBtn {
    background: #FFFFFF;
    border: 1px solid rgba(0, 0, 0, 0.14);
    border-radius: 13px;
    font-size: 12px;
    font-weight: 600;
    color: #1D1D1F;
    padding: 5px 14px;
}
QPushButton.ConnectBtn:hover {
    border: 1px solid #0071E3;
    color: #0071E3;
}

QPushButton.PillCopy {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0071E3, stop:1 #47A1FF);
    border: 1px solid rgba(255, 255, 255, 0.9);
    border-radius: 13px;
    color: #FFFFFF;
    font-size: 12px;
    font-weight: 700;
    padding: 5px 18px;
    min-height: 24px;
}
QPushButton.PillCopy:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0077ED, stop:1 #62B0FF);
}
QPushButton.PillCopy:pressed {
    background: #005BB5;
}
"""


# ==============================================================================
# 3. 浮动 Toast 提示组件
# ==============================================================================

class ToastNotification(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("""
            background: rgba(25, 25, 30, 0.92);
            color: #FFFFFF;
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 14px;
            font-size: 12px;
            font-weight: 600;
            padding: 8px 18px;
        """)
        self.hide()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)

    def show_msg(self, text: str, duration_ms: int = 1800):
        self.setText(text)
        self.adjustSize()
        if self.parent():
            parent_rect = self.parent().rect()
            x = (parent_rect.width() - self.width()) // 2
            self.move(x, 66)
        self.show()
        self.raise_()
        self.timer.start(duration_ms)


# ==============================================================================
# 4. 示波器 1:1 原生视口 (800×480 精确无损像素)
# ==============================================================================

class OscilloscopeScreenView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # 严格锁定 800x480 原生点对点分辨率
        self.setFixedSize(800, 480)
        self.pixmap = QPixmap(800, 480)
        self.render_standby_screen()

    def update_image(self, image: QImage):
        if image.width() != 800 or image.height() != 480:
            image = image.scaled(800, 480, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.pixmap = QPixmap.fromImage(image)
        self.update()

    def render_standby_screen(self):
        img = QImage(800, 480, QImage.Format.Format_RGB32)
        img.fill(QColor(14, 17, 22))
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        grid_pen = QPen(QColor(42, 50, 60), 1, Qt.PenStyle.DotLine)
        painter.setPen(grid_pen)
        for x in range(0, 800, 50):
            painter.drawLine(x, 0, x, 440)
        for y in range(0, 440, 50):
            painter.drawLine(0, y, 800, y)

        painter.fillRect(0, 440, 800, 40, QColor(22, 26, 34))

        import math
        painter.setPen(QPen(QColor(255, 204, 0), 2))
        prev_y = 220
        for px in range(0, 800):
            py = int(220 + 60 * math.sin(px * 0.035))
            if px > 0:
                painter.drawLine(px - 1, prev_y, px, py)
            prev_y = py

        painter.setPen(QColor(240, 240, 240))
        painter.setFont(QFont("Monospace", 9, QFont.Weight.Bold))
        painter.drawText(20, 25, "ZDS3024 PLUS (STANDBY)")
        painter.setPen(QColor(255, 204, 0))
        painter.drawText(20, 465, "CH1: 1.00V/div")
        painter.setPen(QColor(52, 199, 89))
        painter.drawText(160, 465, "CH2: 1.00V/div")
        painter.setPen(QColor(0, 122, 255))
        painter.drawText(300, 465, "CH3: 1.00V/div")
        painter.setPen(QColor(175, 82, 222))
        painter.drawText(440, 465, "CH4: 1.00V/div")
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(620, 465, "TB: 1.00us/div  2.00GSa/s")
        painter.end()

        self.pixmap = QPixmap.fromImage(img)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self.pixmap)
        # 精致的 1px 暗黑仪表外壳边框
        painter.setPen(QPen(QColor(0, 0, 0, 40), 1))
        painter.drawRect(0, 0, 799, 479)


# ==============================================================================
# 5. 主控制窗口 (精准几何对齐版)
# ==============================================================================

class ZDSStreamMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.engine = HighSpeedLanEngine()
        self.settings = QSettings("ZLG_ZTMI", "ZDS_LAN_Streamer")

        self.fps_counter = 0
        self.last_fps_time = time.time()

        self.setup_ui()
        self.bind_signals()
        self.load_settings()

    def setup_ui(self):
        self.setWindowTitle("ZDS3024 PLUS 高速屏幕实时同步")

        central = QWidget()
        central.setObjectName("CentralWidget")
        self.setCentralWidget(central)

        main_vbox = QVBoxLayout(central)
        # 左右统一 16px，上下 12px，间距 10px
        main_vbox.setContentsMargins(16, 12, 16, 12)
        main_vbox.setSpacing(10)

        # 1. 顶部纯以太网连接栏 (宽度随父布局与下方完全一致)
        main_vbox.addWidget(self._build_top_bar())

        # 2. 1:1 视口容器 (精确固定 820 x 500，四周均等留白 10px，杜绝任何溢出与挤压)
        self.viewport_frame = QFrame()
        self.viewport_frame.setProperty("class", "GlassCard")
        self.viewport_frame.setFixedSize(820, 500)

        v_layout = QVBoxLayout(self.viewport_frame)
        v_layout.setContentsMargins(10, 10, 10, 10)
        v_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.screen_view = OscilloscopeScreenView()
        v_layout.addWidget(self.screen_view)
        main_vbox.addWidget(self.viewport_frame, 0, Qt.AlignmentFlag.AlignCenter)

        # Toast 提示
        self.toast = ToastNotification(self)

        # 快捷键支持: Ctrl+C 快速复制
        copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self)
        copy_shortcut.activated.connect(self.copy_image_to_clipboard)

        # 关键：自底向上精准计算窗口物理尺寸，彻底消除尺寸倒挂与任何裁切
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setProperty("class", "GlassCard")
        bar.setFixedWidth(820)  # 与下方视口卡片严格等宽对齐
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 7, 14, 7)
        layout.setSpacing(8)

        lbl_ip = QLabel("以太网 IP:")
        lbl_ip.setStyleSheet("font-weight: 600; font-size: 12px;")

        self.edit_ip = QLineEdit("192.168.31.173")
        self.edit_ip.setFixedWidth(125)
        self.edit_ip.setPlaceholderText("IP 地址")
        self.edit_ip.returnPressed.connect(self.toggle_connection)

        lbl_port = QLabel("端口:")
        lbl_port.setStyleSheet("font-size: 12px; color: #86868B;")

        # 扩宽输入框至 65px，彻底解决 025 裁切歧义
        self.edit_port = QLineEdit("5025")
        self.edit_port.setFixedWidth(65)
        self.edit_port.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.edit_port.returnPressed.connect(self.toggle_connection)

        self.btn_connect = QPushButton("⚡ 连接设备")
        self.btn_connect.setProperty("class", "ConnectBtn")
        self.btn_connect.clicked.connect(self.toggle_connection)

        self.lbl_status = QLabel("● 离线")
        self.lbl_status.setStyleSheet("color: #FF3B30; font-weight: bold; font-size: 11px;")

        self.lbl_fps = QLabel("FPS: 0")
        self.lbl_fps.setStyleSheet("color: #86868B; font-weight: bold; font-size: 11px;")

        self.btn_copy = QPushButton("📋 复制图片 (1:1)")
        self.btn_copy.setProperty("class", "PillCopy")
        self.btn_copy.clicked.connect(self.copy_image_to_clipboard)

        layout.addWidget(lbl_ip)
        layout.addWidget(self.edit_ip)
        layout.addWidget(lbl_port)
        layout.addWidget(self.edit_port)
        layout.addWidget(self.btn_connect)
        layout.addSpacing(6)
        layout.addWidget(self.lbl_status)
        layout.addStretch()
        layout.addWidget(self.lbl_fps)
        layout.addSpacing(4)
        layout.addWidget(self.btn_copy)

        return bar

    def bind_signals(self):
        self.engine.frame_decoded.connect(self._on_frame_decoded)
        self.engine.connection_changed.connect(self._on_connection_changed)

    def toggle_connection(self):
        if self.engine.is_connected:
            self.engine.disconnect()
        else:
            ip = self.edit_ip.text().strip()
            port = int(self.edit_port.text().strip() or 5025)
            self.engine.connect_device(ip, port)

    def _on_connection_changed(self, connected: bool, msg: str):
        if connected:
            self.lbl_status.setText("● 已连接")
            self.lbl_status.setStyleSheet("color: #34C759; font-weight: bold; font-size: 11px;")
            self.btn_connect.setText("✕ 断开")
            self.toast.show_msg(f"✓ {msg}")
        else:
            self.lbl_status.setText("● 离线")
            self.lbl_status.setStyleSheet("color: #FF3B30; font-weight: bold; font-size: 11px;")
            self.btn_connect.setText("⚡ 连接设备")
            self.lbl_fps.setText("FPS: 0")
            self.screen_view.render_standby_screen()

    def _on_frame_decoded(self, image: QImage):
        self.screen_view.update_image(image)
        self.fps_counter += 1
        now = time.time()
        if now - self.last_fps_time >= 1.0:
            channel_tag = "Web" if self.engine.use_web_fallback else "SCPI"
            self.lbl_fps.setText(f"FPS: {self.fps_counter} ({channel_tag})")
            self.fps_counter = 0
            self.last_fps_time = now

    def copy_image_to_clipboard(self):
        """1:1 原生无损图像复制到系统剪贴板"""
        clipboard = QApplication.clipboard()
        pix = self.screen_view.pixmap.copy()
        clipboard.setPixmap(pix)
        clipboard.setImage(pix.toImage())
        self.toast.show_msg("📋 已将 800×480 原生高清图复制到剪贴板！")

    def load_settings(self):
        saved_ip = self.settings.value("ip", "192.168.31.173", type=str)
        saved_port = self.settings.value("port", "5025", type=str)
        self.edit_ip.setText(saved_ip)
        self.edit_port.setText(saved_port)
        QTimer.singleShot(200, self.toggle_connection)

    def closeEvent(self, event):
        self.settings.setValue("ip", self.edit_ip.text().strip())
        self.settings.setValue("port", self.edit_port.text().strip())
        self.engine.disconnect()
        event.accept()


# ==============================================================================
# 6. 主程序入口
# ==============================================================================

def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(IOS26_LIQUID_WHITE_QSS)

    if hasattr(Qt.ApplicationAttribute, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

    win = ZDSStreamMainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
