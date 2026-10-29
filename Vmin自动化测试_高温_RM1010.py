import os
import sys
import time
import json
import threading
import queue
import subprocess
import re
import ctypes
from datetime import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, simpledialog
import serial
import serial.tools.list_ports
import openpyxl

# =========================================================================
# 0. 动态库加载与底层 API 绑定 (兼容 PyInstaller 打包与跨平台源码运行)
# =========================================================================
if getattr(sys, 'frozen', False):
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    meipass_dir = getattr(sys, '_MEIPASS', exe_dir)
    script_dir = exe_dir if os.path.exists(os.path.join(exe_dir, "librockmong.dll")) else meipass_dir
else:
    script_dir = os.path.dirname(os.path.abspath(__file__))

if sys.platform.startswith("win"):
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(script_dir)
            if getattr(sys, 'frozen', False):
                os.add_dll_directory(os.path.dirname(os.path.abspath(sys.executable)))
        except Exception:
            pass
    os.environ["PATH"] = script_dir + os.pathsep + os.environ.get("PATH", "")

try:
    libusb_name = "libusb-1.0.dll" if sys.platform.startswith("win") else "libusb-1.0.so"
    libusb_p = os.path.join(script_dir, libusb_name)
    if os.path.exists(libusb_p):
        ctypes.CDLL(libusb_p)
except Exception:
    pass

try:
    if sys.platform.startswith("win"):
        dll_path = os.path.join(script_dir, "librockmong.dll")
        lib = ctypes.CDLL(dll_path if os.path.exists(dll_path) else "librockmong.dll")
    elif sys.platform.startswith("linux"):
        dll_path = os.path.join(script_dir, "librockmong.so")
        lib = ctypes.CDLL(dll_path if os.path.exists(dll_path) else "librockmong.so")
    elif sys.platform.startswith("darwin"):
        dll_path = os.path.join(script_dir, "librockmong.dylib")
        lib = ctypes.CDLL(dll_path if os.path.exists(dll_path) else "librockmong.dylib")
    else:
        lib = ctypes.CDLL("librockmong")
except Exception:
    lib = None

# ==================== C 结构体定义 (严格 32 位 ABI 对齐) ====================
class ADC_Init_TxStruct_t(ctypes.Structure):  
    _fields_ = [("Channel", ctypes.c_ubyte), ("SampleRateHz", ctypes.c_uint32)]

class ADC_Init_RxStruct_t(ctypes.Structure):  
    _fields_ = [("Ret", ctypes.c_ubyte)]

class ADC_Read_TxStruct_t(ctypes.Structure):  
    _fields_ = [("Channel", ctypes.c_ubyte)]

class ADC_Read_RxStruct_t(ctypes.Structure):  
    _fields_ = [("Ret", ctypes.c_ubyte), ("Value", ctypes.c_uint16)]

if lib is not None:
    lib.UsbDevice_Scan.argtypes = [ctypes.POINTER(ctypes.c_int)]
    lib.UsbDevice_Scan.restype = ctypes.c_int

    lib.IO_InitPin.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.IO_InitPin.restype = ctypes.c_int

    lib.IO_WritePin.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.IO_WritePin.restype = ctypes.c_int

    lib.ADC_InitMulti.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ADC_Init_TxStruct_t),
        ctypes.POINTER(ADC_Init_RxStruct_t),
        ctypes.c_int
    ]
    lib.ADC_InitMulti.restype = ctypes.c_int

    lib.ADC_ReadMulti.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ADC_Read_TxStruct_t),
        ctypes.POINTER(ADC_Read_RxStruct_t),
        ctypes.c_int
    ]
    lib.ADC_ReadMulti.restype = ctypes.c_int


# =========================================================================
# 1. 算法引擎：Welford 在线增量统计 + 台式万用表级自适应信号调理引擎
# =========================================================================
def format_duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h > 0: return f"{h}小时{m}分{sec}秒"
    if m > 0: return f"{m}分{sec}秒"
    return f"{sec}秒"


class HighPrecisionStats:
    """Welford 在线增量统计引擎 (O(1) 复杂度，无数值溢出)"""
    __slots__ = ('count', 'min_val', 'max_val', 'mean', 'M2')

    def __init__(self):
        self.reset()

    def update(self, x: float):
        self.count += 1
        if x < self.min_val: self.min_val = x
        if x > self.max_val: self.max_val = x
        delta = x - self.mean
        self.mean += delta / self.count
        self.M2 += delta * (x - self.mean)

    def reset(self):
        self.count = 0
        self.min_val = float('inf')
        self.max_val = float('-inf')
        self.mean = 0.0
        self.M2 = 0.0


class PrecisionDMMIntegrator:
    """
    Keysight / Fluke 台式数字万用表架构信号调理引擎：
    - L0: 物理原始采样数据保真捕获 (Raw Code / Raw Voltage 完全保留)
    - L1: 200ms (10 NPLC @ 50Hz) 稳健抗尖峰截尾积分 (Alpha-trimmed Integration)
    - L2: 参考通道慢速退耦观测器 (Decoupled Reference Observer，阻断 VREF 高频噪声串扰)
    - L3: 自适应摆率感知双模态滤波 (Adaptive Slew-Rate Auto-Filter，稳态高平滑，阶跃闪电跟进)
    """
    __slots__ = ('integration_sec', 'window_start_t', 'buf_t', 'buf_p',
                 'ref_filtered', 'ref_initialized',
                 'last_voltage', 'last_raw_voltage',
                 'last_mean_t', 'last_mean_p')

    def __init__(self, integration_sec: float = 0.200):
        self.integration_sec = integration_sec
        self.window_start_t = None
        self.buf_t = []
        self.buf_p = []
        self.ref_filtered = 2877.7
        self.ref_initialized = False
        self.last_voltage = None
        self.last_raw_voltage = None
        self.last_mean_t = 0.0
        self.last_mean_p = 2877.7

    def reset(self):
        self.window_start_t = None
        self.buf_t.clear()
        self.buf_p.clear()
        self.ref_initialized = False
        self.last_voltage = None
        self.last_raw_voltage = None
        self.last_mean_t = 0.0
        self.last_mean_p = 2877.7

    def push(self, raw_t: int, raw_p: int, t_now: float, vref_physical: float) -> bool:
        if self.window_start_t is None:
            self.window_start_t = t_now

        self.buf_t.append(raw_t)
        self.buf_p.append(raw_p)

        elapsed = t_now - self.window_start_t
        n = len(self.buf_t)
        if elapsed >= self.integration_sec and n >= 4:
            if n >= 6:
                st = sorted(self.buf_t)
                sp = sorted(self.buf_p)
                k = max(1, n // 8)
                mean_t = sum(st[k:-k]) / (n - 2 * k)
                mean_p = sum(sp[k:-k]) / (n - 2 * k)
            else:
                mean_t = sum(self.buf_t) / n
                mean_p = sum(self.buf_p) / n

            if not self.ref_initialized:
                self.ref_filtered = mean_p
                self.ref_initialized = True
            else:
                self.ref_filtered += 0.06 * (mean_p - self.ref_filtered)

            ref_denom = self.ref_filtered if self.ref_filtered > 100.0 else 2877.7
            if mean_p > 100.0:
                raw_v = vref_physical * (mean_t / ref_denom)
            else:
                raw_v = (mean_t / 4095.0) * 4.6857

            if self.last_voltage is None:
                smooth_v = raw_v
            else:
                delta = abs(raw_v - self.last_voltage)
                if delta > 0.0040:
                    smooth_v = self.last_voltage * 0.15 + raw_v * 0.85
                elif delta > 0.0018:
                    smooth_v = self.last_voltage * 0.55 + raw_v * 0.45
                else:
                    smooth_v = self.last_voltage * 0.88 + raw_v * 0.12

            self.last_raw_voltage = raw_v
            self.last_voltage = smooth_v
            self.last_mean_t = mean_t
            self.last_mean_p = mean_p

            self.buf_t.clear()
            self.buf_p.clear()
            self.window_start_t = t_now
            return True
        return False


# =========================================================================
# 2. 硬件驱动层 (RM1010 GPIO+ADC 高精电压表 + 风枪控制器)
# =========================================================================
class DeviceState:
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"


class RM1010Driver:
    CONFIG_FILE = "rm1010_sn_profiles.json"
    MEASURE_CH_IDX = 0  # P8 -> CH0
    REF_CH_IDX = 7      # P15 -> CH7

    __slots__ = ('device_sn', 'connected', 'state', 'lock', 'log', 'last_error',
                 'dll_error', '_initialized_pins', '_initialized_adc',
                 'vref_physical', 'sn_profiles', 'integrator', 'stats',
                 'voltage_val', 'voltage_raw', 'raw_adc_p8', 'raw_adc_p15',
                 'mean_raw_p8', 'mean_raw_p15', 'sample_seq',
                 'tx_init', 'rx_init', 'tx_read', 'rx_read', 'running',
                 'adc_fail_count', 'is_reconnecting', '_adc_thread')

    def __init__(self, log_func=None):
        self.device_sn = None
        self.connected = False
        self.state = DeviceState.DISCONNECTED
        self.lock = threading.RLock()
        self.log = log_func or print
        self.last_error = ""
        self.dll_error = "" if lib is not None else "librockmong.dll 加载失败"
        self._initialized_pins = False
        self._initialized_adc = False

        self.sn_profiles = self.load_sn_profiles()
        self.vref_physical = 3.3000
        self.integrator = PrecisionDMMIntegrator(integration_sec=0.200)
        self.stats = HighPrecisionStats()

        self.voltage_val = None
        self.voltage_raw = None
        self.raw_adc_p8 = 0
        self.raw_adc_p15 = 0
        self.mean_raw_p8 = 0.0
        self.mean_raw_p15 = 0.0
        self.sample_seq = 0
        self.adc_fail_count = 0
        self.is_reconnecting = False

        self.tx_init = (ADC_Init_TxStruct_t * 8)()
        self.rx_init = (ADC_Init_RxStruct_t * 8)()
        for i in range(8):
            self.tx_init[i].Channel = i
            self.tx_init[i].SampleRateHz = 0

        self.tx_read = (ADC_Read_TxStruct_t * 8)()
        self.rx_read = (ADC_Read_RxStruct_t * 8)()
        for i in range(8):
            self.tx_read[i].Channel = i

        self.running = True
        self._adc_thread = threading.Thread(target=self._adc_worker_loop, name="RM1010_ADC_Worker", daemon=True)
        self._adc_thread.start()

    def load_sn_profiles(self) -> dict:
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_sn_profile(self, sn: int, vref_val: float):
        self.sn_profiles[str(sn)] = {"vref_physical": vref_val}
        try:
            with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.sn_profiles, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def update_vref_setting(self, new_vref: float):
        with self.lock:
            self.vref_physical = new_vref
            if self.device_sn:
                self.save_sn_profile(self.device_sn, new_vref)
            self.stats.reset()
            self.integrator.reset()

    def scan_and_sync(self) -> tuple[bool, bool]:
        if lib is None:
            old_conn = self.connected
            self.connected = False
            self.state = DeviceState.ERROR
            self.last_error = self.dll_error or "DLL未加载"
            return False, (self.connected != old_conn)

        with self.lock:
            old_conn = self.connected
            try:
                serials = (ctypes.c_int * 16)()
                count = lib.UsbDevice_Scan(serials)

                if count <= 0:
                    self.connected = False
                    self.state = DeviceState.DISCONNECTED
                    self.device_sn = None
                    self._initialized_pins = False
                    self._initialized_adc = False
                    self.is_reconnecting = True
                    self.last_error = "未检测到 RM1010 硬件 (USB已拔出或未连接)"
                    return False, (self.connected != old_conn)

                current_sn = int(serials[0])
                if not self.connected or self.device_sn != current_sn or not self._initialized_pins or not self._initialized_adc:
                    self.state = DeviceState.RECONNECTING if self.device_sn else DeviceState.CONNECTING
                    self.device_sn = current_sn

                    for pin in range(16):
                        lib.IO_InitPin(self.device_sn, pin, 1, 0)
                        lib.IO_WritePin(self.device_sn, pin, 1)
                    self._initialized_pins = True

                    ret_adc = lib.ADC_InitMulti(self.device_sn, self.tx_init, self.rx_init, 8)
                    if ret_adc >= 0:
                        self._initialized_adc = True

                    sn_key = str(current_sn)
                    if sn_key in self.sn_profiles:
                        self.vref_physical = float(self.sn_profiles[sn_key].get("vref_physical", 3.3000))
                    else:
                        self.vref_physical = 3.3000
                        self.save_sn_profile(current_sn, 3.3000)

                    self.stats.reset()
                    self.integrator.reset()
                    self.connected = True
                    self.is_reconnecting = False
                    self.state = DeviceState.CONNECTED
                    self.last_error = ""
                    return True, True

                return True, False

            except Exception as e:
                self.connected = False
                self.state = DeviceState.ERROR
                self.device_sn = None
                self._initialized_pins = False
                self._initialized_adc = False
                self.last_error = str(e)
                return False, (self.connected != old_conn)

    def _adc_worker_loop(self):
        while self.running:
            try:
                if not self.connected or not self._initialized_adc or self.device_sn is None or lib is None:
                    time.sleep(0.05)
                    continue

                with self.lock:
                    sn = self.device_sn
                    vref = self.vref_physical

                ret = lib.ADC_ReadMulti(sn, self.tx_read, self.rx_read, 8)
                now_t = time.perf_counter()

                if ret < 0 or self.rx_read[self.MEASURE_CH_IDX].Ret != 0 or self.rx_read[self.REF_CH_IDX].Ret != 0:
                    self.adc_fail_count += 1
                    if self.adc_fail_count >= 5:
                        with self.lock:
                            self.connected = False
                            self.is_reconnecting = True
                            self.voltage_val = None
                        time.sleep(0.1)
                    else:
                        time.sleep(0.02)
                    continue
                else:
                    self.adc_fail_count = 0

                raw_p8 = self.rx_read[self.MEASURE_CH_IDX].Value & 0x0FFF
                raw_p15 = self.rx_read[self.REF_CH_IDX].Value & 0x0FFF

                epoch_ready = self.integrator.push(raw_p8, raw_p15, now_t, vref)
                if epoch_ready:
                    clean_v = self.integrator.last_voltage
                    raw_v = self.integrator.last_raw_voltage
                    clean_p8 = self.integrator.last_mean_t
                    clean_p15 = self.integrator.last_mean_p

                    with self.lock:
                        self.voltage_val = clean_v
                        self.voltage_raw = raw_v
                        self.raw_adc_p8 = raw_p8
                        self.raw_adc_p15 = raw_p15
                        self.mean_raw_p8 = clean_p8
                        self.mean_raw_p15 = clean_p15
                        self.sample_seq += 1
                        self.stats.update(clean_v)

                time.sleep(0.012)
            except Exception:
                time.sleep(0.05)

    def clear_stats(self):
        with self.lock:
            self.stats.reset()
            self.integrator.reset()
            self.voltage_val = None
            self.voltage_raw = None
            self.raw_adc_p8 = 0
            self.raw_adc_p15 = 0
            self.mean_raw_p8 = 0.0
            self.mean_raw_p15 = 0.0
            self.sample_seq = 0

    def send_pulse(self, pin_idx: int) -> bool:
        """非阻塞式硬件脉冲发生器：睡眠期主动让渡互斥锁，彻底杜绝 ADC 采集卡顿"""
        with self.lock:
            if not self.connected:
                conn, _ = self.scan_and_sync()
                if not conn: return False
            if self.device_sn is None or lib is None:
                return False
            sn = self.device_sn
            try:
                lib.IO_WritePin(sn, pin_idx, 0)
            except Exception as e:
                self.connected = False
                self.state = DeviceState.DISCONNECTED
                self._initialized_pins = False
                self.last_error = f"脉冲发送失败: {e}"
                return False

        time.sleep(0.080)

        with self.lock:
            if not self.connected or self.device_sn != sn or lib is None:
                return False
            try:
                lib.IO_WritePin(sn, pin_idx, 1)
                return True
            except Exception as e:
                self.connected = False
                self.state = DeviceState.DISCONNECTED
                self._initialized_pins = False
                self.last_error = f"脉冲复位失败: {e}"
                return False

    def temp_up(self, count: int = 1) -> bool:
        for _ in range(count):
            if not self.send_pulse(0): return False
            if count > 1: time.sleep(0.06)
        return True

    def temp_down(self, count: int = 1) -> bool:
        for _ in range(count):
            if not self.send_pulse(1): return False
            if count > 1: time.sleep(0.06)
        return True

    def temp_down_fast(self, count: int = 35) -> bool:
        if not self.connected:
            with self.lock:
                conn, _ = self.scan_and_sync()
            if not conn: return False
        try:
            for _ in range(count):
                if not self.running or not self.connected:
                    break
                with self.lock:
                    if not self.connected or self.device_sn is None or lib is None:
                        return False
                    lib.IO_WritePin(self.device_sn, 1, 0)
                time.sleep(0.060)
                with self.lock:
                    if not self.connected or self.device_sn is None or lib is None:
                        return False
                    lib.IO_WritePin(self.device_sn, 1, 1)
                time.sleep(0.060)
            return True
        except Exception as e:
            with self.lock:
                self.connected = False
                self.last_error = f"快速降温失败: {e}"
            return False

    def close(self):
        self.running = False


class HeatGunDriver:
    __slots__ = ("ser", "port", "baudrate", "is_on", "lock", "log", "off_pulse_sec")

    def __init__(self, log_func=None):
        self.ser = None
        self.port = ""
        self.baudrate = 9600
        self.is_on = False
        self.lock = threading.RLock()
        self.log = log_func or print
        self.off_pulse_sec = 0.8

    @staticmethod
    def _opened(ser) -> bool:
        return bool(ser and getattr(ser, "is_open", False))

    def open(self, port: str, baudrate: int = 9600) -> bool:
        with self.lock:
            self.close()
            self.port, self.baudrate = port, baudrate
            try:
                self.ser = serial.Serial(
                    port=port, baudrate=baudrate,
                    timeout=0.2, rtscts=False, dsrdtr=False
                )
                self._apply_level()
                return True
            except Exception as e:
                self.ser = None
                self.log(f"⚠️ [风枪串口打开失败] {port}: {e}")
                return False

    def _apply_level(self) -> bool:
        if not self._opened(self.ser):
            return False
        try:
            self.ser.setRTS(not self.is_on)
            return True
        except Exception as e:
            self.log(f"⚠️ [风枪 RTS 置位失败]: {e}")
            return False

    def power_on(self, source: str = "") -> bool:
        with self.lock:
            self.is_on = True
            ok = self._apply_level()
            if source:
                self.log(f"[风枪12V] ON <- {source}")
            return ok

    def power_off(self, source: str = "", duration: float = None) -> bool:
        with self.lock:
            self.is_on = False
            if not self._opened(self.ser):
                return False

            try:
                duration = self.off_pulse_sec if duration is None else max(0.0, float(duration))
                if duration <= 0:
                    ok = self.ser.setRTS(True) is None
                else:
                    deadline = time.perf_counter() + duration
                    while time.perf_counter() < deadline:
                        self.ser.setRTS(True)
                        time.sleep(0.01)
                        self.ser.setRTS(False)
                        time.sleep(0.01)
                    ok = True
            except Exception as e:
                self.log(f"⚠️ [风枪关断失败]: {e}")
                ok = False

            if source:
                self.log(f"[风枪12V] OFF <- {source} ({duration:.1f}s 翻转)")
            return ok

    def toggle(self) -> bool:
        return self.power_off(source="MANUAL") if self.is_on else self.power_on(source="MANUAL")

    def close(self):
        with self.lock:
            if self._opened(self.ser):
                try:
                    self.ser.setRTS(True)
                except Exception:
                    pass
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None
            self.is_on = False


# =========================================================================
# 3. 全局通用异常防御库、测试看门狗、Tj 流式解析与自适应热管理模型
# =========================================================================
UNIVERSAL_FATAL_PATTERNS = (
    r";ID:",                # SoC 硬件异常掉电/重启 BootROM 特征
    r"Segmentation fault",  # 核心转储段错误
)

class CaseRule:
    __slots__ = ('key', 'match_pattern', 'fail_patterns', 'heartbeat_pattern')

    def __init__(self, key: str, match_pattern: str, fail_patterns: list = None, heartbeat_pattern: str = None):
        self.key = key.lower()
        self.match_pattern = match_pattern
        self.fail_patterns = fail_patterns or []
        self.heartbeat_pattern = heartbeat_pattern


GLOBAL_RULE_REGISTRY = [
    CaseRule("gpuburn", match_pattern=r"gpu\.burn|gpuburn", fail_patterns=[r"Test Failed!"], heartbeat_pattern=r"\(gpu\.burn\)\s*Successfully"),
    CaseRule("stressapptest", match_pattern=r"stressapptest", fail_patterns=[r"Status:\s*FAIL", r"Report Error:\s*miscompare\s*:"], heartbeat_pattern=r"seconds\s+remaining"),
    CaseRule("run.sh", match_pattern=r"run\.sh", fail_patterns=[r"memory\s+compare\s+fail"], heartbeat_pattern=r"memory\s+compare\s+pass"),
    CaseRule("cpuburn", match_pattern=r"cpuburn|cpu\.burn"),
    CaseRule("dhrystone", match_pattern=r"dhrystone"),
]

REBOOT_REGEX = re.compile(r";ID:", re.IGNORECASE)
CHIP_ID_REGEX = re.compile(r";ID:([A-Za-z0-9_-]+);")


class TjStreamParser:
    """结温流式解析引擎：全温区 -100.0℃ ~ 200.0℃ 高精捕获"""
    __slots__ = ('line_buffer', 'last_tj_celsius', 'last_update_time', 'new_sample_event', 'lock')
    MIN_TJ_MC = -100000   # -100℃ (毫摄氏度)
    MAX_TJ_MC = 200000    # +200℃ (毫摄氏度)

    _ANSI_RX = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    _TS_RX = re.compile(r"^\d{4}/\d{2}/\d{2}[_\s]\d{2}:\d{2}:\d{2}\s*")
    _JOB_PID_RX = re.compile(r"^\[\d+\]\s+[-+]?\d+\s*$")
    _TEMP_RX = re.compile(r"^(?:.*[#$>])?\s*(-?\d{1,6})$")

    def __init__(self):
        self.line_buffer = ""
        self.last_tj_celsius = None
        self.last_update_time = 0.0
        self.new_sample_event = threading.Event()
        self.lock = threading.RLock()

    def reset(self):
        with self.lock:
            self.line_buffer = ""
            self.last_tj_celsius = None
            self.last_update_time = 0.0
            self.new_sample_event.clear()

    def feed_raw_chunk(self, chunk: str):
        if not chunk: return
        with self.lock:
            self.line_buffer += chunk
            nl_pos = max(self.line_buffer.rfind("\n"), self.line_buffer.rfind("\r"))
            if nl_pos != -1:
                ready_text = self.line_buffer[:nl_pos]
                self.line_buffer = self.line_buffer[nl_pos + 1:]
                lines = [p.strip() for p in ready_text.splitlines() if p.strip()]
            else:
                lines = []
            if len(self.line_buffer) > 2048:
                self.line_buffer = self.line_buffer[-256:]

        for line in lines:
            self._process_single_line(line)

    def _process_single_line(self, line: str):
        c = self._ANSI_RX.sub("", line.replace(chr(0), "")).strip() if ("\x1b" in line or "\x00" in line) else line.strip()
        c = self._TS_RX.sub("", c).strip()
        if not c or not ('0' <= c[-1] <= '9') or self._JOB_PID_RX.match(c): return

        m = self._TEMP_RX.match(c)
        if m:
            raw_val = int(m.group(1))
            celsius = None
            if raw_val == 0:
                celsius = 0.0
            elif abs(raw_val) > 200 and self.MIN_TJ_MC <= raw_val <= self.MAX_TJ_MC:
                celsius = round(raw_val / 1000.0, 1)
            elif -100 <= raw_val <= 200:
                celsius = float(raw_val)

            if celsius is not None:
                now = time.time()
                with self.lock:
                    self.last_tj_celsius = celsius
                    self.last_update_time = now
                    self.new_sample_event.set()

    def get_latest_tj(self, max_age_sec: float = None) -> tuple[float | None, float]:
        with self.lock:
            if max_age_sec is not None and self.last_update_time > 0:
                if (time.time() - self.last_update_time) > max_age_sec:
                    return None, self.last_update_time
            return self.last_tj_celsius, self.last_update_time


class AdaptiveBootManager:
    __slots__ = ("configured_delay", "learned_total_delay", "is_learned", "lock")

    def __init__(self, default_delay: float = 18.0):
        self.configured_delay = max(6.0, float(default_delay))
        self.learned_total_delay = self.configured_delay
        self.is_learned = False
        self.lock = threading.RLock()

    def reset_session(self, configured_delay: float):
        with self.lock:
            self.configured_delay = max(6.0, float(configured_delay))

    def get_current_boot_delay(self) -> float:
        with self.lock:
            return self.learned_total_delay if self.is_learned else self.configured_delay

    def record_pre_scan_boot(self, reboot_mono: float, second_fixed_cmd_send_mono: float, log_func=None):
        if reboot_mono <= 0 or second_fixed_cmd_send_mono <= reboot_mono:
            return
        measured = second_fixed_cmd_send_mono - reboot_mono
        learned = max(6.0, round(measured + 2.0, 1))
        with self.lock:
            self.learned_total_delay = learned
            self.is_learned = True
        if log_func:
            log_func(
                f"⚡ [自适应开机时间] 预扫描实测：板卡重启 → 第二条固定命令发送 = "
                f"{measured:.1f}s；+2.0s 安全余量 → Case1 开机等待 = {learned:.1f}s"
            )


class WatchdogState:
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    FAILED = "FAILED"


class RealtimeStreamWatchdog:
    HEARTBEAT_TIMEOUT_SEC = 10.0
    MONITOR_INTERVAL_SEC = 0.10
    __slots__ = ('fail_rx', 'hb_rx', 'state', 'case_start_time', 'last_heartbeat_time', 'last_reboot_time',
                 'first_hb_received', 'fail_detected', 'is_reboot_fail',
                 'fail_reason', 'interrupt_event', 'lock', 'has_hb_rule',
                 'monitor_thread', 'monitor_stop_event', '_tail_buffer')

    _ANSI_RX = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

    def __init__(self):
        self.fail_rx = None
        self.hb_rx = None
        self.state = WatchdogState.IDLE
        self.case_start_time = 0.0
        self.last_heartbeat_time = 0.0
        self.last_reboot_time = 0.0
        self.first_hb_received = False
        self.fail_detected = False
        self.is_reboot_fail = False
        self.fail_reason = ""
        self._tail_buffer = ""
        self.interrupt_event = threading.Event()
        self.lock = threading.RLock()
        self.has_hb_rule = False
        self.monitor_thread = None
        self.monitor_stop_event = threading.Event()

    def _stop_monitor(self):
        self.monitor_stop_event.set()
        th = self.monitor_thread
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=0.2)
        self.monitor_thread = None

    def reset_and_configure(self, case_info: str):
        self._stop_monitor()
        fail_pats = list(UNIVERSAL_FATAL_PATTERNS)
        hb_pats = []

        for rule in GLOBAL_RULE_REGISTRY:
            if re.search(rule.match_pattern, case_info, re.IGNORECASE):
                fail_pats.extend(rule.fail_patterns)
                if rule.heartbeat_pattern:
                    hb_pats.append(rule.heartbeat_pattern)

        fail_pats_sorted = sorted(dict.fromkeys(fail_pats), key=lambda x: len(x), reverse=True)
        self.fail_rx = re.compile("|".join(f"(?:{p})" for p in fail_pats_sorted), re.IGNORECASE) if fail_pats else None
        self.hb_rx = re.compile("|".join(f"(?:{p})" for p in dict.fromkeys(hb_pats)), re.IGNORECASE) if hb_pats else None
        self.has_hb_rule = bool(self.hb_rx)

        with self.lock:
            self.state = WatchdogState.IDLE
            self.case_start_time = self.last_heartbeat_time = 0.0
            self.last_reboot_time = 0.0
            self.first_hb_received = self.fail_detected = self.is_reboot_fail = False
            self.fail_reason = ""
            self._tail_buffer = ""
            self.interrupt_event.clear()

    def start_monitoring(self):
        self._stop_monitor()
        with self.lock:
            now = time.monotonic()
            self.case_start_time = now
            self.last_heartbeat_time = now
            self.first_hb_received = False
            self.state = WatchdogState.RUNNING
            self.interrupt_event.clear()
            self.monitor_stop_event.clear()

        if self.has_hb_rule:
            self.monitor_thread = threading.Thread(
                target=self._heartbeat_monitor_loop,
                name="Case_Heartbeat_Watchdog",
                daemon=True,
            )
            self.monitor_thread.start()

    def mark_case_starting(self):
        self.start_monitoring()

    def force_check(self) -> tuple[bool, str]:
        return self.check_heartbeat_timeout()

    def mark_running(self):
        with self.lock:
            self.state = WatchdogState.RUNNING

    def stop_monitor(self):
        self._stop_monitor()

    def _heartbeat_monitor_loop(self):
        while not self.monitor_stop_event.wait(self.MONITOR_INTERVAL_SEC):
            try:
                with self.lock:
                    if self.fail_detected or self.state not in (WatchdogState.STARTING, WatchdogState.RUNNING):
                        return
                self.check_heartbeat_timeout()
            except Exception:
                pass

    def mark_unexpected_reboot(self, chip_id: str = ""):
        with self.lock:
            if self.state not in (WatchdogState.STARTING, WatchdogState.RUNNING) or self.fail_detected:
                return
            self.fail_detected = True
            self.state = WatchdogState.FAILED
            self.is_reboot_fail = True
            self.last_reboot_time = time.monotonic()
            self.fail_reason = "匹配到异常特征: ';ID:' (硬件重启)"
            self.interrupt_event.set()
        self.monitor_stop_event.set()

    def feed_text(self, text: str):
        if not text or self.fail_detected:
            return

        clean_text = self._ANSI_RX.sub("", text.replace("\x00", "")) if ("\x1b" in text or "\x00" in text) else text
        now = time.monotonic()
        with self.lock:
            combined = self._tail_buffer + clean_text
            self._tail_buffer = combined[-256:] if len(combined) > 256 else combined

            if self.fail_detected:
                return

            fail_m = self.fail_rx.search(combined) if self.fail_rx else None
            hb_m = self.hb_rx.search(combined) if self.hb_rx else None

            if not (fail_m or hb_m):
                return

            if fail_m:
                self.fail_detected = True
                self.state = WatchdogState.FAILED
                matched_str = fail_m.group(0)
                self.is_reboot_fail = bool(REBOOT_REGEX.search(matched_str))
                if self.is_reboot_fail:
                    self.last_reboot_time = now
                    self.fail_reason = "匹配到异常特征: ';ID:' (硬件重启)"
                else:
                    self.fail_reason = f"匹配到异常特征: '{matched_str}'"
                self.interrupt_event.set()
                self.monitor_stop_event.set()
                return

            if hb_m:
                self.last_heartbeat_time = now
                self.first_hb_received = True
                if self.state == WatchdogState.STARTING:
                    self.state = WatchdogState.RUNNING

    def check_heartbeat_timeout(self, timeout_sec: float = None) -> tuple[bool, str]:
        timeout = self.HEARTBEAT_TIMEOUT_SEC if timeout_sec is None else max(0.1, float(timeout_sec))
        with self.lock:
            if self.fail_detected:
                return True, self.fail_reason
            if self.has_hb_rule and self.state in (WatchdogState.STARTING, WatchdogState.RUNNING):
                silent_sec = time.monotonic() - self.last_heartbeat_time
                if silent_sec >= timeout:
                    self.is_reboot_fail = False
                    self.fail_detected = True
                    self.state = WatchdogState.FAILED
                    self.fail_reason = (
                        f"心跳卡死超时: 连续 {silent_sec:.1f} 秒无心跳 "
                        f"(阈值 {timeout:.1f}s)"
                    )
                    self.interrupt_event.set()
                    self.monitor_stop_event.set()
                    return True, self.fail_reason
            return False, ""


class ThermalActionRecord:
    __slots__ = ('action', 'pulses', 'tj_start', 'timestamp', 'eval_time', 'evaluated')

    def __init__(self, action: str, pulses: int, tj_start: float, timestamp: float, eval_delay: float = 3.8):
        self.action = action
        self.pulses = pulses
        self.tj_start = tj_start
        self.timestamp = timestamp
        self.eval_time = timestamp + eval_delay
        self.evaluated = False


class OnlineThermalIdentifier:
    __slots__ = ('heat_gain', 'cool_gain', 'action_history', 'lock')

    def __init__(self, initial_heat_gain: float = 0.25, initial_cool_gain: float = 0.25):
        self.heat_gain = initial_heat_gain
        self.cool_gain = initial_cool_gain
        self.action_history = []
        self.lock = threading.RLock()

    def reset(self):
        with self.lock:
            self.action_history.clear()
            self.heat_gain = 0.25
            self.cool_gain = 0.25

    def log_action(self, action: str, pulses: int, tj_start: float, timestamp: float):
        with self.lock:
            if len(self.action_history) > 10:
                self.action_history = self.action_history[-6:]
            self.action_history.append(ThermalActionRecord(action, pulses, tj_start, timestamp, eval_delay=3.8))

    def update_with_sample(self, tj_curr: float, now: float):
        with self.lock:
            for rec in self.action_history:
                if not rec.evaluated and now >= rec.eval_time:
                    rec.evaluated = True
                    delta = tj_curr - rec.tj_start
                    pulses = max(1, rec.pulses)
                    if rec.action == "UP" and delta > 0.05:
                        eff_g = delta / pulses
                        if 0.06 <= eff_g <= 0.85:
                            self.heat_gain = 0.80 * self.heat_gain + 0.20 * eff_g
                    elif rec.action == "DOWN" and delta < -0.05:
                        eff_g = (-delta) / pulses
                        if 0.06 <= eff_g <= 0.85:
                            self.cool_gain = 0.80 * self.cool_gain + 0.20 * eff_g


class SmartThermalManager:
    __slots__ = (
        "rm1010", "heatgun", "tj_parser", "send_board_cmd", "log",
        "probe_running", "control_running", "stop_probe_event", "stop_control_event",
        "probe_thread", "control_thread", "target_temp", "control_start_time",
        "emergency_shutdown", "emergency_reason",
        "last_sample_tj", "last_sample_time", "filtered_rate", "filtered_accel",
        "last_adjust_time", "last_no_sample_log_time", "overheat_count",
        "initial_boost_done", "identifier"
    )
    TJ_POLL_INTERVAL_SEC = 2.0
    TARGET_BAND = 1.0
    STABLE_RATE = 0.10
    SAFETY_MARGIN = 8.5
    MAX_SAFE_TJ = 125.0

    def __init__(self, rm1010, heatgun, tj_parser, send_cmd_func, log_func):
        self.rm1010 = rm1010
        self.heatgun = heatgun
        self.tj_parser = tj_parser
        self.send_board_cmd = send_cmd_func
        self.log = log_func

        self.probe_running = False
        self.control_running = False
        self.stop_probe_event = threading.Event()
        self.stop_control_event = threading.Event()
        self.probe_thread = None
        self.control_thread = None
        self.target_temp = None
        self.control_start_time = 0.0

        self.emergency_shutdown = threading.Event()
        self.emergency_reason = ""
        self.last_sample_tj = None
        self.last_sample_time = 0.0
        self.filtered_rate = 0.0
        self.filtered_accel = 0.0
        self.last_adjust_time = 0.0
        self.last_no_sample_log_time = 0.0
        self.overheat_count = 0
        self.initial_boost_done = False
        self.identifier = OnlineThermalIdentifier()

    def start_probe_only(self):
        if self.probe_running:
            return
        self.stop_probe_event.clear()
        self.probe_running = True
        self.probe_thread = threading.Thread(
            target=self._probe_loop, name="Tj_Probe_Loop", daemon=True
        )
        self.probe_thread.start()

    def start_thermal_control(self, target_temp: float):
        self.stop_thermal_control()
        self.target_temp = float(target_temp)
        self.control_start_time = time.time()
        self.stop_control_event.clear()
        self.emergency_shutdown.clear()
        self.emergency_reason = ""
        self.last_sample_tj = None
        self.last_sample_time = 0.0
        self.filtered_rate = 0.0
        self.filtered_accel = 0.0
        self.last_adjust_time = 0.0
        self.last_no_sample_log_time = 0.0
        self.overheat_count = 0
        self.initial_boost_done = False
        self.identifier.reset()
        self.control_running = True

        self.heatgun.power_on(source="THERMAL_START")
        rm_status = (
            f"RM1010已就绪(SN:{self.rm1010.device_sn})"
            if self.rm1010.connected else "RM1010未连接"
        )
        self.log(
            f">>> 启动自适应闭环温控 (目标 [{self.target_temp:.0f}℃~"
            f"{self.target_temp + self.TARGET_BAND:.0f}℃] | {rm_status})"
        )
        self.control_thread = threading.Thread(
            target=self._control_loop, name="Thermal_Control_Loop", daemon=True
        )
        self.control_thread.start()

    def stop_thermal_control(self):
        self.control_running = False
        self.stop_control_event.set()
        self.target_temp = None
        th = self.control_thread
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=0.2)
        self.control_thread = None

    def stop_all(self):
        self.stop_thermal_control()
        self.probe_running = False
        self.stop_probe_event.set()
        th = self.probe_thread
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=0.2)
        self.probe_thread = None

    def is_temp_in_target_window(self) -> bool:
        if self.target_temp is None:
            return True
        tj, _ = self.tj_parser.get_latest_tj(max_age_sec=5.0)
        return tj is not None and self.target_temp <= tj <= self.target_temp + self.TARGET_BAND

    def wait_for_thermal_convergence(self, target_temp, check_stop_func=None, timeout_sec=300.0):
        start = time.time()
        stable = 0
        last_log = 0.0
        high = target_temp + self.TARGET_BAND

        while True:
            if check_stop_func and check_stop_func():
                return False
            if self.emergency_shutdown.is_set():
                self.log(f"🚨 [温度安全熔断] {self.emergency_reason}")
                return False
            now = time.time()
            if now - start > timeout_sec:
                self.log(
                    f"⚠️ [温控超时保底] {timeout_sec:.0f}s 内未稳定在 "
                    f"[{target_temp:.0f}℃~{high:.0f}℃]"
                )
                return False

            tj, sample_time = self.tj_parser.get_latest_tj(max_age_sec=5.0)
            if tj is not None and sample_time >= self.control_start_time - 0.5:
                rate = self.filtered_rate
                if target_temp <= tj <= high and abs(rate) <= 0.15:
                    stable += 1
                    if stable >= 3:
                        self.log(
                            f"    ⚡ [温度锁定] Tj={tj:.1f}℃ | 速率={rate:+.2f}℃/s | "
                            f"热增益=+{self.identifier.heat_gain:.2f}/"
                            f"-{self.identifier.cool_gain:.2f}℃/p"
                        )
                        return True
                else:
                    stable = 0

                if now - last_log >= 3.5:
                    last_log = now
                    hint = (
                        "升温" if tj < target_temp
                        else "降温" if tj > high
                        else "收敛"
                    )
                    self.log(
                        f"    [高温温控] Tj={tj:.1f}℃ | 速率={rate:+.2f}℃/s | "
                        f"{hint} -> [{target_temp:.0f}~{high:.0f}]℃"
                    )
            elif now - last_log >= 4.0:
                last_log = now
                self.log("    [高温温控] 等待最新 Tj 读数...")

            time.sleep(0.4)

    def _probe_loop(self):
        while self.probe_running:
            try:
                self.send_board_cmd(r"cat /sys/class/thermal/thermal_zone0/temp &")
            except Exception:
                pass
            if self.stop_probe_event.wait(self.TJ_POLL_INTERVAL_SEC):
                break

    def _control_loop(self):
        while self.control_running:
            try:
                self.tj_parser.new_sample_event.wait(timeout=0.8)
                if self.stop_control_event.is_set():
                    break
                self.tj_parser.new_sample_event.clear()

                tj, _ = self.tj_parser.get_latest_tj(max_age_sec=6.0)
                if tj is None:
                    self._log_no_sample_once()
                    continue

                now = time.time()
                if self.target_temp is None:
                    continue

                safe_limit = min(
                    self.target_temp + self.SAFETY_MARGIN, self.MAX_SAFE_TJ
                )
                if tj >= safe_limit:
                    self.overheat_count += 1
                    if self.overheat_count >= 2:
                        self.emergency_reason = (
                            f"【安全熔断】Tj={tj:.1f}℃ 超过保护上限 "
                            f"{safe_limit:.1f}℃"
                        )
                        self.log(f"🚨 {self.emergency_reason}")
                        self.emergency_shutdown.set()
                        self.control_running = False
                        break
                else:
                    self.overheat_count = 0

                self._compute_and_adjust(tj, now)
            except Exception as e:
                self.log(f"⚠️ [温控线程异常]: {e}")

    def _log_no_sample_once(self):
        now = time.time()
        if now - self.last_no_sample_log_time >= 6.0:
            self.last_no_sample_log_time = now
            self.log("[Tj采样] 正在持续轮询板端 Tj...")

    def _update_dynamics(self, tj, now):
        if self.last_sample_tj is None:
            self.last_sample_tj, self.last_sample_time = tj, now
            self.filtered_rate = self.filtered_accel = 0.0
            return

        dt = now - self.last_sample_time
        if dt < 0.3:
            return

        rate = max(-3.0, min(3.0, (tj - self.last_sample_tj) / dt))
        prev_rate = self.filtered_rate
        self.filtered_rate = 0.65 * prev_rate + 0.35 * rate
        accel = max(-0.40, min(0.40, (self.filtered_rate - prev_rate) / dt))
        self.filtered_accel = 0.75 * self.filtered_accel + 0.25 * accel
        self.last_sample_tj, self.last_sample_time = tj, now

    def _compute_and_adjust(self, tj: float, now: float):
        self._update_dynamics(tj, now)
        self.identifier.update_with_sample(tj, now)

        low = self.target_temp
        high = low + self.TARGET_BAND
        v = self.filtered_rate
        a = self.filtered_accel

        horizon = 6.0 if v > 0.35 else (4.5 if v > 0.05 else 3.2)
        pred = tj + v * horizon + 0.5 * max(0.0, a) * horizon * horizon

        if not self.initial_boost_done:
            self.initial_boost_done = True
            e = low - tj
            if e > 10.0 and v < 0.5:
                self._dispatch_temp_adjust(
                    tj, pred, "启动快速预热",
                    min(3, max(1, round(e / 10.0))), True
                )
                return

        error = low - tj
        if tj > high + 0.2 or pred > high + 0.55:
            dwell = 0.8
        elif abs(error) > 8.0:
            dwell = 1.0
        elif abs(error) > 3.0:
            dwell = 1.5
        else:
            dwell = 2.2
        if now - self.last_adjust_time < dwell:
            return

        if tj > high + 0.05:
            excess = tj - high
            if pred > high:
                excess = max(excess, min(2.5, pred - high))
            pulses = min(4, max(1, round(excess / max(0.10, self.identifier.cool_gain))))
            self._dispatch_temp_adjust(tj, pred, "自适应降温", pulses, False)
            return

        if (pred > high + 0.10 and v > 0.03) or (tj > low + 0.75 and v > 0.12):
            self._dispatch_temp_adjust(tj, pred, "前瞻制动", 1, False)
            return

        if low <= tj <= high:
            if abs(v) <= self.STABLE_RATE:
                return
            if tj < low + 0.35 and v < -0.05:
                self._dispatch_temp_adjust(tj, pred, "低界防跌", 1, True)
            elif tj > low + 0.80 and v > 0.05:
                self._dispatch_temp_adjust(tj, pred, "高界抑制", 1, False)
            return

        if tj < low:
            effective_error = low - pred
            if effective_error <= 0.05 and v >= 0:
                return

            if error > 12.0:
                pulses = 3 if v < 0.20 else 2
            elif error > 5.0:
                pulses = 2 if effective_error > 1.2 and v < 0.25 else 1
            else:
                pulses = 1

            self._dispatch_temp_adjust(tj, pred, "自适应升温", pulses, True)

    def _dispatch_temp_adjust(self, tj, pred_tj, action, pulses, is_up):
        pulses = max(1, min(4, int(pulses)))
        ok = self.rm1010.temp_up(pulses) if is_up else self.rm1010.temp_down(pulses)
        self.last_adjust_time = time.time()

        if ok:
            self.identifier.log_action(
                "UP" if is_up else "DOWN", pulses, tj, self.last_adjust_time
            )

        status = "已发送" if ok else "失败(RM1010未连接)"
        gain = self.identifier.heat_gain if is_up else self.identifier.cool_gain
        high = self.target_temp + self.TARGET_BAND
        self.log(
            f"[风枪温控] Tj={tj:.1f}℃ | 目标[{self.target_temp:.0f}~{high:.0f}]℃ | "
            f"预测={pred_tj:.1f}℃ | 增益={gain:.2f}℃/p -> "
            f"{action} {pulses}p [{status}]"
        )


# =========================================================================
# 4. Vmin 自动化控制主系统 (RM1010 14-Bit 高精 ADC + 板端软重启版)
# =========================================================================
class VminAutoTestGUI:
    UI_REFRESH_MS = 30
    CONFIG_FILE = "vmin_config.json"
    MAX_VOLTAGE, MIN_VOLTAGE = 30.00, 0.00
    MAX_CURRENT, MIN_CURRENT = 3.000, 0.000
    STEPS = [10.0, 1.0, 0.1, 0.01]
    FIXED_KERNEL_CMDS = (
        "su",
        "echo 0 > /proc/sys/kernel/printk",
        "mount -t debugfs none /sys/kernel/debug/",
        "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_available_frequencies",
        "chmod -R 777 /data",
    )

    THEME = {
        "bg": "#F2F5F9", "card_bg": "#FFFFFF", "card_border": "#D2DCED",
        "text_main": "#0B1528", "text_sub": "#5B6B82",
        "accent_blue": "#007AFF", "accent_green": "#34C759", "accent_red": "#FF3B30", "accent_amber": "#FF9500",
        "glass_blue_bg": "#007AFF", "glass_blue_hover": "#0062CC", "glass_blue_border": "#5AC8FA", "glass_blue_fg": "#FFFFFF",
        "glass_green_bg": "#34C759", "glass_green_hover": "#28A745", "glass_green_border": "#62E484", "glass_green_fg": "#FFFFFF",
        "glass_red_bg": "#FF3B30", "glass_red_hover": "#D70015", "glass_red_border": "#FF7A70", "glass_red_fg": "#FFFFFF",
        "glass_light_bg": "#E8EEF5", "glass_light_hover": "#D5E0EC", "glass_light_border": "#C4D4E6", "glass_light_fg": "#0B1528",
        "step_plus_bg": "#E1F0FF", "step_plus_hover": "#CCE4FF", "step_plus_border": "#99CCFF", "step_plus_fg": "#0066CC",
        "step_minus_bg": "#FFE8EB", "step_minus_hover": "#FFD1D7", "step_minus_border": "#FFA8B5", "step_minus_fg": "#CC0029"
    }

    FONT_FAMILY = "Microsoft YaHei UI"
    FONT_HERO = (FONT_FAMILY, 26, "bold")
    FONT_LARGE = (FONT_FAMILY, 22, "bold")
    FONT_MEDIUM = (FONT_FAMILY, 13, "bold")
    FONT_BASE = (FONT_FAMILY, 10)
    FONT_BASE_BOLD = (FONT_FAMILY, 10, "bold")
    FONT_SMALL = (FONT_FAMILY, 9)

    @staticmethod
    def _parse_temp_mode(mode_str: str) -> tuple[bool, float | None]:
        """
        解析温区模式：
        返回 (is_high_temp, temp_val)
        - '常温': (False, None)
        - '85℃' / '95' / '95.5℃': (True, 95.5) 若温度在合理安全区间 [30.0, 130.0]
        - 其余非法输入返回 (False, None)
        """
        s = str(mode_str).strip()
        if not s or s == "常温":
            return False, None
        clean_s = s.replace("℃", "").replace("°C", "").strip()
        try:
            val = float(clean_s)
            if 30.0 <= val <= 130.0:
                return True, round(val, 1)
        except ValueError:
            pass
        return False, None

    def __init__(self, root):
        self.root = root
        self.root.title("Vmin 智能自动化测试控制系统 (RM1010 14-Bit 高精 ADC 闭环版)")
        self.root.configure(bg=self.THEME["bg"])

        self.lock = threading.RLock()
        self._main_thread_id = threading.get_ident()
        self._closing = False
        self.running = True
        self.ui_queue = queue.Queue()
        self.log_queue = queue.Queue()
        self._early_logs = []
        self._log_lines_approx = 0
        self.serial_threads = []
        self._cached_ports = []
        self._last_port_scan_time = 0.0

        self._pulse_queue = queue.Queue()
        self._hold_timer = None
        self._repeat_timer = None
        self._current_hold_dir = None

        self.rm1010 = RM1010Driver(log_func=self.append_log)
        self.heatgun = HeatGunDriver(log_func=self.append_log)
        self.tj_parser = TjStreamParser()
        self.thermal_mgr = SmartThermalManager(
            self.rm1010, self.heatgun, self.tj_parser, self.send_board_cmd, self.append_log
        )

        self.rt_watchdog = RealtimeStreamWatchdog()
        self.boot_mgr = AdaptiveBootManager(default_delay=18.0)
        self.voltage_preempted_event = threading.Event()

        self._test_reboot_count = 0
        self._test_finalized = False
        self._test_reboot_lock = threading.RLock()
        self._formal_case1_reboot_mono = 0.0

        self._last_v_hero_str = ""
        self._last_v_stats_tuple = None
        self._last_pwr_rt_tuple = None
        self._last_pwr_output_state = None
        self._last_tj_str = ""
        self._last_rm_status_str = ""
        self._last_hg_btn_state = None
        self._displayed_sample_seq = 0
        self._last_conn_states = {"board": False, "pwr": False, "hg": False}

        self.board_port, self.board_baud, self.board_active = "", 921600, False
        self.board_log_chip_id = ""
        self.board_log_path = ""
        self.pending_board_cmds = queue.Queue()
        self.detected_chip_id = ""
        self.latest_chip_id = ""

        self.pwr_port, self.pwr_baud, self.pwr_active = "", 9600, False
        self.pwr_measured_v = 0.00
        self.pwr_measured_i = 0.000
        self.pwr_output_state = True
        self.pwr_target_v, self.pwr_target_i = 0.00, 0.500
        self.pwr_pending_vset, self.pwr_pending_iset, self.pwr_pending_output = None, None, None

        self.hg_port, self.hg_baud, self.hg_active = "", 921600, False

        self.test_running = False
        self.test_thread = None

        self.saved_cfg = self.load_saved_config()
        self.step_delay_value = self._positive_float(self.saved_cfg.get("step_delay", 15.0), 15.0)
        self.fast_delay_value = self._positive_float(self.saved_cfg.get("fast_delay", 1.0), 1.0)
        self.reboot_delay_value = self._positive_float(self.saved_cfg.get("reboot_delay", 18.0), 18.0)
        self.pwr_target_i = self._bounded_float(self.saved_cfg.get("pwr_iset", self.pwr_target_i), self.pwr_target_i, self.MIN_CURRENT, self.MAX_CURRENT)
        raw_saved_temp = str(self.saved_cfg.get("target_temp", "常温"))
        is_high_mode, parsed_saved_temp = self._parse_temp_mode(raw_saved_temp)
        self.target_temp_mode = f"{parsed_saved_temp:g}℃" if is_high_mode else "常温"
        self.excel_path = str(self.saved_cfg.get("excel_file", "vmin_test.xlsx"))

        self.heatgun.is_on = (self.target_temp_mode != "常温")

        self.var_step_delay = tk.StringVar(value=str(self.step_delay_value))
        self.var_fast_delay = tk.StringVar(value=str(self.fast_delay_value))
        self.var_reboot_delay = tk.StringVar(value=str(self.reboot_delay_value))

        self.var_step_delay.trace_add("write", self._on_step_delay_var_change)
        self.var_fast_delay.trace_add("write", self._on_fast_delay_var_change)
        self.var_reboot_delay.trace_add("write", self._on_reboot_delay_var_change)

        self.create_widgets()
        self._init_window_geometry()

        if self._early_logs:
            for l in self._early_logs:
                self.txt_log.insert(tk.END, l)
            self._early_logs.clear()

        self.serial_threads = [
            threading.Thread(target=self.board_serial_loop, name="board_serial_loop", daemon=True),
            threading.Thread(target=self.pwr_serial_loop, name="pwr_serial_loop", daemon=True),
            threading.Thread(target=self.hg_serial_loop, name="hg_serial_loop", daemon=True),
            threading.Thread(target=self._rm1010_auto_scan_loop, name="rm1010_scan_loop", daemon=True),
            threading.Thread(target=self._pulse_worker_loop, name="pulse_worker_loop", daemon=True),
            threading.Thread(target=self._port_scan_worker_loop, name="port_scan_worker_loop", daemon=True)
        ]
        for thread in self.serial_threads:
            thread.start()

        self._schedule_ui_loop()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        try:
            self.root.update_idletasks()
            self.root.deiconify()
        except Exception: pass

    def _on_step_delay_var_change(self, *args):
        try:
            val = float(self.var_step_delay.get().strip())
            if val > 0:
                self.step_delay_value = val
                self.save_config("step_delay", val)
        except Exception: pass

    def _on_fast_delay_var_change(self, *args):
        try:
            val = float(self.var_fast_delay.get().strip())
            if val > 0:
                self.fast_delay_value = val
                self.save_config("fast_delay", val)
        except Exception: pass

    def _on_reboot_delay_var_change(self, *args):
        try:
            val = float(self.var_reboot_delay.get().strip())
            if val > 0:
                self.reboot_delay_value = val
                self.save_config("reboot_delay", val)
        except Exception: pass

    def _init_window_geometry(self):
        try:
            self.root.update_idletasks()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            w = min(1400, max(1040, sw - 40))
            h = min(920, max(740, sh - 80))
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            self.root.minsize(1020, 700)
        except Exception:
            self.root.geometry("1400x900")

    def _port_scan_worker_loop(self):
        last_ports_set = None
        while self.running:
            try:
                current_ports = [p.device for p in serial.tools.list_ports.comports()]
                current_ports_set = set(current_ports)
                
                if last_ports_set is None:
                    last_ports_set = current_ports_set
                    with self.lock:
                        self._cached_ports = current_ports
                    self._ui_call(self._init_ports_on_startup, current_ports)
                elif current_ports_set != last_ports_set:
                    inserted = current_ports_set - last_ports_set
                    removed = last_ports_set - current_ports_set
                    last_ports_set = current_ports_set
                    with self.lock:
                        self._cached_ports = current_ports
                    self._on_ports_difference(current_ports, inserted, removed)
            except Exception:
                pass
            time.sleep(1.5)

    def _on_ports_difference(self, current_ports: list, inserted: set, removed: set):
        for prefix in ["board", "pwr", "hg"]:
            if getattr(self, f"{prefix}_active", False):
                cur_p = getattr(self, f"{prefix}_port", "")
                if cur_p in removed:
                    with self.lock:
                        setattr(self, f"{prefix}_active", False)
                    self.append_log(f"⚠️ [USB热插拔感知] {self._unit_label(prefix)} 物理端口 ({cur_p}) 已拔出！")
                    if prefix == "hg":
                        self.heatgun.close()
                    self._ui_call(self._reset_conn_btn_to_idle, prefix)

        self._ui_call(self._refresh_combobox_ports_ui, current_ports)

    def _reset_conn_btn_to_idle(self, prefix: str):
        btn = getattr(self, f"btn_{prefix}_conn", None)
        if btn:
            btn.config(text="连接")
            self._apply_liquid_glass_btn(btn, self.THEME["glass_blue_bg"], "white", self.THEME["glass_blue_hover"], self.THEME["glass_blue_border"])

    def _refresh_combobox_ports_ui(self, ports: list):
        display = ports if ports else ["无可用串口"]
        for prefix in ["board", "pwr", "hg"]:
            cb = getattr(self, f"cb_{prefix}_port", None)
            if not cb: continue
            saved = self.saved_cfg.get(f"{prefix}_port", "")
            cb['values'] = display
            if cb.get() not in display and ports:
                cb.set(saved if saved in ports else display[0])

    def _init_ports_on_startup(self, ports: list):
        if self._closing: return
        display = ports if ports else ["无可用串口"]
        for prefix in ["board", "pwr", "hg"]:
            cb = getattr(self, f"cb_{prefix}_port", None)
            if not cb: continue
            saved = self.saved_cfg.get(f"{prefix}_port", "")
            cb['values'] = display
            if saved and saved in ports:
                cb.set(saved)
                self.auto_connect_unit(prefix, saved)
            elif ports:
                cb.current(0)
            elif saved:
                cb.set(saved)

    def _pulse_worker_loop(self):
        while self.running:
            try:
                action = self._pulse_queue.get(timeout=0.2)
                if self.target_temp_mode == "常温":
                    continue
                if action == "UP":
                    ok = self.rm1010.temp_up(1)
                    if ok: self.append_log("[手动温控] 升温 +1℃")
                    else: self.append_log(f"[手动温控] 升温失败 ({self.rm1010.last_error or 'RM1010 未连接'})")
                elif action == "DOWN":
                    ok = self.rm1010.temp_down(1)
                    if ok: self.append_log("[手动温控] 降温 -1℃")
                    else: self.append_log(f"[手动温控] 降温失败 ({self.rm1010.last_error or 'RM1010 未连接'})")
            except queue.Empty:
                pass
            except Exception as e:
                self.append_log(f"⚠️ [温度控制队列异常]: {e}")

    def reset_heatgun_to_80(self, reason: str = "", force_baseline: bool = False):
        self.thermal_mgr.stop_all()

        if self.target_temp_mode == "常温" and not force_baseline:
            try:
                self.heatgun.power_off(source="NORMAL_MODE")
            except Exception:
                pass
            return

        if self.rm1010.connected:
            self.append_log(f">>> [风枪归底] {reason} -> RM1010 下发 45 个降温脉冲回归 80℃基准...")
            threading.Thread(target=lambda: self.rm1010.temp_down_fast(45), daemon=True).start()

    def _rm1010_auto_scan_loop(self):
        first_check = True
        while self.running:
            try:
                conn, state_changed = self.rm1010.scan_and_sync()
                if state_changed:
                    if conn:
                        self.append_log(f"⚡ [RM1010识别成功] SN: {self.rm1010.device_sn} | 14-Bit NPLC 200ms ADC采样已锁定 (P15基准={self.rm1010.vref_physical:.4f}V)")
                        self._ui_call(lambda v=self.rm1010.vref_physical: (self.ent_vref_physical.delete(0, tk.END), self.ent_vref_physical.insert(0, f"{v:.4f}")))
                        if self.target_temp_mode != "常温":
                            self.reset_heatgun_to_80("RM1010就绪/热插拔恢复")
                    else:
                        if not first_check:
                            self.append_log("⚠️ [RM1010掉线/已拔出] 硬件已断开连接！")
                elif first_check and not conn:
                    err_hint = self.rm1010.dll_error or self.rm1010.last_error or "未检测到硬件 (等待热插拔)"
                    self.append_log(f"[RM1010状态] {err_hint}")
                first_check = False
            except Exception as e:
                if first_check:
                    self.append_log(f"⚠️ [RM1010自检异常]: {e}")
                    first_check = False
            time.sleep(1.0)

    def load_saved_config(self) -> dict:
        try:
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            self.append_log(f"⚠️ [配置读取提示] 使用默认配置 ({e})")
        return {}

    def save_config(self, key, value):
        self.saved_cfg[key] = value
        try:
            with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.saved_cfg, f, ensure_ascii=False, indent=2)
        except Exception: pass

    @staticmethod
    def _positive_float(value, default: float) -> float:
        try:
            val = float(value)
            return val if val > 0 else default
        except Exception: return default

    @staticmethod
    def _bounded_float(value, default: float, min_value: float, max_value: float) -> float:
        try: return max(min_value, min(max_value, float(value)))
        except Exception: return default

    def _is_ui_thread(self) -> bool:
        return threading.get_ident() == self._main_thread_id

    def _ui_call(self, func, *args, **kwargs):
        if self._closing: return
        if self._is_ui_thread():
            try: func(*args, **kwargs)
            except (RuntimeError, tk.TclError): pass
            return
        try: self.ui_queue.put_nowait((func, args, kwargs))
        except Exception: pass

    def _schedule_ui_loop(self):
        if not self.running or self._closing: return
        try: self.root.after(self.UI_REFRESH_MS, self.update_ui_loop)
        except (RuntimeError, tk.TclError): pass

    def _drain_gui_queues(self):
        buf = []
        while len(buf) < 1000:
            try: buf.append(self.log_queue.get_nowait())
            except queue.Empty: break
        if buf:
            joined = "".join(buf)
            self.txt_log.insert(tk.END, joined)
            self._log_lines_approx += joined.count("\n")
            if self._log_lines_approx > 3000:
                try:
                    line_count = int(self.txt_log.index("end-1c").split(".")[0])
                    if line_count > 3000:
                        self.txt_log.delete("1.0", f"{line_count - 2000}.0")
                        self._log_lines_approx = 2000
                    else:
                        self._log_lines_approx = line_count
                except Exception:
                    pass
            self.txt_log.see(tk.END)

        for _ in range(100):
            try: func, args, kwargs = self.ui_queue.get_nowait()
            except queue.Empty: break
            try: func(*args, **kwargs)
            except (RuntimeError, tk.TclError): pass

    def get_step_delay(self) -> float:
        try:
            val = float(self.var_step_delay.get().strip())
            if val > 0:
                self.step_delay_value = val
                return val
        except Exception: pass
        return self.step_delay_value

    def get_fast_ramp_delay(self) -> float:
        try:
            val = float(self.var_fast_delay.get().strip())
            if val > 0:
                self.fast_delay_value = val
                return val
        except Exception: pass
        return self.fast_delay_value

    def get_reboot_delay(self) -> float:
        try:
            val = float(self.var_reboot_delay.get().strip())
            if val > 0:
                self.reboot_delay_value = val
                return val
        except Exception: pass
        return self.reboot_delay_value

    def _create_card(self, parent, bg=None, border_color=None, pady=0, padx=0):
        return tk.Frame(
            parent, bg=bg or self.THEME["card_bg"],
            highlightbackground=border_color or self.THEME["card_border"],
            highlightthickness=1, padx=padx, pady=pady
        )

    def _apply_liquid_glass_btn(self, btn, bg, fg, hover_bg, border_color="#C4D4E6", disabled_bg=None, disabled_fg=None):
        disabled_bg = disabled_bg or "#F2F4F7"
        disabled_fg = disabled_fg or "#8E8E93"
        btn.config(
            bg=bg, fg=fg, activebackground=hover_bg, activeforeground=fg,
            disabledforeground=disabled_fg, font=self.FONT_BASE_BOLD,
            relief="flat", bd=0, highlightthickness=1, highlightbackground=border_color, cursor="hand2"
        )
        btn.bind("<Enter>", lambda e: btn.config(bg=hover_bg) if str(btn['state']) != 'disabled' else None)
        btn.bind("<Leave>", lambda e: btn.config(bg=bg if str(btn['state']) != 'disabled' else disabled_bg) if str(btn['state']) != 'disabled' else None)

    def append_log(self, msg: str):
        if self._closing: return
        full_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n"
        if hasattr(self, "txt_log"):
            try: self.log_queue.put_nowait(full_msg)
            except Exception: pass
        else:
            self._early_logs.append(full_msg)

    def append_log_raw(self, full_msg: str):
        if self._closing: return
        msg = full_msg if full_msg.endswith("\n") else f"{full_msg}\n"
        if hasattr(self, "txt_log"):
            try: self.log_queue.put_nowait(msg)
            except Exception: pass
        else:
            self._early_logs.append(msg)

    def browse_excel_file(self):
        filename = filedialog.askopenfilename(
            title="选择 Excel 测试文件",
            filetypes=[("Excel Files", "*.xlsx *.xls"), ("All Files", "*.*")]
        )
        if filename:
            self.excel_path = filename
            self.ent_excel_file.delete(0, tk.END)
            self.ent_excel_file.insert(0, filename)
            self.save_config("excel_file", filename)

    def _update_temp_combobox_values(self, active_mode: str):
        presets = ["常温", "85℃", "105℃", "115℃"]
        vals = list(presets)
        if active_mode not in presets and active_mode != "常温":
            vals.append(active_mode)
        vals.append("自定义...")
        self.cb_target_temp['values'] = vals
        self.cb_target_temp.set(active_mode)

    def on_target_temp_changed(self, event=None):
        selected = self.cb_target_temp.get().strip()
        old_mode = self.target_temp_mode

        if selected == "自定义...":
            cur_val = 90.0
            is_h, old_val = self._parse_temp_mode(old_mode)
            if is_h and old_val is not None:
                cur_val = old_val

            custom_val = simpledialog.askfloat(
                "自定义目标温度",
                "请输入自定义高温目标温度 (℃，范围 30~130):",
                initialvalue=cur_val,
                minvalue=30.0,
                maxvalue=130.0,
                parent=self.root
            )
            if custom_val is not None:
                new_mode = f"{custom_val:g}℃"
                self._update_temp_combobox_values(new_mode)
                self.target_temp_mode = new_mode
            else:
                self.cb_target_temp.set(old_mode)
                return
        else:
            is_high, parsed_val = self._parse_temp_mode(selected)
            if is_high:
                new_mode = f"{parsed_val:g}℃"
                self._update_temp_combobox_values(new_mode)
                self.target_temp_mode = new_mode
            else:
                self.target_temp_mode = "常温"
                self._update_temp_combobox_values("常温")

        self.save_config("target_temp", self.target_temp_mode)
        self.thermal_mgr.stop_thermal_control()
        if self.target_temp_mode == "常温":
            self.heatgun.power_off(source="TEMP_MODE_NORMAL")
            self.append_log("🌡️ [温控模式] 已切换为【常温】→ 风枪 12V 供电常 OFF，无需温控代码干预，仅保留板端 P8 ADC 与 Tj 采样。")
        else:
            if old_mode == "常温":
                self.reset_heatgun_to_80("高温预备", force_baseline=True)
            self.heatgun.power_on(source="TEMP_MODE_HIGH")
            self.append_log(f"🌡️ [温控模式] 已切换为【{self.target_temp_mode}】→ 启用风枪 12V 供电与在线热模型闭环温控。")
        self._sync_heatgun_button_state()

    def on_vref_updated(self, event=None):
        try:
            val = float(self.ent_vref_physical.get().strip())
            if 2.0 <= val <= 4.0:
                self.rm1010.update_vref_setting(val)
                self.append_log(f"⚡ [物理基准更新] 已设置 P15 3.3V 实际基准为: {val:.4f} V (已持久化绑定 SN:{self.rm1010.device_sn or '默认'})")
            else:
                self.append_log("⚠️ [基准电压异常] 建议在 2.5V ~ 3.5V 范围内！")
        except ValueError:
            self.append_log("⚠️ [输入格式错误] 请输入有效浮点数电压！")

    def _on_temp_press(self, direction: str):
        if self.target_temp_mode == "常温":
            self.append_log("⚠️ [温控提示] 当前为【常温】模式，如需风枪加减温请先选择高温温区。")
            return
        self._current_hold_dir = direction
        self._pulse_queue.put(direction)

        if self._hold_timer:
            self.root.after_cancel(self._hold_timer)
            self._hold_timer = None
        if self._repeat_timer:
            self.root.after_cancel(self._repeat_timer)
            self._repeat_timer = None

        self._hold_timer = self.root.after(400, self._start_hold_repeat, direction)

    def _start_hold_repeat(self, direction: str):
        self._hold_timer = None
        self._repeat_step(direction)

    def _repeat_step(self, direction: str):
        if self._current_hold_dir == direction and self.rm1010.connected and self.target_temp_mode != "常温":
            self._pulse_queue.put(direction)
            self._repeat_timer = self.root.after(160, self._repeat_step, direction)

    def _on_temp_release(self, event=None):
        self._current_hold_dir = None
        if self._hold_timer:
            self.root.after_cancel(self._hold_timer)
            self._hold_timer = None
        if self._repeat_timer:
            self.root.after_cancel(self._repeat_timer)
            self._repeat_timer = None

    def _sync_heatgun_button_state(self):
        hg_active = self.hg_active and bool(self.heatgun.ser and getattr(self.heatgun.ser, "is_open", False))
        hg_is_on = self.heatgun.is_on
        self._last_hg_btn_state = (hg_active, hg_is_on)
        if not hg_active:
            self.btn_hg_toggle.config(
                text="风枪12V: OFF", state="disabled",
                bg=self.THEME["glass_light_bg"], fg=self.THEME["text_sub"],
                activebackground=self.THEME["glass_light_bg"], activeforeground=self.THEME["text_sub"],
                highlightbackground=self.THEME["glass_light_border"]
            )
        else:
            self.btn_hg_toggle.config(state="normal")
            if hg_is_on:
                self.btn_hg_toggle.config(
                    text="风枪12V: ON ",
                    bg=self.THEME["glass_green_bg"], fg=self.THEME["glass_green_fg"],
                    activebackground=self.THEME["glass_green_hover"], activeforeground=self.THEME["glass_green_fg"],
                    highlightbackground=self.THEME["glass_green_border"]
                )
            else:
                self.btn_hg_toggle.config(
                    text="风枪12V: OFF",
                    bg=self.THEME["glass_light_bg"], fg=self.THEME["glass_light_fg"],
                    activebackground=self.THEME["glass_light_hover"], activeforeground=self.THEME["glass_light_fg"],
                    highlightbackground=self.THEME["glass_light_border"]
                )

    def _on_hg_btn_enter(self, event):
        if str(self.btn_hg_toggle['state']) == 'disabled': return
        if self.heatgun.is_on and self.hg_active:
            self.btn_hg_toggle.config(bg=self.THEME["glass_green_hover"])
        else:
            self.btn_hg_toggle.config(bg=self.THEME["glass_light_hover"])

    def _on_hg_btn_leave(self, event):
        if str(self.btn_hg_toggle['state']) == 'disabled': return
        if self.heatgun.is_on and self.hg_active:
            self.btn_hg_toggle.config(bg=self.THEME["glass_green_bg"])
        else:
            self.btn_hg_toggle.config(bg=self.THEME["glass_light_bg"])

    def toggle_heatgun_manual(self):
        if not self.hg_active or not self.heatgun._opened(self.heatgun.ser):
            self.append_log("⚠️ [风枪操作] 请先连接风枪控制串口！")
            return
        self.btn_hg_toggle.config(state="disabled")

        def _toggle():
            try:
                is_on = self.heatgun.toggle()
                state_txt = "常开 (ON)" if is_on else "常关 (OFF)"
                self.append_log(f">>> [手动控制] 风枪 12V 供电已切换为: 【{state_txt}】！")
            except Exception as e:
                self.append_log(f"⚠️ [风枪操作] 切换失败: {e}")
            finally:
                self._ui_call(self._sync_heatgun_button_state)

        threading.Thread(target=_toggle, name="HeatGun_Manual_Toggle", daemon=True).start()

    def _sync_pwr_output_button_state(self):
        pwr_active = self.pwr_active
        p_out = self.pwr_output_state if pwr_active else False
        self._last_pwr_output_state = (pwr_active, p_out)
        if not pwr_active:
            self.btn_pwr_output.config(
                text="OUT: OFF", state="disabled",
                bg=self.THEME["glass_light_bg"], fg=self.THEME["text_sub"],
                activebackground=self.THEME["glass_light_bg"], activeforeground=self.THEME["text_sub"],
                highlightbackground=self.THEME["glass_light_border"]
            )
        else:
            self.btn_pwr_output.config(state="normal")
            if p_out:
                self.btn_pwr_output.config(
                    text="OUT: ON ",
                    bg=self.THEME["glass_green_bg"], fg=self.THEME["glass_green_fg"],
                    activebackground=self.THEME["glass_green_hover"], activeforeground=self.THEME["glass_green_fg"],
                    highlightbackground=self.THEME["glass_green_border"]
                )
            else:
                self.btn_pwr_output.config(
                    text="OUT: OFF",
                    bg=self.THEME["glass_light_bg"], fg=self.THEME["glass_light_fg"],
                    activebackground=self.THEME["glass_light_hover"], activeforeground=self.THEME["glass_light_fg"],
                    highlightbackground=self.THEME["glass_light_border"]
                )

    def _on_pwr_btn_enter(self, event):
        if str(self.btn_pwr_output['state']) == 'disabled': return
        if self.pwr_output_state and self.pwr_active:
            self.btn_pwr_output.config(bg=self.THEME["glass_green_hover"])
        else:
            self.btn_pwr_output.config(bg=self.THEME["glass_light_hover"])

    def _on_pwr_btn_leave(self, event):
        if str(self.btn_pwr_output['state']) == 'disabled': return
        if self.pwr_output_state and self.pwr_active:
            self.btn_pwr_output.config(bg=self.THEME["glass_green_bg"])
        else:
            self.btn_pwr_output.config(bg=self.THEME["glass_light_bg"])

    def toggle_pwr_output(self):
        with self.lock:
            self.pwr_pending_output = not self.pwr_output_state

    def copy_to_clipboard(self, text: str, label_name: str):
        if not text or any(k in text for k in ["等待", "断开", "失败", "错误", "--"]): return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.append_log(f"📋 已复制{label_name}: {text.replace(chr(9), '  ')}")
        except Exception: pass

    def copy_realtime_v(self):
        with self.rm1010.lock:
            v = self.rm1010.voltage_val
        if v is not None:
            self.copy_to_clipboard(f"{v:.5f}", "实时核心电压")

    def copy_max_v(self):
        with self.rm1010.lock:
            v = self.rm1010.stats.max_val if self.rm1010.stats.count > 0 else None
        if v is not None:
            self.copy_to_clipboard(f"{v:.5f}", "最大电压")

    def copy_min_v(self):
        with self.rm1010.lock:
            v = self.rm1010.stats.min_val if self.rm1010.stats.count > 0 else None
        if v is not None:
            self.copy_to_clipboard(f"{v:.5f}", "最小电压")

    def copy_avg_v(self):
        with self.rm1010.lock:
            v = self.rm1010.stats.mean if self.rm1010.stats.count > 0 else None
        if v is not None:
            self.copy_to_clipboard(f"{v:.5f}", "平均电压")

    def copy_all_v_stats(self):
        with self.rm1010.lock:
            if self.rm1010.stats.count > 0:
                s = f"{self.rm1010.stats.max_val:.5f}\t{self.rm1010.stats.min_val:.5f}\t{self.rm1010.stats.mean:.5f}"
                self.copy_to_clipboard(s, "MAX/MIN/AVG (Excel格式)")

    def clear_voltage_stats(self):
        self.rm1010.clear_stats()
        self.append_log("🧹 已清空 RM1010 P8 核心电压采样统计数据。")

    def create_widgets(self):
        main_container = tk.Frame(self.root, bg=self.THEME["bg"], padx=14, pady=8)
        main_container.pack(fill=tk.BOTH, expand=True)

        card_ports = self._create_card(main_container, pady=6, padx=10)
        card_ports.pack(fill=tk.X, pady=(0, 6))

        ports_grid = tk.Frame(card_ports, bg=self.THEME["card_bg"])
        ports_grid.pack(fill=tk.X)
        ports_grid.columnconfigure(0, weight=14)
        ports_grid.columnconfigure(1, weight=11)
        ports_grid.columnconfigure(2, weight=11)
        ports_grid.columnconfigure(3, weight=16)

        tk.Label(
            ports_grid, text="智能硬件通信与温控配置 (RM1010 14-Bit 高精 ADC 闭环版)",
            font=self.FONT_MEDIUM, fg=self.THEME["text_main"], bg=self.THEME["card_bg"]
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

        self.btn_hg_toggle = tk.Button(
            ports_grid, text="风枪12V: OFF", command=self.toggle_heatgun_manual,
            width=14, padx=4, pady=1, font=self.FONT_BASE_BOLD,
            relief="flat", bd=0, highlightthickness=1, highlightbackground=self.THEME["card_border"], cursor="hand2"
        )
        self.btn_hg_toggle.grid(row=0, column=2, sticky="w", padx=(0, 6), pady=(0, 4))
        self.btn_hg_toggle.bind("<Enter>", self._on_hg_btn_enter)
        self.btn_hg_toggle.bind("<Leave>", self._on_hg_btn_leave)

        self.lbl_rm1010_status = tk.Label(
            ports_grid, text="RM1010: 检测中...", width=38, anchor="w",
            font=self.FONT_BASE_BOLD, fg=self.THEME["accent_amber"], bg=self.THEME["card_bg"]
        )
        self.lbl_rm1010_status.grid(row=0, column=3, sticky="w", padx=(0, 0), pady=(0, 4))

        self.create_serial_unit_ui(ports_grid, col=0, title="① 板子调试串口", prefix="board", default_baud="921600", show_baud=True)
        self.create_serial_unit_ui(ports_grid, col=1, title="② 直流电源 (KA3003P)", prefix="pwr", default_baud="9600", show_baud=False)
        self.create_serial_unit_ui(ports_grid, col=2, title="③ 风枪控制串口", prefix="hg", default_baud="921600", show_baud=False)

        frame_rm_ctrl = tk.Frame(ports_grid, bg=self.THEME["card_bg"])
        frame_rm_ctrl.grid(row=1, column=3, sticky="w", padx=(0, 0))

        row_vref = tk.Frame(frame_rm_ctrl, bg=self.THEME["card_bg"])
        row_vref.pack(anchor="w", pady=(0, 2))
        tk.Label(row_vref, text="P15基准:", font=self.FONT_BASE_BOLD, fg=self.THEME["text_main"], bg=self.THEME["card_bg"]).pack(side=tk.LEFT, padx=(0, 2))
        self.ent_vref_physical = tk.Entry(row_vref, font=self.FONT_BASE_BOLD, width=7, justify="center")
        self.ent_vref_physical.insert(0, f"{self.rm1010.vref_physical:.4f}")
        self.ent_vref_physical.pack(side=tk.LEFT, padx=(0, 2))
        self.ent_vref_physical.bind("<Return>", self.on_vref_updated)
        self.ent_vref_physical.bind("<FocusOut>", self.on_vref_updated)
        tk.Label(row_vref, text="V", font=self.FONT_BASE, fg=self.THEME["text_sub"], bg=self.THEME["card_bg"]).pack(side=tk.LEFT, padx=(0, 6))

        tk.Label(row_vref, text="温区:", font=self.FONT_BASE_BOLD, fg=self.THEME["text_main"], bg=self.THEME["card_bg"]).pack(side=tk.LEFT, padx=(0, 2))
        self.cb_target_temp = ttk.Combobox(row_vref, width=6, font=self.FONT_BASE)
        self._update_temp_combobox_values(self.target_temp_mode)
        self.cb_target_temp.pack(side=tk.LEFT, padx=(0, 4))
        self.cb_target_temp.bind("<<ComboboxSelected>>", self.on_target_temp_changed)
        self.cb_target_temp.bind("<Return>", self.on_target_temp_changed)

        btn_t_up = tk.Button(row_vref, text="+1℃", width=4, padx=2, pady=1)
        btn_t_up.pack(side=tk.LEFT, padx=(0, 2))
        self._apply_liquid_glass_btn(btn_t_up, self.THEME["step_plus_bg"], self.THEME["step_plus_fg"], self.THEME["step_plus_hover"], self.THEME["step_plus_border"])
        btn_t_up.bind("<ButtonPress-1>", lambda e: self._on_temp_press("UP"))
        btn_t_up.bind("<ButtonRelease-1>", self._on_temp_release)

        btn_t_down = tk.Button(row_vref, text="-1℃", width=4, padx=2, pady=1)
        btn_t_down.pack(side=tk.LEFT)
        self._apply_liquid_glass_btn(btn_t_down, self.THEME["step_minus_bg"], self.THEME["step_minus_fg"], self.THEME["step_minus_hover"], self.THEME["step_minus_border"])
        btn_t_down.bind("<ButtonPress-1>", lambda e: self._on_temp_press("DOWN"))
        btn_t_down.bind("<ButtonRelease-1>", self._on_temp_release)

        mid_frame = tk.Frame(main_container, bg=self.THEME["bg"])
        mid_frame.pack(fill=tk.X, pady=(0, 6))
        mid_frame.columnconfigure(0, weight=1)
        mid_frame.columnconfigure(1, weight=1)

        card_volt = self._create_card(mid_frame, pady=6, padx=10)
        card_volt.grid(row=0, column=0, sticky="nsew", padx=(0, 3))

        f_volt_top = tk.Frame(card_volt, bg=self.THEME["card_bg"])
        f_volt_top.pack(fill=tk.X, pady=(0, 2))
        tk.Label(
            f_volt_top, text="RM1010 P8 实时核心电压反馈 (14-Bit NPLC 200ms)",
            font=self.FONT_MEDIUM, fg=self.THEME["text_main"], bg=self.THEME["card_bg"]
        ).pack(side=tk.LEFT)

        btn_copy_all = tk.Button(f_volt_top, text="复制全部", command=self.copy_all_v_stats, pady=1, padx=4)
        btn_copy_all.pack(side=tk.RIGHT, padx=(2, 0))
        self._apply_liquid_glass_btn(btn_copy_all, self.THEME["glass_light_bg"], self.THEME["text_main"], self.THEME["glass_light_hover"], self.THEME["glass_light_border"])

        btn_volt_clear = tk.Button(f_volt_top, text="清空统计", command=self.clear_voltage_stats, pady=1, padx=4)
        btn_volt_clear.pack(side=tk.RIGHT, padx=(2, 2))
        self._apply_liquid_glass_btn(btn_volt_clear, self.THEME["glass_light_bg"], self.THEME["text_main"], self.THEME["glass_light_hover"], self.THEME["glass_light_border"])

        box_hero = tk.Frame(card_volt, bg=self.THEME["card_bg"])
        box_hero.pack(fill=tk.X, pady=1)
        self.lbl_volt_hero = tk.Label(
            box_hero, text="-- V", font=self.FONT_HERO, width=14,
            fg=self.THEME["accent_green"], bg=self.THEME["card_bg"], anchor="w", cursor="hand2"
        )
        self.lbl_volt_hero.pack(side=tk.LEFT)
        self.lbl_volt_hero.bind("<Button-1>", lambda e: self.copy_realtime_v())

        self.lbl_volt_meta = tk.Label(
            card_volt, text="P8均值码: -- | P15基准码: -- | 读数: #0 (5Hz) | 波动: --mV",
            font=self.FONT_SMALL, fg=self.THEME["text_sub"], bg=self.THEME["card_bg"], anchor="w"
        )
        self.lbl_volt_meta.pack(fill=tk.X, pady=(0, 2))

        volt_stats_grid = tk.Frame(card_volt, bg=self.THEME["card_bg"])
        volt_stats_grid.pack(fill=tk.X, pady=(1, 0))
        for i in range(4): volt_stats_grid.columnconfigure(i, weight=1)

        for i, (h, c) in enumerate([("Max", self.THEME["accent_red"]), ("Min", self.THEME["accent_blue"]), ("Avg", self.THEME["accent_amber"]), ("采样", self.THEME["text_sub"])]):
            tk.Label(volt_stats_grid, text=h, font=self.FONT_BASE_BOLD, fg=c, bg=self.THEME["card_bg"]).grid(row=0, column=i, sticky="ew")

        self.lbl_volt_max = tk.Label(volt_stats_grid, text="--", font=self.FONT_MEDIUM, fg=self.THEME["accent_red"], bg=self.THEME["card_bg"], width=8, anchor="c", cursor="hand2")
        self.lbl_volt_max.grid(row=1, column=0, sticky="ew")
        self.lbl_volt_max.bind("<Button-1>", lambda e: self.copy_max_v())

        self.lbl_volt_min = tk.Label(volt_stats_grid, text="--", font=self.FONT_MEDIUM, fg=self.THEME["accent_blue"], bg=self.THEME["card_bg"], width=8, anchor="c", cursor="hand2")
        self.lbl_volt_min.grid(row=1, column=1, sticky="ew")
        self.lbl_volt_min.bind("<Button-1>", lambda e: self.copy_min_v())

        self.lbl_volt_avg = tk.Label(volt_stats_grid, text="--", font=self.FONT_MEDIUM, fg=self.THEME["accent_amber"], bg=self.THEME["card_bg"], width=8, anchor="c", cursor="hand2")
        self.lbl_volt_avg.grid(row=1, column=2, sticky="ew")
        self.lbl_volt_avg.bind("<Button-1>", lambda e: self.copy_avg_v())

        self.lbl_volt_count = tk.Label(volt_stats_grid, text="0 次", font=self.FONT_BASE, fg=self.THEME["text_sub"], bg=self.THEME["card_bg"], width=8, anchor="c")
        self.lbl_volt_count.grid(row=1, column=3, sticky="ew")

        card_pwr = self._create_card(mid_frame, pady=6, padx=10)
        card_pwr.grid(row=0, column=1, sticky="nsew", padx=(3, 0))

        pwr_top_box = tk.Frame(card_pwr, bg=self.THEME["card_bg"])
        pwr_top_box.pack(fill=tk.X, pady=(0, 2))
        tk.Label(pwr_top_box, text="KA3003P DC 调压源", font=self.FONT_MEDIUM, fg=self.THEME["text_main"], bg=self.THEME["card_bg"]).pack(side=tk.LEFT)

        self.btn_pwr_output = tk.Button(
            pwr_top_box, text="OUT: OFF", command=self.toggle_pwr_output,
            width=10, padx=4, pady=1, font=self.FONT_BASE_BOLD,
            relief="flat", bd=0, highlightthickness=1, highlightbackground=self.THEME["glass_light_border"], cursor="hand2"
        )
        self.btn_pwr_output.pack(side=tk.RIGHT)
        self.btn_pwr_output.bind("<Enter>", self._on_pwr_btn_enter)
        self.btn_pwr_output.bind("<Leave>", self._on_pwr_btn_leave)

        frame_steps_up = tk.Frame(card_pwr, bg=self.THEME["card_bg"])
        frame_steps_up.pack(fill=tk.X, pady=1)
        for i in range(4): frame_steps_up.columnconfigure(i, weight=1, uniform="pwr_steps")

        for i, step in enumerate(self.STEPS):
            btn = tk.Button(
                frame_steps_up, text=f"+{step:g}V" if step >= 1 else f"+{step:.2f}V",
                command=lambda s=step: self.step_pwr_voltage(s), width=6, pady=1
            )
            btn.grid(row=0, column=i, sticky="ew", padx=2)
            self._apply_liquid_glass_btn(btn, self.THEME["step_plus_bg"], self.THEME["step_plus_fg"], self.THEME["step_plus_hover"], self.THEME["step_plus_border"])

        frame_steps_down = tk.Frame(card_pwr, bg=self.THEME["card_bg"])
        frame_steps_down.pack(fill=tk.X, pady=1)
        for i in range(4): frame_steps_down.columnconfigure(i, weight=1, uniform="pwr_steps")

        for i, step in enumerate(self.STEPS):
            btn = tk.Button(
                frame_steps_down, text=f"-{step:g}V" if step >= 1 else f"-{step:.2f}V",
                command=lambda s=step: self.step_pwr_voltage(-s), width=6, pady=1
            )
            btn.grid(row=0, column=i, sticky="ew", padx=2)
            self._apply_liquid_glass_btn(btn, self.THEME["step_minus_bg"], self.THEME["step_minus_fg"], self.THEME["step_minus_hover"], self.THEME["step_minus_border"])

        pwr_bottom_grid = tk.Frame(card_pwr, bg=self.THEME["card_bg"])
        pwr_bottom_grid.pack(fill=tk.X, pady=(2, 0))
        for i in range(4): pwr_bottom_grid.columnconfigure(i, weight=1, uniform="pwr_bot")

        tk.Label(pwr_bottom_grid, text="DC调压(V):", font=self.FONT_BASE, fg=self.THEME["text_sub"], bg=self.THEME["card_bg"], anchor="center").grid(row=0, column=0, sticky="ew")
        tk.Label(pwr_bottom_grid, text="实际电压 VOUT", font=self.FONT_BASE, fg=self.THEME["text_sub"], bg=self.THEME["card_bg"], anchor="center").grid(row=0, column=1, sticky="ew")
        tk.Label(pwr_bottom_grid, text="限流(A):", font=self.FONT_BASE, fg=self.THEME["text_sub"], bg=self.THEME["card_bg"], anchor="center").grid(row=0, column=2, sticky="ew")
        tk.Label(pwr_bottom_grid, text="实际电流 IOUT", font=self.FONT_BASE, fg=self.THEME["text_sub"], bg=self.THEME["card_bg"], anchor="center").grid(row=0, column=3, sticky="ew")

        f_v_cell = tk.Frame(pwr_bottom_grid, bg=self.THEME["card_bg"])
        f_v_cell.grid(row=1, column=0, sticky="nsew")
        self.ent_pwr_v = tk.Entry(f_v_cell, font=self.FONT_BASE_BOLD, width=7, justify="center", bd=1, relief="solid", highlightthickness=0)
        self.ent_pwr_v.pack(expand=True, pady=1)
        self.ent_pwr_v.insert(0, "0.00")
        self.ent_pwr_v.bind("<Return>", lambda e: self.apply_pwr_v_setting(is_manual=True))

        self.lbl_pwr_v = tk.Label(pwr_bottom_grid, text="0.00 V", width=8, font=self.FONT_BASE_BOLD, fg=self.THEME["accent_blue"], bg=self.THEME["card_bg"], anchor="center")
        self.lbl_pwr_v.grid(row=1, column=1, sticky="nsew")

        f_i_cell = tk.Frame(pwr_bottom_grid, bg=self.THEME["card_bg"])
        f_i_cell.grid(row=1, column=2, sticky="nsew")
        self.ent_pwr_i = tk.Entry(f_i_cell, font=self.FONT_BASE_BOLD, width=7, justify="center", bd=1, relief="solid", highlightthickness=0)
        self.ent_pwr_i.pack(expand=True, pady=1)
        self.ent_pwr_i.insert(0, str(self.saved_cfg.get("pwr_iset", "0.500")))
        self.ent_pwr_i.bind("<Return>", lambda e: self.apply_pwr_i_setting())

        self.lbl_pwr_i = tk.Label(pwr_bottom_grid, text="0.000 A", width=8, font=self.FONT_BASE_BOLD, fg=self.THEME["accent_green"], bg=self.THEME["card_bg"], anchor="center")
        self.lbl_pwr_i.grid(row=1, column=3, sticky="nsew")

        card_vmin = self._create_card(main_container, pady=6, padx=10)
        card_vmin.pack(fill=tk.BOTH, expand=True)

        vmin_top_bar = tk.Frame(card_vmin, bg=self.THEME["card_bg"])
        vmin_top_bar.pack(fill=tk.X, pady=(0, 4))

        tk.Label(vmin_top_bar, text="Vmin 闭环寻优控制台", font=self.FONT_MEDIUM, fg=self.THEME["text_main"], bg=self.THEME["card_bg"]).pack(side=tk.LEFT)

        tk.Label(vmin_top_bar, text="单步测试时长(s):", font=self.FONT_BASE, fg=self.THEME["text_sub"], bg=self.THEME["card_bg"]).pack(side=tk.LEFT, padx=(6, 2))
        self.ent_step_delay = tk.Entry(vmin_top_bar, textvariable=self.var_step_delay, font=self.FONT_BASE_BOLD, width=4, justify="center")
        self.ent_step_delay.pack(side=tk.LEFT)

        tk.Label(vmin_top_bar, text="DC步进(s):", font=self.FONT_BASE, fg=self.THEME["text_sub"], bg=self.THEME["card_bg"]).pack(side=tk.LEFT, padx=(6, 2))
        self.ent_fast_delay = tk.Entry(vmin_top_bar, textvariable=self.var_fast_delay, font=self.FONT_BASE_BOLD, width=4, justify="center")
        self.ent_fast_delay.pack(side=tk.LEFT)

        tk.Label(vmin_top_bar, text="重启超时(s):", font=self.FONT_BASE, fg=self.THEME["text_sub"], bg=self.THEME["card_bg"]).pack(side=tk.LEFT, padx=(6, 2))
        self.ent_reboot_delay = tk.Entry(vmin_top_bar, textvariable=self.var_reboot_delay, font=self.FONT_BASE_BOLD, width=4, justify="center")
        self.ent_reboot_delay.pack(side=tk.LEFT)

        self.btn_start_test = tk.Button(vmin_top_bar, text="开始 Vmin 测试", command=self.start_vmin_test, width=14, padx=4, pady=1)
        self.btn_start_test.pack(side=tk.RIGHT, padx=(6, 0))
        self._apply_liquid_glass_btn(
            self.btn_start_test,
            bg=self.THEME["glass_green_bg"], fg="white",
            hover_bg=self.THEME["glass_green_hover"], border_color=self.THEME["glass_green_border"],
            disabled_bg="#065F46", disabled_fg="#A7F3D0"
        )

        self.btn_stop_test = tk.Button(vmin_top_bar, text="终止测试", command=self.stop_vmin_test, state="disabled", width=10, padx=4, pady=1)
        self.btn_stop_test.pack(side=tk.RIGHT)
        self._apply_liquid_glass_btn(
            self.btn_stop_test,
            bg=self.THEME["glass_red_bg"], fg="white",
            hover_bg=self.THEME["glass_red_hover"], border_color=self.THEME["glass_red_border"],
            disabled_bg="#881337", disabled_fg="#FECDD3"
        )

        vmin_excel_bar = tk.Frame(card_vmin, bg=self.THEME["card_bg"])
        vmin_excel_bar.pack(fill=tk.X, pady=(0, 4))

        tk.Label(vmin_excel_bar, text="Excel 测试文件:", font=self.FONT_BASE_BOLD, fg=self.THEME["text_main"], bg=self.THEME["card_bg"]).pack(side=tk.LEFT)
        self.ent_excel_file = tk.Entry(vmin_excel_bar, font=self.FONT_BASE, width=48)
        self.ent_excel_file.pack(side=tk.LEFT, padx=(4, 4), fill=tk.X, expand=True)
        self.ent_excel_file.insert(0, self.excel_path)

        btn_browse = tk.Button(vmin_excel_bar, text="浏览...", command=self.browse_excel_file, width=8, padx=4, pady=1)
        btn_browse.pack(side=tk.LEFT)
        self._apply_liquid_glass_btn(btn_browse, self.THEME["glass_light_bg"], self.THEME["text_main"], self.THEME["glass_light_hover"], self.THEME["glass_light_border"])

        result_box = tk.Frame(card_vmin, bg=self.THEME["bg"], padx=8, pady=2, highlightbackground=self.THEME["card_border"], highlightthickness=1)
        result_box.pack(fill=tk.X, pady=(0, 4))

        tk.Label(result_box, text="测试状态/结果:", font=self.FONT_MEDIUM, fg=self.THEME["text_main"], bg=self.THEME["bg"]).pack(side=tk.LEFT)
        
        self.lbl_tj_status = tk.Label(result_box, text="Tj: -- ℃", width=24, anchor="w", font=self.FONT_MEDIUM, fg=self.THEME["accent_amber"], bg=self.THEME["bg"])
        self.lbl_tj_status.pack(side=tk.LEFT, padx=(14, 0))

        self.lbl_vmin_result = tk.Label(result_box, text="-- V (就绪)", font=self.FONT_LARGE, fg=self.THEME["accent_red"], bg=self.THEME["bg"], width=28, anchor="e")
        self.lbl_vmin_result.pack(side=tk.RIGHT)

        self.txt_log = scrolledtext.ScrolledText(
            card_vmin, font=self.FONT_BASE, bg="#FFFFFF", fg="#0F172A",
            highlightthickness=1, highlightbackground=self.THEME["card_border"], bd=0, padx=6, pady=4
        )
        self.txt_log.pack(fill=tk.BOTH, expand=True)
        self.append_log("⚡ 智能系统已就绪 (RM1010 P8 14-Bit 高精 ADC 闭环控制)。")

    def create_serial_unit_ui(self, parent, col, title, prefix, default_baud="9600", show_baud=True):
        frame = tk.Frame(parent, bg=self.THEME["card_bg"])
        frame.grid(row=1, column=col, sticky="w", padx=(0, 10))

        tk.Label(frame, text=title, font=self.FONT_BASE_BOLD, fg=self.THEME["text_main"], bg=self.THEME["card_bg"]).pack(anchor="w", pady=(0, 2))

        row_ctrl = tk.Frame(frame, bg=self.THEME["card_bg"])
        row_ctrl.pack(anchor="w", pady=1)

        tk.Label(row_ctrl, text="串口", font=self.FONT_BASE, fg=self.THEME["text_sub"], bg=self.THEME["card_bg"]).pack(side=tk.LEFT, padx=(0, 2))
        cb_port = ttk.Combobox(row_ctrl, width=7 if show_baud else 8, state="readonly", font=self.FONT_BASE)
        cb_port.pack(side=tk.LEFT, padx=(0, 4))
        saved_p = self.saved_cfg.get(f"{prefix}_port", "")
        if saved_p:
            cb_port['values'] = [saved_p]
            cb_port.set(saved_p)
        cb_port.bind("<<ComboboxSelected>>", lambda e, p=prefix: self.on_smart_port_selected(p))
        cb_port.bind("<Button-1>", lambda e: self.smart_refresh_ports(startup=False))
        setattr(self, f"cb_{prefix}_port", cb_port)

        if show_baud:
            tk.Label(row_ctrl, text="波特率", font=self.FONT_BASE, fg=self.THEME["text_sub"], bg=self.THEME["card_bg"]).pack(side=tk.LEFT, padx=(0, 2))
            cb_baud = ttk.Combobox(row_ctrl, width=7, font=self.FONT_BASE)
            cb_baud['values'] = ["921600", "115200", "57600", "38400", "19200", "9600"]
            cb_baud.set(str(self.saved_cfg.get(f"{prefix}_baud", default_baud)))
            cb_baud.pack(side=tk.LEFT, padx=(0, 4))
            cb_baud.bind("<Return>", lambda e, p=prefix: self.on_smart_port_selected(p))
            setattr(self, f"cb_{prefix}_baud", cb_baud)
        else:
            setattr(self, f"{prefix}_baud", int(default_baud))

        btn_conn = tk.Button(row_ctrl, text="连接", width=6, padx=2, pady=1, command=lambda p=prefix: self.toggle_unit_connection(p))
        btn_conn.pack(side=tk.LEFT)
        self._apply_liquid_glass_btn(btn_conn, self.THEME["glass_blue_bg"], "white", self.THEME["glass_blue_hover"], self.THEME["glass_blue_border"])
        setattr(self, f"btn_{prefix}_conn", btn_conn)

    def _unit_label(self, prefix: str) -> str:
        return {
            "board": "板子调试串口",
            "pwr": "直流电源",
            "hg": "风枪控制串口",
        }.get(prefix, prefix)

    def smart_refresh_ports(self, startup=False):
        with self.lock:
            ports = list(self._cached_ports)
        display = ports if ports else ["无可用串口"]
        
        for prefix in ["board", "pwr", "hg"]:
            if getattr(self, f"{prefix}_active", False):
                cur_p = getattr(self, f"{prefix}_port", "")
                if cur_p and ports and cur_p not in ports:
                    with self.lock:
                        setattr(self, f"{prefix}_active", False)
                    self.append_log(f"⚠️ [USB热插拔感知] {self._unit_label(prefix)} 物理端口 ({cur_p}) 已拔出！")
                    if prefix == "hg":
                        self.heatgun.close()
                    btn = getattr(self, f"btn_{prefix}_conn", None)
                    if btn:
                        btn.config(text="连接")
                        self._apply_liquid_glass_btn(btn, self.THEME["glass_blue_bg"], "white", self.THEME["glass_blue_hover"], self.THEME["glass_blue_border"])

        for prefix in ["board", "pwr", "hg"]:
            cb = getattr(self, f"cb_{prefix}_port", None)
            if not cb: continue
            saved = self.saved_cfg.get(f"{prefix}_port", "")
            cb['values'] = display
            if cb.get() not in display and ports:
                cb.set(saved if saved in ports else display[0])

    def on_smart_port_selected(self, prefix: str):
        cb = getattr(self, f"cb_{prefix}_port", None)
        if not cb: return
        port = cb.get().strip()
        if port and port != "无可用串口":
            self.auto_connect_unit(prefix, port)

    def auto_connect_unit(self, prefix: str, port: str):
        if hasattr(self, f"cb_{prefix}_baud"):
            try: baud = int(getattr(self, f"cb_{prefix}_baud").get().strip())
            except ValueError: baud = 921600 if prefix in ("board", "hg") else 9600
        else:
            baud = 921600 if prefix == "hg" else getattr(self, f"{prefix}_baud", 9600)
        self.connect_unit(prefix, port, baud)

    def connect_unit(self, prefix: str, port: str, baud: int):
        if not port or port == "无可用串口": return

        for other in ["board", "pwr", "hg"]:
            if other != prefix and getattr(self, f"{other}_active", False):
                other_port = getattr(self, f"{other}_port", "").strip().upper()
                if other_port == port.strip().upper():
                    self.append_log(f"⚠️ [串口冲突拦截] 端口 {port} 已被【{self._unit_label(other)}】连接占用，无法重复打开！")
                    return

        with self.lock:
            setattr(self, f"{prefix}_port", port)
            setattr(self, f"{prefix}_baud", baud)
            setattr(self, f"{prefix}_active", True)
            if prefix == "pwr":
                self.pwr_output_state = True
                self.pwr_pending_output = True

        self.save_config(f"{prefix}_port", port)
        self.save_config(f"{prefix}_baud", baud)

        btn = getattr(self, f"btn_{prefix}_conn", None)
        if btn:
            btn.config(text="断开")
            self._apply_liquid_glass_btn(btn, self.THEME["glass_red_bg"], "white", self.THEME["glass_red_hover"], self.THEME["glass_red_border"])
        self.append_log(f"⚡ [串口连接] {self._unit_label(prefix)} 开启监听 -> {port} ({baud} Baud)")

        if prefix == "hg":
            if self.target_temp_mode != "常温":
                self.heatgun.power_on(source="CONNECT")
            else:
                self.heatgun.power_off(source="CONNECT")
            self._sync_heatgun_button_state()
        elif prefix == "pwr":
            self._sync_pwr_output_button_state()

    def disconnect_unit(self, prefix: str):
        with self.lock:
            setattr(self, f"{prefix}_active", False)

        if prefix == "hg":
            self.heatgun.close()

        btn = getattr(self, f"btn_{prefix}_conn", None)
        if btn:
            btn.config(text="连接")
            self._apply_liquid_glass_btn(btn, self.THEME["glass_blue_bg"], "white", self.THEME["glass_blue_hover"], self.THEME["glass_blue_border"])
        self.append_log(f"{self._unit_label(prefix)} 已手动断开。")
        if prefix == "hg":
            self._sync_heatgun_button_state()
        elif prefix == "pwr":
            self._sync_pwr_output_button_state()

    def toggle_unit_connection(self, prefix: str):
        if getattr(self, f"{prefix}_active", False):
            self.disconnect_unit(prefix)
        else:
            self.on_smart_port_selected(prefix)

    def board_serial_loop(self):
        while self.running:
            try:
                if not self.board_active:
                    time.sleep(0.02)
                    continue

                with self.lock:
                    port, baud = self.board_port, self.board_baud

                folder = os.path.join("Serial-COM", f"Serial-{port}")
                os.makedirs(folder, exist_ok=True)

                ser = None
                log_file = None
                log_path = ""
                chip_id_buf = ""
                pre_chip_buffer = []
                pre_chip_buffer_bytes = 0
                at_line_start = True

                def _sanitize_chip_id(chip_id: str) -> str:
                    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(chip_id))[:80] or "UNKNOWN"

                def _format_timestamped(text: str) -> str:
                    nonlocal at_line_start
                    if not text: return ""
                    now_str = datetime.now().strftime("%Y/%m/%d_%H:%M:%S ")
                    formatted_parts = []
                    for line in text.splitlines(keepends=True):
                        if at_line_start:
                            formatted_parts.append(now_str)
                        formatted_parts.append(line)
                        at_line_start = line.endswith("\n") or line.endswith("\r")
                    return "".join(formatted_parts)

                def _write_log(formatted_str: str):
                    if log_file and formatted_str:
                        log_file.write(formatted_str)
                        if "\n" in formatted_str or "\r" in formatted_str:
                            log_file.flush()

                def _open_chip_log(chip_id: str):
                    nonlocal log_file, log_path, pre_chip_buffer, pre_chip_buffer_bytes, at_line_start
                    safe_id = _sanitize_chip_id(chip_id)

                    existing_chip = self.board_log_chip_id
                    existing_path = self.board_log_path

                    if existing_chip == chip_id and existing_path and log_file and not log_file.closed:
                        return

                    if existing_chip == chip_id and existing_path:
                        new_path = existing_path
                    else:
                        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                        new_path = os.path.join(folder, f"{stamp}_{safe_id}.log")
                        if log_file and not log_file.closed:
                            try: log_file.close()
                            except Exception: pass
                        self.board_log_chip_id = chip_id
                        self.board_log_path = new_path

                    if log_file and not log_file.closed:
                        try: log_file.close()
                        except Exception: pass
                    log_path = new_path
                    log_file = open(log_path, "a", encoding="utf-8", errors="ignore")
                    at_line_start = True

                    if pre_chip_buffer:
                        for pending_text in pre_chip_buffer:
                            _write_log(pending_text)
                        pre_chip_buffer.clear()
                        pre_chip_buffer_bytes = 0

                    self.append_log(f"🧾 [Chip ID 日志绑定] {chip_id} -> {log_path}")

                try:
                    ser = serial.Serial(port=port, baudrate=baud, timeout=0.02, rtscts=False, dsrdtr=False)
                    try:
                        ser.dtr = False
                        ser.setRTS(False)
                    except Exception:
                        pass
                    ser.reset_input_buffer()
                    self.append_log(f"⚡ [板子串口已连接] 端口: {port} ({baud} Baud)")

                    while self.running and self.board_active:
                        try:
                            while True:
                                item = self.pending_board_cmds.get_nowait()
                                done_event = None
                                result = None
                                cmd = None
                                
                                if isinstance(item, tuple):
                                    cmd = item[0]
                                    done_event = item[1] if len(item) >= 2 else None
                                    result = item[2] if len(item) >= 3 else None
                                else:
                                    cmd = item

                                try:
                                    if cmd == "__VMIN_RESET_BOARD_RX__":
                                        ser.reset_input_buffer()
                                        if isinstance(result, dict):
                                            result["ok"] = True
                                    else:
                                        wire = b"\x03" if cmd == "\x03" else f"{cmd}\n".encode("utf-8")
                                        ser.write(wire)
                                        ser.flush()
                                        if isinstance(result, dict):
                                            result["ok"] = True
                                finally:
                                    if done_event is not None:
                                        done_event.set()
                        except queue.Empty:
                            pass

                        raw_data = ser.read(1024)
                        if not raw_data:
                            time.sleep(0.002)
                            continue

                        data = raw_data.decode("utf-8", errors="replace")
                        if not data:
                            continue

                        self.rt_watchdog.feed_text(data)
                        chip_id_buf += data
                        matches = list(CHIP_ID_REGEX.finditer(chip_id_buf))
                        detected_id = matches[-1].group(1) if matches else ""
                        if matches:
                            chip_id_buf = chip_id_buf[matches[-1].end():]
                        if len(chip_id_buf) > 512:
                            chip_id_buf = chip_id_buf[-256:]

                        if detected_id:
                            with self.lock:
                                self.detected_chip_id = detected_id
                                self.latest_chip_id = detected_id

                        self.tj_parser.feed_raw_chunk(data)

                        formatted_data = _format_timestamped(data)
                        if log_file is None:
                            pre_chip_buffer.append(formatted_data)
                            pre_chip_buffer_bytes += len(formatted_data.encode("utf-8", errors="ignore"))
                            while pre_chip_buffer and pre_chip_buffer_bytes > 256 * 1024:
                                dropped = pre_chip_buffer.pop(0)
                                pre_chip_buffer_bytes -= len(dropped.encode("utf-8", errors="ignore"))

                            if detected_id:
                                _open_chip_log(detected_id)
                        elif detected_id and detected_id != self.board_log_chip_id:
                            pre_chip_buffer = [formatted_data]
                            pre_chip_buffer_bytes = len(formatted_data.encode("utf-8", errors="ignore"))
                            _open_chip_log(detected_id)
                        else:
                            _write_log(formatted_data)

                        self.append_log_raw(formatted_data)

                except Exception as e:
                    with self.lock:
                        self.board_active = False
                    self.append_log(f"⚠️ [板子串口异常/拔出]: {e}")
                finally:
                    if log_file:
                        try: log_file.close()
                        except Exception: pass
                    if ser and ser.is_open:
                        try: ser.close()
                        except Exception: pass
                    time.sleep(0.1)

            except Exception as e:
                self.append_log(f"⚠️ [板子通信顶层异常]: {e}")
                time.sleep(0.2)

    def pwr_serial_loop(self):
        while self.running:
            try:
                if not self.pwr_active:
                    time.sleep(0.05)
                    continue

                with self.lock:
                    port, baud = self.pwr_port, self.pwr_baud

                ser = None
                consecutive_errors = 0

                try:
                    ser = serial.Serial(port=port, baudrate=baud, timeout=0.15, rtscts=False, dsrdtr=False)
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()

                    with self.lock:
                        init_v = self.pwr_target_v
                        init_i = self.pwr_target_i
                        self.pwr_output_state = True

                    ser.write(f"VSET1:{float(init_v):.2f}".encode("ascii"))
                    time.sleep(0.02)
                    ser.write(f"ISET1:{float(init_i):.3f}".encode("ascii"))
                    time.sleep(0.02)
                    ser.write(b"OUT1")
                    time.sleep(0.02)
                    self.append_log(f"⚡ [直流电源已连接] 端口: {port} | 默认供电输出已开启: OUT ON")

                    while self.running and self.pwr_active:
                        with self.lock:
                            vset, iset, out_cmd = self.pwr_pending_vset, self.pwr_pending_iset, self.pwr_pending_output
                            self.pwr_pending_vset = self.pwr_pending_iset = self.pwr_pending_output = None

                        if vset is not None:
                            ser.reset_input_buffer()
                            ser.write(f"VSET1:{float(vset):.2f}".encode("ascii"))
                            time.sleep(0.02)
                        if iset is not None:
                            ser.reset_input_buffer()
                            ser.write(f"ISET1:{float(iset):.3f}".encode("ascii"))
                            time.sleep(0.02)
                        if out_cmd is not None:
                            ser.reset_input_buffer()
                            ser.write(b"OUT1" if out_cmd else b"OUT0")
                            time.sleep(0.02)
                            with self.lock:
                                self.pwr_output_state = bool(out_cmd)

                        ser.write(b"IOUT1?"); time.sleep(0.01); i_b = ser.read(5)
                        ser.write(b"STATUS?"); time.sleep(0.01); s_b = ser.read(1)

                        if i_b or s_b:
                            consecutive_errors = 0
                            with self.lock:
                                self.pwr_measured_v = self.pwr_target_v
                                if i_b:
                                    try:
                                        i_parsed = float(i_b.decode("ascii", errors="ignore").strip())
                                        if i_parsed >= 0.0: self.pwr_measured_i = i_parsed
                                    except ValueError: pass
                        else:
                            consecutive_errors += 1

                        if consecutive_errors >= 8:
                            self.append_log("⚠️ [直流电源] 连续 8 次无响应，判定掉线。")
                            with self.lock: self.pwr_active = False
                            break

                        time.sleep(0.03)

                except Exception as e:
                    with self.lock: self.pwr_active = False
                    self.append_log(f"⚠️ [直流电源串口异常/拔出]: {e}")
                finally:
                    if ser and ser.is_open:
                        try: ser.close()
                        except Exception: pass
                    time.sleep(0.1)

            except Exception as e:
                self.append_log(f"⚠️ [直流电源通信顶层异常]: {e}")
                time.sleep(0.2)

    def hg_serial_loop(self):
        while self.running:
            try:
                if not self.hg_active:
                    time.sleep(0.05)
                    continue

                with self.lock:
                    port, baud = self.hg_port, self.hg_baud

                if not self.heatgun.ser or not getattr(self.heatgun.ser, "is_open", False):
                    ok = self.heatgun.open(port, baudrate=baud)
                    if not ok:
                        with self.lock: self.hg_active = False
                        self.append_log(f"⚠️ [风枪串口打开失败]: {port}")
                        time.sleep(0.5)
                        continue
                    else:
                        self.append_log(f"⚡ [风枪串口已连接] 端口: {port} ({baud} Baud)")
                        if self.target_temp_mode != "常温":
                            self.heatgun.power_on(source="AUTO")
                        else:
                            self.heatgun.power_off(source="AUTO")
                        self._sync_heatgun_button_state()

                time.sleep(0.2)
            except Exception as e:
                self.append_log(f"⚠️ [风枪串口通信异常]: {e}")
                time.sleep(0.5)

    def _set_pwr_voltage_widgets(self, v_str: str):
        self.ent_pwr_v.delete(0, tk.END)
        self.ent_pwr_v.insert(0, v_str)
        self.lbl_pwr_v.config(text=f"{v_str} V")

    def _set_pwr_current_widgets(self, i_str: str):
        self.ent_pwr_i.delete(0, tk.END)
        self.ent_pwr_i.insert(0, i_str)

    def _set_test_buttons_idle(self):
        self.btn_start_test.config(state="normal")
        self.btn_stop_test.config(state="disabled")

    def apply_pwr_v_setting(self, val: float = None, is_manual: bool = False):
        try:
            if val is None:
                if self._is_ui_thread():
                    val = float(self.ent_pwr_v.get())
                else:
                    with self.lock: val = self.pwr_target_v
                is_manual = True
            val = max(self.MIN_VOLTAGE, min(self.MAX_VOLTAGE, round(val, 2)))
            v_str = f"{val:.2f}"
            self._ui_call(self._set_pwr_voltage_widgets, v_str)
            with self.lock:
                self.pwr_target_v = self.pwr_measured_v = val
                self.pwr_pending_vset = v_str
                if is_manual:
                    self.voltage_preempted_event.set()
        except ValueError: pass

    def step_pwr_voltage(self, delta: float):
        try: curr = float(self.ent_pwr_v.get())
        except ValueError: curr = self.pwr_target_v
        self.apply_pwr_v_setting(round(curr + delta, 2), is_manual=True)

    def apply_pwr_i_setting(self):
        try:
            if self._is_ui_thread():
                raw_val = float(self.ent_pwr_i.get())
            else:
                with self.lock: raw_val = self.pwr_target_i
            val = max(self.MIN_CURRENT, min(self.MAX_CURRENT, round(raw_val, 3)))
            i_str = f"{val:.3f}"
            self._ui_call(self._set_pwr_current_widgets, i_str)
            with self.lock:
                self.pwr_target_i = val
                self.pwr_pending_iset = i_str
            self.save_config("pwr_iset", i_str)
        except ValueError: pass

    def _stop_thermal_and_flush_cmds(self):
        self.thermal_mgr.stop_all()
        while True:
            try:
                item = self.pending_board_cmds.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple) and len(item) >= 2:
                try:
                    if len(item) >= 3 and isinstance(item[2], dict):
                        item[2]["ok"] = False
                    item[1].set()
                except Exception:
                    pass

    def send_board_cmd(self, cmd: str) -> bool:
        if self.board_active:
            self.pending_board_cmds.put(cmd)
            return True
        return False

    def send_board_cmd_sync(self, cmd: str, timeout: float = 4.0) -> bool:
        if not self.board_active:
            return False
        done_event = threading.Event()
        result = {"ok": False}
        self.pending_board_cmds.put((cmd, done_event, result))
        if not done_event.wait(timeout=max(0.5, timeout)):
            self.append_log(f"⚠️ [UART写入超时] {cmd!r} 在 {timeout:.1f}s 内未完成写入。")
            return False
        return bool(result["ok"])

    def send_board_soft_reboot(self, timeout: float = 4.0) -> bool:
        if not self.board_active:
            return False
        self.send_board_cmd_sync("\x03", timeout=1.0)
        time.sleep(0.08)
        return self.send_board_cmd_sync("reboot", timeout=timeout)

    def get_current_measured_voltage(self) -> float:
        with self.rm1010.lock:
            return self.rm1010.voltage_val or 0.0

    def execute_smart_step_watchdog(self, fixed_delay: float = None) -> tuple[bool, str]:
        step_start = time.monotonic()
        target_delay = fixed_delay if fixed_delay is not None else self.get_step_delay()

        while self.test_running:
            if not self.board_active:
                self._stop_thermal_and_flush_cmds()
                return True, "板子串口连接断开"

            if self.voltage_preempted_event.is_set():
                self.voltage_preempted_event.clear()
                return False, "PREEMPTED_BY_USER"

            if self.thermal_mgr.emergency_shutdown.is_set():
                self._stop_thermal_and_flush_cmds()
                return True, self.thermal_mgr.emergency_reason

            if self.rt_watchdog.fail_detected:
                self._stop_thermal_and_flush_cmds()
                return True, self.rt_watchdog.fail_reason

            remaining = target_delay - (time.monotonic() - step_start)
            if remaining <= 0:
                break

            if self.rt_watchdog.interrupt_event.wait(timeout=min(0.05, remaining)):
                if self.rt_watchdog.fail_detected:
                    self._stop_thermal_and_flush_cmds()
                    return True, self.rt_watchdog.fail_reason
                if not self.test_running:
                    return False, ""

        if self.rt_watchdog.fail_detected:
            self._stop_thermal_and_flush_cmds()
            return True, self.rt_watchdog.fail_reason

        if self.rt_watchdog.has_hb_rule:
            is_timeout, reason = self.rt_watchdog.force_check()
            if is_timeout:
                self._stop_thermal_and_flush_cmds()
                return True, reason

        return False, ""

    def ramp_dc_until_dmm_drops_to(self, target_dmm_v: float) -> tuple[bool, str]:
        while self.test_running:
            if self.rt_watchdog.fail_detected:
                return True, self.rt_watchdog.fail_reason

            curr_v = self.get_current_measured_voltage()

            if curr_v > 0.0 and curr_v <= (target_dmm_v + 0.0005):
                if self.rt_watchdog.fail_detected:
                    return True, self.rt_watchdog.fail_reason
                return False, ""

            next_dc = round(self.pwr_target_v + 0.01, 2)
            if next_dc > self.MAX_VOLTAGE:
                return True, f"DC 调压达到硬件保护上限 {self.MAX_VOLTAGE:.2f}V"

            self.apply_pwr_v_setting(next_dc, is_manual=False)

            fast_delay = self.get_fast_ramp_delay()
            is_failed, fail_reason = self.execute_smart_step_watchdog(fixed_delay=fast_delay)
            if fail_reason == "PREEMPTED_BY_USER":
                continue
            if is_failed:
                return True, fail_reason

        if self.rt_watchdog.fail_detected:
            return True, self.rt_watchdog.fail_reason

        return False, ""

    def _auto_open_excel(self, file_path: str):
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            self.append_log(f"⚠️ [Excel打开提示] 报表文件未找到: {abs_path}")
            return
        self.append_log(f"📊 正在自动打开 Excel 测试报表: {abs_path} ...")
        try:
            if sys.platform == "win32":
                try:
                    os.startfile(abs_path)
                except Exception:
                    subprocess.Popen(f'explorer "{abs_path}"', shell=True)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", abs_path])
            else:
                subprocess.Popen(["xdg-open", abs_path])
        except Exception as e:
            self.append_log(f"⚠️ [Excel打开异常] 无法自动打开 Excel ({e})，请手动打开: {abs_path}")

    def _send_test_reboot(self, reason: str) -> float:
        with self._test_reboot_lock:
            reboot_mono = time.monotonic()
            next_no = self._test_reboot_count + 1
            self.append_log(f">>> [测试流程 reboot#{next_no}] {reason} -> 立即向板子串口下发 reboot 命令...")
            if self.board_active:
                ok = self.send_board_soft_reboot()
                if ok:
                    self._test_reboot_count = next_no
                    return reboot_mono
                self.append_log("❌ 板子串口 reboot 命令下发失败！")
                return 0.0
            self.append_log("❌ 板子串口未连接，无法发送 reboot 命令！")
            return 0.0

    def _finalize_device_safe_state(self, is_reboot_fail: bool):
        with self._test_reboot_lock:
            if self._test_finalized:
                return
            self._test_finalized = True

        self._stop_thermal_and_flush_cmds()
        self.rt_watchdog.stop_monitor()
        if self.target_temp_mode != "常温":
            self.reset_heatgun_to_80("测试结束/安全收尾")
        self.apply_pwr_v_setting(0.00, is_manual=False)
        time.sleep(0.05)

        if is_reboot_fail:
            self.append_log(">>> [智能收尾] 检测到板卡已硬件掉电/异常重启，DC调压归零 0.00V。")
        else:
            self._send_test_reboot("Case 测试结束（最终安全复位）")

    def _prepare_case_environment(
        self,
        sheet,
        r: int,
        case_name: str,
        cmd_text: str,
        log_prefix: str = "",
        reuse_reboot_ts: float = 0.0,
        learn_boot_from_pre_scan: bool = False,
    ) -> tuple[bool, str, float]:
        self._stop_thermal_and_flush_cmds()
        thermal_enabled = (self.target_temp_mode != "常温")
        if thermal_enabled:
            self.reset_heatgun_to_80("用例准备前置")
        else:
            self.heatgun.power_off(source="NORMAL_MODE_CASE_PREP")

        self.apply_pwr_v_setting(0.00, is_manual=False)
        self.apply_pwr_i_setting()
        with self.lock:
            self.pwr_pending_output = True
            self.detected_chip_id = ""
        self.tj_parser.reset()

        if thermal_enabled:
            tj, _ = self.tj_parser.get_latest_tj(max_age_sec=5.0)
            if tj is not None and tj > 83.0:
                self.append_log(f"\n>>> {log_prefix} [高温保护] 当前 Tj={tj:.1f}℃ 偏高，快速归底 80℃ 冷却...")
                self.reset_heatgun_to_80("超温冷却")
                self.append_log("    ⏳ 等待芯片温度回落至安全复位区间 (≤82.0℃)...")

                cooldown_start = time.time()
                while self.test_running:
                    tj, _ = self.tj_parser.get_latest_tj(max_age_sec=5.0)
                    if tj is not None and tj <= 82.0:
                        self.append_log(f"    ⚡ [温度已安全回落] 当前 Tj={tj:.1f}℃，正式开始重启板卡！")
                        break
                    if time.time() - cooldown_start > 45.0:
                        self.append_log("    ⚠️ [降温超时保底] 快速归底完成，直接开始开机流程...")
                        break
                    time.sleep(0.5)

        if not self.test_running:
            if thermal_enabled:
                self.reset_heatgun_to_80("终止测试")
            return False, "", 0.0

        configured_delay = self.get_reboot_delay()
        self.boot_mgr.reset_session(configured_delay)

        if reuse_reboot_ts > 0.0:
            reboot_mono = reuse_reboot_ts
            target_boot_delay = self.boot_mgr.get_current_boot_delay()
            elapsed = time.monotonic() - reboot_mono
            remaining = max(0.0, target_boot_delay - elapsed)
            self.append_log(
                f"\n>>> {log_prefix} [复用预扫描结束 reboot] 不再重复重启，"
                f"等待自适应开机时间 {target_boot_delay:.1f}s "
                f"(已等待 {elapsed:.1f}s，剩余 {remaining:.1f}s)..."
            )
        else:
            self.append_log(f"\n>>> {log_prefix} 板端下发 reboot 命令重启板卡 (DC=0.00V)...")
            reboot_mono = self._send_test_reboot(f"{log_prefix} 测试阶段开始")
            if reboot_mono <= 0:
                return False, "", 0.0
            target_boot_delay = self.boot_mgr.get_current_boot_delay()
            self.append_log(
                f">>> DC 调压已归零 0.00V，等待开机 "
                f"(目标 {target_boot_delay:.1f}s)..."
            )

        while self.test_running:
            elapsed = time.monotonic() - reboot_mono
            if elapsed >= target_boot_delay:
                break
            time.sleep(min(0.1, max(0.02, target_boot_delay - elapsed)))

        if not self.test_running:
            return False, "", 0.0

        with self.lock:
            chip_id = self.get_best_chip_id()
            boot_v = self.get_current_measured_voltage()

        if chip_id:
            self.append_log(f"    [Chip ID 识别成功]: {chip_id}")
            sheet.cell(row=r, column=1).value = chip_id

        self.append_log(f"    [开机基准核心电压]: RM1010 P8实测 = {boot_v:.4f} V (DC=0.00V)")
        self.rt_watchdog.reset_and_configure(f"{case_name} {cmd_text}")

        fixed_kernel_cmds = self.FIXED_KERNEL_CMDS

        self.append_log("    [阶段 1/2] 开始下发固定 Kernel 初始化命令...")
        second_fixed_cmd_send_mono = 0.0
        for index, cmd in enumerate(fixed_kernel_cmds, 1):
            if not self.test_running:
                return False, "", 0.0

            if learn_boot_from_pre_scan and index == 2:
                second_fixed_cmd_send_mono = time.monotonic()

            self.append_log(f"    [{index}/{len(fixed_kernel_cmds)}] {cmd}")
            ok = self.send_board_cmd_sync(cmd, timeout=4.0)
            if not ok:
                self.append_log(f"❌ [固定命令发送失败] {cmd}")
                return False, "", 0.0
            time.sleep(0.60 if cmd == "su" else 0.35)

        if learn_boot_from_pre_scan and second_fixed_cmd_send_mono > 0.0:
            self.boot_mgr.record_pre_scan_boot(
                reboot_mono,
                second_fixed_cmd_send_mono,
                log_func=self.append_log,
            )

        self.append_log("    ✅ [阶段 1/2] 所有固定 Kernel 命令已成功写入板端。")

        case_cmds = [c.strip() for c in cmd_text.splitlines() if c.strip()]
        self.append_log("    [阶段 2/2] 开始下发 Case 启动命令...")
        for index, cmd in enumerate(case_cmds, 1):
            if not self.test_running:
                return False, "", 0.0
            self.append_log(f"    [Case {index}/{len(case_cmds)}] {cmd}")
            ok = self.send_board_cmd_sync(cmd, timeout=4.0)
            if not ok:
                self.append_log(f"❌ [Case命令发送失败] {cmd}")
                return False, "", 0.0
            time.sleep(0.35)

        self.append_log("    ✅ [阶段 2/2] Case 命令已成功发送并启动。")

        if not self.test_running:
            return False, "", 0.0

        self.rt_watchdog.start_monitoring()
        if self.rt_watchdog.has_hb_rule:
            self.append_log(
                f"    ⚡ [独立心跳看门狗生效] 连续 {self.rt_watchdog.HEARTBEAT_TIMEOUT_SEC:.0f}s 无心跳即 Fail (Case命令已发完，监测已启动)"
            )

        self.thermal_mgr.start_probe_only()
        self.append_log("    ⚡ [Tj记录探针] 持续采样读取 Tj，用于记录 Vmin 温度...")

        if thermal_enabled:
            is_high, parsed_val = self._parse_temp_mode(self.target_temp_mode)
            target_temp_val = parsed_val if (is_high and parsed_val is not None) else 85.0

            self.append_log(f"\n⚡ [高温温控启动] 目标温区: [{target_temp_val:.0f}℃ ~ {target_temp_val + 1.0:.0f}℃]...")
            self.thermal_mgr.start_thermal_control(target_temp_val)

            temp_ok = self.thermal_mgr.wait_for_thermal_convergence(
                target_temp_val,
                check_stop_func=lambda: (
                    not self.test_running or self.rt_watchdog.fail_detected
                ),
                timeout_sec=300.0,
            )

            if self.rt_watchdog.fail_detected:
                self.append_log(f"    ❌ [Case 看门狗] {self.rt_watchdog.fail_reason}")
                self.reset_heatgun_to_80("CaseFail/心跳超时")
                return False, "", 0.0

            if not temp_ok or not self.test_running:
                self.reset_heatgun_to_80("终止测试/温控未就绪")
                return False, "", 0.0

        return True, chip_id, boot_v

    def _handle_case_fail_and_reboot(self, reason: str, is_hardware_reboot: bool = False) -> float:
        self.append_log(f"❌ [Case立即Fail] {reason}")
        self.voltage_preempted_event.clear()
        self._stop_thermal_and_flush_cmds()
        self.rt_watchdog.stop_monitor()
        self.apply_pwr_v_setting(0.00, is_manual=False)
        time.sleep(0.05)

        if is_hardware_reboot:
            self.append_log(">>> [异常重启已发生] 检测到板卡已硬件掉电/异常重启，DC调压归零，等待板端重启恢复。")
            return time.monotonic()
        else:
            return self._send_test_reboot(f"Case Fail ({reason}) -> 立即下发 reboot 命令复位板卡")

    def get_best_chip_id(self) -> str:
        """多级冗余探测获取最新有效 Chip ID:
        L1: 内存持久变量 self.latest_chip_id / self.detected_chip_id / self.board_log_chip_id
        L2: 当前绑定日志文件 (self.board_log_path) 逆向尾部扫描
        L3: 历史串口日志目录最新文件逆向扫描
        """
        with self.lock:
            if self.latest_chip_id:
                return self.latest_chip_id
            if self.detected_chip_id:
                self.latest_chip_id = self.detected_chip_id
                return self.latest_chip_id
            if self.board_log_chip_id and self.board_log_chip_id != "UNKNOWN":
                self.latest_chip_id = self.board_log_chip_id
                return self.latest_chip_id

        # L2: 当前活跃日志文件尾部扫描 (快速定位最新 ID)
        log_path = self.board_log_path
        if log_path and os.path.isfile(log_path):
            try:
                with open(log_path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 128 * 1024))
                    chunk = f.read().decode("utf-8", errors="ignore")
                    matches = CHIP_ID_REGEX.findall(chunk)
                    if matches:
                        with self.lock:
                            self.latest_chip_id = matches[-1]
                        return matches[-1]
            except Exception:
                pass

        # L3: 串口日志目录扫描最近文件
        try:
            port = self.board_port or self.saved_cfg.get("board_port", "")
            if port:
                folder = os.path.join("Serial-COM", f"Serial-{port}")
                if os.path.isdir(folder):
                    files = [
                        os.path.join(folder, fn)
                        for fn in os.listdir(folder)
                        if fn.endswith(".log")
                    ]
                    if files:
                        files.sort(key=os.path.getmtime, reverse=True)
                        for fpath in files[:3]:
                            with open(fpath, "rb") as f:
                                f.seek(0, os.SEEK_END)
                                size = f.tell()
                                f.seek(max(0, size - 64 * 1024))
                                chunk = f.read().decode("utf-8", errors="ignore")
                                matches = CHIP_ID_REGEX.findall(chunk)
                                if matches:
                                    with self.lock:
                                        self.latest_chip_id = matches[-1]
                                    return matches[-1]
        except Exception:
            pass

        return ""

    def sync_chip_id_to_excel(self, wb, sheet, case_rows: list, excel_path: str = None) -> str:
        """将最新 Chip ID 完整同步/回填至所有有效用例行"""
        best_id = self.get_best_chip_id()
        if not best_id:
            return ""

        changed = False
        for r in case_rows:
            curr = str(sheet.cell(row=r, column=1).value or "").strip()
            if curr != best_id:
                sheet.cell(row=r, column=1).value = best_id
                changed = True

        if changed and excel_path and wb:
            for _ in range(3):
                try:
                    wb.save(excel_path)
                    break
                except Exception:
                    time.sleep(0.3)
        return best_id

    def _record_vmin_result(self, r: int, freq: float, vmin: float, fail_reason: str, stage_tag: str, 
                            duration_sec: float, wb, excel_path: str, summary_records: list):
        self._stop_thermal_and_flush_cmds()
        if self.target_temp_mode != "常温":
            self.reset_heatgun_to_80(f"Row {r} 完成")
        self.apply_pwr_v_setting(0.00, is_manual=False)

        tj_val, _ = self.tj_parser.get_latest_tj()
        tj_record = round(tj_val, 1) if tj_val is not None else "--"
        dur_str = format_duration(duration_sec)

        ts_str = datetime.now().strftime("[%H:%M:%S]")
        fail_line = f"{ts_str} ! [{stage_tag} Fail] 原因: {fail_reason}"
        result_line = f"{ts_str} ★ Row {r} 完成, 最低稳定 Vmin = {vmin:.4f} V (P8实测) | Tj: {tj_record}℃ | 耗时: {dur_str}"
        summary_records.append((fail_line, result_line))
        self.append_log_raw(fail_line)
        self.append_log_raw(result_line)

        sheet = wb.active
        best_id = self.get_best_chip_id()
        if best_id:
            sheet.cell(row=r, column=1).value = best_id
        sheet.cell(row=r, column=4).value = round(vmin, 4)
        sheet.cell(row=r, column=6).value = tj_record
        sheet.cell(row=r, column=7).value = str(fail_reason)
        sheet.cell(row=r, column=8).value = dur_str

        for _ in range(3):
            try: wb.save(excel_path); break
            except Exception: time.sleep(0.4)

        self._ui_call(lambda: self.lbl_vmin_result.config(text=f"Row {r} ({freq}MHz): {vmin:.4f} V", fg=self.THEME["accent_green"]))

    def start_vmin_test(self):
        if not (self.board_active and self.pwr_active):
            self.append_log("[警告] 无法启动！请确保板子调试串口与直流电源均已连接。")
            return
        
        if not self.rm1010.connected:
            self.append_log("[警告] 无法启动！未检测到 RM1010 硬件 (电压采集与温控核心)！")
            return

        target_mode_str = self.cb_target_temp.get().strip()
        is_high_mode, parsed_temp = self._parse_temp_mode(target_mode_str)
        target_mode_str = f"{parsed_temp:g}℃" if is_high_mode else "常温"
        if self.test_running: return

        excel_path = self.ent_excel_file.get().strip()
        if not os.path.exists(excel_path):
            self.append_log(f"[错误] Excel 文件不存在: '{excel_path}'")
            return

        self.save_config("step_delay", self.get_step_delay())
        self.save_config("fast_delay", self.get_fast_ramp_delay())
        self.save_config("reboot_delay", self.get_reboot_delay())
        self.save_config("excel_file", excel_path)
        self.save_config("target_temp", target_mode_str)
        self.excel_path = excel_path
        self.target_temp_mode = target_mode_str
        self.apply_pwr_i_setting()

        self.test_running = True
        with self._test_reboot_lock:
            self._test_reboot_count = 0
            self._test_finalized = False
            self._formal_case1_reboot_mono = 0.0
        self.btn_start_test.config(state="disabled")
        self.btn_stop_test.config(state="normal")
        self.lbl_vmin_result.config(text="测试中...", fg=self.THEME["accent_blue"])

        self.test_thread = threading.Thread(target=self.run_vmin_test_sequence, daemon=True)
        self.test_thread.start()

    def stop_vmin_test(self):
        if self.test_running:
            self.test_running = False
            self._stop_thermal_and_flush_cmds()
            self.append_log("[提示] 用户终止了测试。")
            self._finalize_device_safe_state(self.rt_watchdog.is_reboot_fail)
            self.btn_start_test.config(state="normal")
            self.btn_stop_test.config(state="disabled")

    @staticmethod
    def is_thermal_fail(reason: str) -> bool:
        if not reason: return False
        return any(k in reason for k in ["安全熔断", "超温", "温度超温", "Tj 温度", "OVERHEAT", "保护上限"])

    def run_vmin_test_sequence(self):
        excel_path = self.excel_path
        try:
            wb = openpyxl.load_workbook(excel_path)
            sheet = wb.active
        except Exception as e:
            self.append_log(f"[错误] 打开 Excel 失败: {e}")
            self.test_running = False
            self._ui_call(self._set_test_buttons_idle)
            return

        if sheet.cell(row=1, column=6).value is None:
            sheet.cell(row=1, column=6).value = "Vmin Tj(℃)"
        if sheet.cell(row=1, column=7).value is None:
            sheet.cell(row=1, column=7).value = "Fail原因"
        if sheet.cell(row=1, column=8).value is None:
            sheet.cell(row=1, column=8).value = "测试耗时"

        raw_cases = []
        for r in range(2, sheet.max_row + 1):
            case_name = str(sheet.cell(row=r, column=2).value or "").strip()
            freq_val = sheet.cell(row=r, column=3).value
            cmd_text = str(sheet.cell(row=r, column=5).value or "").strip()
            if case_name or cmd_text:
                try: freq_num = float(freq_val) if freq_val is not None else 0.0
                except (ValueError, TypeError): freq_num = 0.0
                raw_cases.append((r, freq_num, case_name, cmd_text))

        if not raw_cases:
            self.append_log("[错误] 未检索到有效测试用例！")
            self.test_running = False
            self._ui_call(self._set_test_buttons_idle)
            return

        test_cases = sorted(raw_cases, key=lambda x: x[1], reverse=True)
        max_test_row = max(c[0] for c in test_cases)

        self.rt_watchdog.reset_and_configure("")
        with self.lock:
            self.rt_watchdog.is_reboot_fail = False
            self.rt_watchdog.last_reboot_time = 0.0
            self.rt_watchdog.fail_detected = False

        self.append_log("==========================================================")
        self.append_log(f"★ 启动 Vmin 闭环寻优 (共 {len(test_cases)} 组用例，按频率降序执行 | 模式: {self.target_temp_mode})")
        self.append_log("==========================================================")

        test_start_time = time.time()
        summary_records = []

        # ==================== Case 1 预扫描粗测探底阶段 ====================
        c1_row, c1_freq, c1_name, c1_cmd = test_cases[0]

        self.append_log(f"\n⚡ ==========================================================")
        self.append_log(f"⚡ >>> [阶段 1: Case 1 预扫描探底] Row {c1_row} | 频率: {c1_freq} MHz | {c1_name}")
        self.append_log(f"⚡ ==========================================================")

        pre_scan_fail_v = None
        pre_scan_dc0_v = None
        if self.test_running:
            ok, _, boot_v = self._prepare_case_environment(
                sheet,
                c1_row,
                c1_name,
                c1_cmd,
                log_prefix="[Case 1 预扫描]",
                learn_boot_from_pre_scan=True,
            )
            if not ok:
                self._finalize_device_safe_state(self.rt_watchdog.is_reboot_fail)
                self.test_running = False
                self._ui_call(self._set_test_buttons_idle)
                return

            pre_scan_dc0_v = self.get_current_measured_voltage() or boot_v
            self.append_log("---> [Case 1 预扫描] DC 从 0.00V 开始持续递增加压快速探底...")
            while self.test_running:
                curr_v = self.get_current_measured_voltage()
                curr_dc = self.pwr_target_v

                next_dc = round(curr_dc + 0.01, 2)
                self.apply_pwr_v_setting(next_dc, is_manual=False)

                fast_delay = self.get_fast_ramp_delay()
                is_failed, fail_reason = self.execute_smart_step_watchdog(fixed_delay=fast_delay)
                if fail_reason == "PREEMPTED_BY_USER":
                    continue

                if is_failed:
                    if self.is_thermal_fail(fail_reason):
                        self.append_log(
                            f"🚨 [预扫描超温] {fail_reason}，"
                            f"终止本轮测试。"
                        )
                        self._finalize_device_safe_state(self.rt_watchdog.is_reboot_fail)
                        self.test_running = False
                        self._ui_call(self._set_test_buttons_idle)
                        return

                    pre_scan_fail_v = self.get_current_measured_voltage() or curr_v
                    self.append_log(
                        f"⚡ [预扫描完成] 粗测 Fail 临界 = "
                        f"{pre_scan_fail_v:.4f} V (原因: {fail_reason})"
                    )
                    break

            if pre_scan_fail_v is None:
                self.append_log("❌ [预扫描失败] 未得到有效 Fail 电压，不进入正式测试。")
                self._finalize_device_safe_state(self.rt_watchdog.is_reboot_fail)
                self.test_running = False
                self._ui_call(self._set_test_buttons_idle)
                return

            self._stop_thermal_and_flush_cmds()
            if self.target_temp_mode != "常温":
                self.reset_heatgun_to_80("预扫描完成")
            self.apply_pwr_v_setting(0.00, is_manual=False)

            self._formal_case1_reboot_mono = self._send_test_reboot("预扫描结束，准备 Case1 正式测试")
            if self._formal_case1_reboot_mono <= 0:
                self._finalize_device_safe_state(self.rt_watchdog.is_reboot_fail)
                self.test_running = False
                self._ui_call(self._set_test_buttons_idle)
                return

        raw_c1_target = round(pre_scan_fail_v + 0.050, 4)
        self.append_log(f"\n★ 预扫描完成: 粗测 Fail = {pre_scan_fail_v:.4f} V")
        if pre_scan_dc0_v is not None and pre_scan_dc0_v > 0.0 and raw_c1_target > pre_scan_dc0_v:
            case1_fast_target_v = round(pre_scan_dc0_v, 4)
            self.append_log(
                f"★ Case 1 正式精测逼近目标 = 粗测Fail+0.050V ({raw_c1_target:.4f}V) 超过 DC OUT=0V 实时电压 ({pre_scan_dc0_v:.4f}V)，"
                f"智能钳位限制为 -> {case1_fast_target_v:.4f} V\n"
            )
        else:
            case1_fast_target_v = raw_c1_target
            self.append_log(f"★ Case 1 正式精测逼近目标 = 回退 +0.050V -> {case1_fast_target_v:.4f} V\n")

        # ==================== 正式测试精测主循环 ====================
        last_case_passed_vmin = None
        last_case_reboot_mono = 0.0

        try:
            for idx, (r, freq, case_name, cmd_text) in enumerate(test_cases):
                if not self.test_running: break

                fast_ramp_target_v = case1_fast_target_v if idx == 0 else round((last_case_passed_vmin if last_case_passed_vmin is not None else case1_fast_target_v) + 0.050, 4)

                self.append_log("==========================================================")
                self.append_log(f">>> [用例 {idx+1}/{len(test_cases)} 正式测试] Row {r} | 频率: {freq} MHz | {case_name}")
                self.append_log(f"    [快速逼近目标]: RM1010 P8 实测电压 -> {fast_ramp_target_v:.4f} V")
                self.append_log("==========================================================")

                case_completed = False
                while not case_completed and self.test_running:
                    case_start_time = time.time()

                    reuse_reboot_ts = self._formal_case1_reboot_mono if idx == 0 else last_case_reboot_mono
                    ok, chip_id, boot_v = self._prepare_case_environment(
                        sheet,
                        r,
                        case_name,
                        cmd_text,
                        log_prefix=f"[Row {r} 正式测试]",
                        reuse_reboot_ts=reuse_reboot_ts,
                    )
                    if not ok:
                        if self.test_running:
                            prep_reason = self.rt_watchdog.fail_reason or "环境准备阶段失败"
                            self.append_log(f"❌ [Row {r} 环境准备失败] {prep_reason}")
                            self._record_vmin_result(r, freq, boot_v or 0.0, prep_reason, "用例准备", time.time() - case_start_time, wb, excel_path, summary_records)
                            last_case_reboot_mono = self._handle_case_fail_and_reboot(prep_reason, is_hardware_reboot=self.rt_watchdog.is_reboot_fail)
                            case_completed = True
                        break

                    case_dc0_v = self.get_current_measured_voltage() or boot_v
                    if case_dc0_v > 0.0 and fast_ramp_target_v > case_dc0_v:
                        self.append_log(
                            f"    ⚠️ [目标钳位] 逼近目标 {fast_ramp_target_v:.4f}V 超过当前 DC OUT=0V 实时电压 ({case_dc0_v:.4f}V)，"
                            f"智能钳位为 -> {case_dc0_v:.4f} V"
                        )
                        fast_ramp_target_v = round(case_dc0_v, 4)

                    # 阶段 1：快速逼近
                    self.append_log(f"---> [Row {r}] DC 0.00V 递增加压逼近目标 {fast_ramp_target_v:.4f} V...")
                    is_failed, fail_reason = self.ramp_dc_until_dmm_drops_to(fast_ramp_target_v)

                    if is_failed:
                        if self.is_thermal_fail(fail_reason):
                            self.append_log(f"🚨 [阶段1超温自愈] 快速逼近过程中发生超温 ({fail_reason})，未测出真实Vmin！启动自愈冷却重测 Row {r}...")
                            continue
                        else:
                            fail_v = self.get_current_measured_voltage() or boot_v
                            last_case_passed_vmin = fail_v
                            dur_sec = time.time() - case_start_time
                            self._record_vmin_result(r, freq, fail_v, fail_reason, "快速逼近阶段", dur_sec, wb, excel_path, summary_records)
                            last_case_reboot_mono = self._handle_case_fail_and_reboot(fail_reason, is_hardware_reboot=self.rt_watchdog.is_reboot_fail)
                            case_completed = True
                            break

                    # 阶段 2：初始基准稳态测试
                    self.clear_voltage_stats()
                    current_v = self.get_current_measured_voltage() or fast_ramp_target_v
                    
                    self.append_log(f"    RM1010 P8 实测电压到达目标 ({current_v:.4f} V)，开启单步测试时长 ({self.get_step_delay():.1f}s) 稳态测试...")

                    is_failed, fail_reason = self.execute_smart_step_watchdog()
                    if is_failed:
                        if self.is_thermal_fail(fail_reason):
                            self.append_log(f"🚨 [阶段2超温自愈] 初始稳态过程中发生超温 ({fail_reason})，未测出真实Vmin！启动自愈冷却重测 Row {r}...")
                            continue
                        else:
                            normal_vmin = boot_v
                            last_case_passed_vmin = normal_vmin
                            dur_sec = time.time() - case_start_time
                            self._record_vmin_result(r, freq, normal_vmin, fail_reason, "初始稳态", dur_sec, wb, excel_path, summary_records)
                            last_case_reboot_mono = self._handle_case_fail_and_reboot(fail_reason, is_hardware_reboot=self.rt_watchdog.is_reboot_fail)
                            case_completed = True
                            break

                    with self.rm1010.lock:
                        last_stable_v = self.rm1010.stats.mean if self.rm1010.stats.count > 0 else (self.rm1010.voltage_val or current_v)
                    self.append_log(f"    初始稳态通过 PASS，基准电压: {last_stable_v:.4f} V")

                    # 阶段 3：10mV 精密闭环步进寻优
                    step_overheated = False
                    while self.test_running:
                        target_next_v = round(last_stable_v - 0.010, 4)
                        self.append_log(f"\n---> [Row {r}] 🎯 下一档目标: 核心电压下降 10mV -> {target_next_v:.4f} V")

                        is_failed, fail_reason = self.ramp_dc_until_dmm_drops_to(target_next_v)
                        if fail_reason == "PREEMPTED_BY_USER":
                            continue

                        if is_failed:
                            if self.is_thermal_fail(fail_reason):
                                self.append_log(f"🚨 [阶段3超温自愈] 步进逼近过程中发生超温 ({fail_reason})，未测出真实Vmin！启动自愈冷却重测 Row {r}...")
                                step_overheated = True
                                break
                            else:
                                normal_vmin = last_stable_v
                                last_case_passed_vmin = normal_vmin
                                dur_sec = time.time() - case_start_time
                                self._record_vmin_result(r, freq, normal_vmin, fail_reason, "逼近中", dur_sec, wb, excel_path, summary_records)
                                last_case_reboot_mono = self._handle_case_fail_and_reboot(fail_reason, is_hardware_reboot=self.rt_watchdog.is_reboot_fail)
                                case_completed = True
                                break

                        self.clear_voltage_stats()
                        cur_meas_v = self.get_current_measured_voltage() or target_next_v
                        
                        self.append_log(f"    已降 10mV (实测: {cur_meas_v:.4f} V)，开启单步测试时长 ({self.get_step_delay():.1f}s) 稳态测试...")

                        is_failed, fail_reason = self.execute_smart_step_watchdog()
                        if is_failed:
                            if self.is_thermal_fail(fail_reason):
                                self.append_log(f"🚨 [阶段3超温自愈] 稳态测试过程中发生超温 ({fail_reason})，未测出真实Vmin！启动自愈冷却重测 Row {r}...")
                                step_overheated = True
                                break
                            else:
                                normal_vmin = last_stable_v
                                last_case_passed_vmin = normal_vmin
                                dur_sec = time.time() - case_start_time
                                self._record_vmin_result(r, freq, normal_vmin, fail_reason, "稳态判裁", dur_sec, wb, excel_path, summary_records)
                                last_case_reboot_mono = self._handle_case_fail_and_reboot(fail_reason, is_hardware_reboot=self.rt_watchdog.is_reboot_fail)
                                case_completed = True
                                break

                        with self.rm1010.lock:
                            last_stable_v = self.rm1010.stats.mean if self.rm1010.stats.count > 0 else (self.rm1010.voltage_val or target_next_v)
                        self.append_log(f"    稳态通过 PASS！受信任 Vmin 更新为: {last_stable_v:.4f} V")

                    if step_overheated:
                        continue

            total_elapsed_sec = time.time() - test_start_time
            total_dur_str = format_duration(total_elapsed_sec)
            
            total_time_row = max_test_row + 1
            sheet.cell(row=total_time_row, column=7).value = "总耗时"
            sheet.cell(row=total_time_row, column=8).value = total_dur_str

            case_row_indices = [c[0] for c in raw_cases]
            final_chip_id = self.sync_chip_id_to_excel(wb, sheet, case_row_indices, excel_path)
            if final_chip_id:
                self.append_log(f"📋 [Chip ID 批量同步] 已将最新 Chip ID ({final_chip_id}) 完整写入所有 {len(case_row_indices)} 组用例行。")

            for _ in range(3):
                try: wb.save(excel_path); break
                except Exception: time.sleep(0.4)

            if summary_records:
                self.append_log("\n==========================================================")
                self.append_log(">>>> Vmin 测试结果最终汇总 <<<<")
                self.append_log("==========================================================")
                for f_line, r_line in summary_records:
                    self.append_log_raw(f_line)
                    self.append_log_raw(r_line)
                self.append_log(f"★ 整体测试总耗时: {total_dur_str} (已写入 Excel H{total_time_row} 单元格)")
                self.append_log("==========================================================")

            if self.test_running:
                self.append_log("\n🎉🎉 全部用例测试完毕！已完成终态 0V 归零与复位，直接更换芯片即可测下一颗。🎉🎉")
                self._auto_open_excel(excel_path)

        except Exception as e:
            self.append_log(f"[系统运行时异常]: {e}")
        finally:
            if 'wb' in locals() and 'sheet' in locals() and 'raw_cases' in locals():
                try:
                    self.sync_chip_id_to_excel(wb, sheet, [c[0] for c in raw_cases], excel_path)
                except Exception:
                    pass
            self._finalize_device_safe_state(self.rt_watchdog.is_reboot_fail)
            self.test_running = False
            self._ui_call(self._set_test_buttons_idle)

    def update_ui_loop(self):
        if not self.running or self._closing: return
        try:
            self._drain_gui_queues()

            for prefix in ["board", "pwr", "hg"]:
                is_act = getattr(self, f"{prefix}_active", False)
                if self._last_conn_states.get(prefix) != is_act:
                    self._last_conn_states[prefix] = is_act
                    btn = getattr(self, f"btn_{prefix}_conn", None)
                    if btn:
                        if is_act:
                            btn.config(text="断开")
                            self._apply_liquid_glass_btn(btn, self.THEME["glass_red_bg"], "white", self.THEME["glass_red_hover"], self.THEME["glass_red_border"])
                        else:
                            btn.config(text="连接")
                            self._apply_liquid_glass_btn(btn, self.THEME["glass_blue_bg"], "white", self.THEME["glass_blue_hover"], self.THEME["glass_blue_border"])

            hg_active = self.hg_active and bool(self.heatgun.ser and getattr(self.heatgun.ser, "is_open", False))
            hg_is_on = self.heatgun.is_on
            hg_btn_state = (hg_active, hg_is_on)
            if hg_btn_state != self._last_hg_btn_state:
                self._sync_heatgun_button_state()

            pwr_active = self.pwr_active
            p_out = self.pwr_output_state if pwr_active else False
            pwr_btn_state = (pwr_active, p_out)
            if pwr_btn_state != self._last_pwr_output_state:
                self._sync_pwr_output_button_state()

            with self.rm1010.lock:
                rm_conn = self.rm1010.connected
                rm_recon = self.rm1010.is_reconnecting
                sn_val = self.rm1010.device_sn
                volt_val = self.rm1010.voltage_val
                volt_raw = self.rm1010.voltage_raw
                p8_code = self.rm1010.mean_raw_p8
                p15_code = self.rm1010.mean_raw_p15
                seq_val = self.rm1010.sample_seq
                count = self.rm1010.stats.count
                v_max = self.rm1010.stats.max_val if count > 0 else None
                v_min = self.rm1010.stats.min_val if count > 0 else None
                v_avg = self.rm1010.stats.mean if count > 0 else None

            if rm_conn:
                rm_str = f"RM1010: 已就绪 (SN:{sn_val} | 14-Bit ADC)"
                rm_fg = self.THEME["accent_green"]
            elif rm_recon:
                rm_str = "RM1010: USB重连中..."
                rm_fg = self.THEME["accent_amber"]
            else:
                rm_str = "RM1010: 未连接"
                rm_fg = self.THEME["accent_red"]

            if rm_str != self._last_rm_status_str:
                self._last_rm_status_str = rm_str
                self.lbl_rm1010_status.config(text=rm_str, fg=rm_fg)

            v_hero_str = f"{volt_val:.5f} V" if volt_val is not None else "-- V"
            if v_hero_str != self._last_v_hero_str:
                self._last_v_hero_str = v_hero_str
                self.lbl_volt_hero.config(text=v_hero_str)

            if seq_val != self._displayed_sample_seq:
                self._displayed_sample_seq = seq_val
                vpp_mv = (v_max - v_min) * 1000.0 if (v_max is not None and v_min is not None) else 0.0
                raw_str = f"{volt_raw:.4f}V" if volt_raw is not None else "--"
                self.lbl_volt_meta.config(
                    text=f"P8码: {p8_code:.1f} | P15基准: {p15_code:.1f} | 原始未滤: {raw_str} | 读数: #{seq_val} (5Hz) | 波动: {vpp_mv:.2f}mV"
                )

            if count > 0:
                v_stats_tuple = (f"{v_max:.4f}", f"{v_min:.4f}", f"{v_avg:.4f}", f"{count} 次")
            else:
                v_stats_tuple = ("--", "--", "--", "0 次")

            if v_stats_tuple != self._last_v_stats_tuple:
                self._last_v_stats_tuple = v_stats_tuple
                self.lbl_volt_max.config(text=v_stats_tuple[0])
                self.lbl_volt_min.config(text=v_stats_tuple[1])
                self.lbl_volt_avg.config(text=v_stats_tuple[2])
                self.lbl_volt_count.config(text=v_stats_tuple[3])

            with self.lock:
                pv = self.pwr_measured_v if self.pwr_active else None
                pi = self.pwr_measured_i if self.pwr_active else None
            pwr_rt_tuple = (f"{pv:.2f} V" if pv is not None else "-- V", f"{pi:.3f} A" if pi is not None else "-- A")
            if pwr_rt_tuple != self._last_pwr_rt_tuple:
                self._last_pwr_rt_tuple = pwr_rt_tuple
                self.lbl_pwr_v.config(text=pwr_rt_tuple[0])
                self.lbl_pwr_i.config(text=pwr_rt_tuple[1])

            tj, _ = self.tj_parser.get_latest_tj(max_age_sec=5.0)
            target_str = self.cb_target_temp.get().strip()
            is_high_mode, parsed_target = self._parse_temp_mode(target_str)
            if tj is not None:
                tj_disp = f"Tj: {tj:.1f} ℃"
                if is_high_mode and parsed_target is not None:
                    t_target = parsed_target
                    tj_disp += f" (目标: {t_target:.0f}~{t_target+1.0:.0f}℃)"
                    if t_target <= tj <= (t_target + 1.0):
                        fg_c = self.THEME["accent_green"]
                    elif tj > (t_target + 1.0):
                        fg_c = self.THEME["accent_red"]
                    else:
                        fg_c = self.THEME["accent_amber"]
                else:
                    fg_c = self.THEME["text_main"]
            else:
                if self.thermal_mgr.control_running and is_high_mode:
                    tj_disp = f"Tj: 采集中... (目标: {target_str})"
                    fg_c = self.THEME["accent_amber"]
                else:
                    tj_disp = "Tj: -- ℃"
                    fg_c = self.THEME["text_sub"]

            if tj_disp != self._last_tj_str:
                self._last_tj_str = tj_disp
                self.lbl_tj_status.config(text=tj_disp, fg=fg_c)

        except Exception: pass
        finally:
            self._schedule_ui_loop()

    def on_closing(self):
        if self._closing: return
        self._closing = True
        self.running = False
        self.test_running = False
        try:
            self.rt_watchdog.interrupt_event.set()
        except Exception: pass
        with self.lock:
            self.board_active = False
            self.pwr_active = False
            self.hg_active = False
        self._stop_thermal_and_flush_cmds()
        self.reset_heatgun_to_80("程序退出")
        self.heatgun.close()
        self.rm1010.close()
        for thread in getattr(self, "serial_threads", []):
            if thread.is_alive():
                thread.join(timeout=0.2)
        try: self.root.destroy()
        except (RuntimeError, tk.TclError): pass


if __name__ == '__main__':
    root = tk.Tk()
    style = ttk.Style()
    style.theme_use('clam')
    style.configure('.', font=('Microsoft YaHei UI', 10))
    style.configure('TCombobox', font=('Microsoft YaHei UI', 10))
    root.option_add('*TCombobox*Listbox.font', ('Microsoft YaHei UI', 10))
    app = VminAutoTestGUI(root)
    root.mainloop()