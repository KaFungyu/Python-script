"""
ART-DAQ (ART Technology USB Data Acquisition Device) USB3132A
高精度差分采集控制台 v4.6 (64位原生安全·零延迟秒粘贴版)
"""

import sys
import os
import re
import time
import json
import math
import ctypes
import threading
from datetime import datetime
from collections import deque

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

try:
    import numpy as np
except ImportError:
    np = None

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

# ==================== ART-DAQ 硬件底层驱动封装 (ACTS2200_64.dll) ====================

class ACTS2200_AI_CH_PARAM(ctypes.Structure):
    _fields_ = [
        ('nChannel', ctypes.c_uint32),
        ('nSampRange', ctypes.c_uint32),
        ('nRefGround', ctypes.c_uint32),
        ('nReserved0', ctypes.c_uint32),
        ('nReserved1', ctypes.c_uint32),
        ('nReserved2', ctypes.c_uint32),
    ]

class ACTS2200_AI_START_TRIG(ctypes.Structure):
    _fields_ = [
        ('nTrigType', ctypes.c_uint32),
        ('nTrigSrc', ctypes.c_uint32),
        ('nTrigDir', ctypes.c_uint32),
        ('bReTrig', ctypes.c_uint32),
        ('fTrigLvlTop', ctypes.c_float),
        ('fTrigLvlBtm', ctypes.c_float),
        ('fTrigDigFltrMinPulseWidth', ctypes.c_double),
        ('fDelaySeconds', ctypes.c_double),
        ('nReserved0', ctypes.c_uint32),
        ('nReserved1', ctypes.c_uint32),
    ]

class ACTS2200_AI_PAUSE_TRIG(ctypes.Structure):
    _fields_ = [
        ('nTrigType', ctypes.c_uint32),
        ('nTrigSrc', ctypes.c_uint32),
        ('nTrigDir', ctypes.c_uint32),
        ('nReserved0', ctypes.c_uint32),
        ('fTrigLvlTop', ctypes.c_float),
        ('fTrigLvlBtm', ctypes.c_float),
        ('fTrigDigFltrMinPulseWidth', ctypes.c_double),
        ('nReserved1', ctypes.c_uint32),
        ('nReserved2', ctypes.c_uint32),
    ]

class ACTS2200_AI_REF_TRIG(ctypes.Structure):
    _fields_ = [
        ('nTrigType', ctypes.c_uint32),
        ('nTrigSrc', ctypes.c_uint32),
        ('nTrigDir', ctypes.c_uint32),
        ('nPretrigSamps', ctypes.c_uint32),
        ('fTrigLvlTop', ctypes.c_float),
        ('fTrigLvlBtm', ctypes.c_float),
        ('fTrigDigFltrMinPulseWidth', ctypes.c_double),
        ('nReserved0', ctypes.c_uint32),
        ('nReserved1', ctypes.c_uint32),
    ]

class ACTS2200_AI_PARAM(ctypes.Structure):
    _fields_ = [
        ('nSampChanCount', ctypes.c_uint32),
        ('nSampSignal', ctypes.c_uint32),
        ('nReserved0', ctypes.c_uint32),
        ('nReserved1', ctypes.c_uint32),
        ('CHParam', ACTS2200_AI_CH_PARAM * 32),
        ('nSampMode', ctypes.c_uint32),
        ('nSampsPerChan', ctypes.c_uint32),
        ('fSampRate', ctypes.c_double),
        ('nSampClkSrc', ctypes.c_uint32),
        ('nExtSampClkEdge', ctypes.c_uint32),
        ('StartTrig', ACTS2200_AI_START_TRIG),
        ('PauseTrig', ACTS2200_AI_PAUSE_TRIG),
        ('RefTrig', ACTS2200_AI_REF_TRIG),
        ('nReserved2', ctypes.c_uint32),
        ('nReserved3', ctypes.c_uint32),
    ]

class ACTS2200_DEV_INFO(ctypes.Structure):
    _fields_ = [
        ('nSerialCode', ctypes.c_uint32),
        ('nDeviceType', ctypes.c_uint32),
        ('bUsed', ctypes.c_int),
    ]

class ArtDaqDriver:
    def __init__(self):
        self.dll = None
        self._load_driver()

    def _load_driver(self):
        dll_name = 'ACTS2200_64.dll'
        search_paths = [
            r'D:\ART\Cards\ACTS2200\Lib\x64\ACTS2200_64.dll',
            r'C:\Windows\System32\ACTS2200_64.dll',
        ]
        bundle_dir = getattr(sys, '_MEIPASS', None)
        if bundle_dir:
            search_paths.insert(0, os.path.join(bundle_dir, dll_name))
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            search_paths.append(os.path.join(script_dir, dll_name))
        except Exception: pass
        search_paths.append(dll_name)

        for path in search_paths:
            try:
                if os.path.isabs(path) and not os.path.isfile(path):
                    continue
                full_path = os.path.abspath(path) if os.path.isfile(path) else path
                if os.name == 'nt' and hasattr(os, 'add_dll_directory') and os.path.isabs(full_path):
                    try: self._dll_directory_handle = os.add_dll_directory(os.path.dirname(full_path))
                    except OSError: self._dll_directory_handle = None
                self.dll = ctypes.WinDLL(full_path) if os.name == 'nt' else None
                if self.dll:
                    self._bind_api()
                    self.dll_path = full_path
                    return
            except Exception as exc:
                self.last_load_error = str(exc)
                self.dll = None
        self.last_load_error = getattr(self, 'last_load_error', '找不到 ACTS2200_64.dll')

    def _bind_api(self):
        if not self.dll: return
        self.dll.ACTS2200_DeviceList.restype = ctypes.c_int
        self.dll.ACTS2200_DeviceList.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte)]

        self.dll.ACTS2200_DEV_Create.restype = ctypes.c_void_p
        self.dll.ACTS2200_DEV_Create.argtypes = [ctypes.c_uint32]

        self.dll.ACTS2200_DEV_Release.restype = None
        self.dll.ACTS2200_DEV_Release.argtypes = [ctypes.c_void_p]

        self.dll.ACTS2200_AI_ResetParam.restype = ctypes.c_int
        self.dll.ACTS2200_AI_ResetParam.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        self.dll.ACTS2200_AI_VerifyParam.restype = ctypes.c_int
        self.dll.ACTS2200_AI_VerifyParam.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        self.dll.ACTS2200_AI_InitTask.restype = ctypes.c_int
        self.dll.ACTS2200_AI_InitTask.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        self.dll.ACTS2200_AI_StartTask.restype = ctypes.c_int
        self.dll.ACTS2200_AI_StartTask.argtypes = [ctypes.c_void_p]

        self.dll.ACTS2200_AI_StopTask.restype = ctypes.c_int
        self.dll.ACTS2200_AI_StopTask.argtypes = [ctypes.c_void_p]

        self.dll.ACTS2200_AI_ReleaseTask.restype = ctypes.c_int
        self.dll.ACTS2200_AI_ReleaseTask.argtypes = [ctypes.c_void_p]

        self.dll.ACTS2200_AI_ReadAnalog.restype = ctypes.c_int
        self.dll.ACTS2200_AI_ReadAnalog.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_double,
            ctypes.c_uint32
        ]

    def list_devices(self):
        if not self.dll: return []
        dev_array = (ACTS2200_DEV_INFO * 16)()
        dev_num = ctypes.c_ubyte(0)
        ret = self.dll.ACTS2200_DeviceList(dev_array, ctypes.c_ubyte(16), ctypes.byref(dev_num))
        if ret and dev_num.value > 0:
            return [(dev_array[i].nSerialCode, str(dev_array[i].nSerialCode)) for i in range(dev_num.value)]
        return []

    def open_device(self, serial_num):
        if not self.dll: return None
        h_dev = self.dll.ACTS2200_DEV_Create(ctypes.c_uint32(int(serial_num)))
        return h_dev if (h_dev and h_dev != -1) else None

    def close_device(self, h_dev):
        if self.dll and h_dev:
            try:
                self.dll.ACTS2200_AI_StopTask(h_dev)
                self.dll.ACTS2200_AI_ReleaseTask(h_dev)
            except Exception: pass
            try: self.dll.ACTS2200_DEV_Release(h_dev)
            except Exception: pass

    def setup_differential_ai(self, h_dev, range_codes, channel_indices=None, rate_hz=10000.0, samps_per_chan=500):
        self.last_setup_error = ""
        if not self.dll or not h_dev:
            self.last_setup_error = "DLL 或设备句柄无效"
            return False, 0.0, self.last_setup_error

        indices = list(range(MAX_DIFF_CHANNELS)) if channel_indices is None else [int(i) for i in channel_indices]
        if not indices or len(indices) > MAX_DIFF_CHANNELS or len(set(indices)) != len(indices):
            self.last_setup_error = f"差分扫描通道配置无效：{indices}"
            return False, 0.0, self.last_setup_error

        ai_param = ACTS2200_AI_PARAM()
        try:
            self.dll.ACTS2200_AI_ResetParam(h_dev, ctypes.byref(ai_param))
            ai_param.nSampChanCount = len(indices)
            for slot, channel_index in enumerate(indices):
                ai_param.CHParam[slot].nChannel = channel_index
                ai_param.CHParam[slot].nSampRange = int(range_codes[slot]) if slot < len(range_codes) else 3
                ai_param.CHParam[slot].nRefGround = 2  # 差分模式 DIFF
            ai_param.nSampMode = 3
            ai_param.fSampRate = float(rate_hz)
            ai_param.nSampsPerChan = int(samps_per_chan)

            if not self.dll.ACTS2200_AI_VerifyParam(h_dev, ctypes.byref(ai_param)):
                self.last_setup_error = f"ACTS2200_AI_VerifyParam 失败；请求扫描速率={rate_hz:g} Hz，通道={indices}"
                return False, float(ai_param.fSampRate), self.last_setup_error

            actual_channel_rate = float(ai_param.fSampRate)
            if not self.dll.ACTS2200_AI_InitTask(h_dev, ctypes.byref(ai_param)):
                self.last_setup_error = f"ACTS2200_AI_InitTask 失败；速率={actual_channel_rate:g} Hz"
                return False, actual_channel_rate, self.last_setup_error

            if not self.dll.ACTS2200_AI_StartTask(h_dev):
                self.last_setup_error = f"ACTS2200_AI_StartTask 失败；速率={actual_channel_rate:g} Hz"
                try: self.dll.ACTS2200_AI_ReleaseTask(h_dev)
                except Exception: pass
                return False, actual_channel_rate, self.last_setup_error

            return True, actual_channel_rate, ""
        except Exception as exc:
            self.last_setup_error = f"驱动调用异常：{exc}"
            return False, 0.0, self.last_setup_error

# ==================== 量程、间隔映射与常数定义 ====================

RANGE_LIST = ["±1V", "±2V", "±5V", "±10V"]
RANGE_CODE_MAP = {"±1V": 3, "±2V": 2, "±5V": 1, "±10V": 0}
RANGE_LIMITS_VOLTS = {"±1V": 1.0, "±2V": 2.0, "±5V": 5.0, "±10V": 10.0}
RANGE_NEXT_MAP = {"±1V": "±2V", "±2V": "±5V", "±5V": "±10V", "±10V": "±10V"}

INTERVAL_SECONDS_MAP = {
    "4us": 0.000004,
    "10ms": 0.010,
    "20ms": 0.020,
    "50ms": 0.050,
    "100ms": 0.100,
    "200ms": 0.200,
    "500ms": 0.500,
    "1s": 1.0,
    "2s": 2.0,
    "5s": 5.0,
}
INTERVAL_LIST = list(INTERVAL_SECONDS_MAP.keys())
MAX_AI_TOTAL_RATE_HZ = 250000.0
MAX_DIFF_CHANNELS = 8
READ_BLOCK_MAX_SAMPLES_PER_CH = 500

CURRENT_TAGS = ("(MA)", "[MA]", "_MA", " MA", "CUR", "CURRENT", "SHUNT")
VOLTAGE_TAGS = ("(V)", "[V]", "_V", " V", "VOLT", "STANDBY", "VDD", "VCC", "AVDD", "VDDIO")
IGNORED_RATIO_LITERALS = frozenset(("1", "+1", "-1", "0", "+0", "-0"))
IGNORED_NAV_KEYSYMS = frozenset((
    "Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R",
    "Caps_Lock", "Tab", "Escape", "Return", "Left", "Right", "Up", "Down",
    "Home", "End", "Prior", "Next", "Insert", "Pause", "Print", "Num_Lock",
    "Scroll_Lock", "Menu"
))
GMEM_MOVEABLE = 0x0002
CF_UNICODETEXT = 13


class ChannelStats:
    """流式在线统计器：均值采用无损 Kahan 累加；极值采用带开机建型遮蔽的二阶临界阻尼低通滤波，精准对齐日置 LR8401-21。"""
    __slots__ = (
        'sum', 'comp', 'count', 'filt_max', 'filt_min', 'over_count',
        'alpha', 'beta', 'y1', 'y2', 'is_initialized', 'warmup_samples'
    )

    def __init__(self, sample_rate_hz=31250.0):
        self.reset(sample_rate_hz)

    def reset(self, sample_rate_hz=None):
        self.sum = 0.0
        self.comp = 0.0
        self.count = 0
        self.filt_max = -float('inf')
        self.filt_min = float('inf')
        self.over_count = 0
        self.y1 = 0.0
        self.y2 = 0.0
        self.is_initialized = False

        rate = sample_rate_hz if (sample_rate_hz is not None and sample_rate_hz > 0) else 31250.0
        dt = 1.0 / rate
        tau = 0.060
        self.alpha = dt / (tau + dt)
        self.beta = 1.0 - self.alpha
        self.warmup_samples = max(10, int(rate * 0.40))

    def add(self, val):
        self.count += 1
        t = self.sum + val
        if abs(self.sum) >= abs(val): self.comp += (self.sum - t) + val
        else: self.comp += (val - t) + self.sum
        self.sum = t

        if not self.is_initialized:
            self.y1 = val
            self.y2 = val
            self.is_initialized = True
        else:
            self.y1 = self.beta * self.y1 + self.alpha * val
            self.y2 = self.beta * self.y2 + self.alpha * self.y1

        if self.count > self.warmup_samples:
            f_val = self.y2
            if f_val > self.filt_max: self.filt_max = f_val
            if f_val < self.filt_min: self.filt_min = f_val

    def add_block(self, vals):
        n = len(vals)
        if n == 0: return

        b_sum = float(np.sum(vals))
        t = self.sum + b_sum
        if abs(self.sum) >= abs(b_sum): self.comp += (self.sum - t) + b_sum
        else: self.comp += (b_sum - t) + self.sum
        self.sum = t

        if not self.is_initialized:
            init_v = float(np.mean(vals[:min(n, 64)]))
            self.y1 = init_v
            self.y2 = init_v
            self.is_initialized = True

        a = self.alpha
        b = self.beta
        y1 = self.y1
        y2 = self.y2

        f_max = self.filt_max
        f_min = self.filt_min

        start_count = self.count
        self.count += n
        warmup = self.warmup_samples

        for idx, v in enumerate(vals):
            y1 = b * y1 + a * v
            y2 = b * y2 + a * y1
            if (start_count + idx) > warmup:
                if y2 > f_max: f_max = y2
                if y2 < f_min: f_min = y2

        self.y1 = y1
        self.y2 = y2
        self.filt_max = f_max
        self.filt_min = f_min

    def mark_over(self):
        self.over_count += 1

    @property
    def avg(self):
        return (self.sum + self.comp) / self.count if self.count > 0 else math.nan

    @property
    def max_val(self):
        if self.count == 0: return math.nan
        if self.filt_max == -float('inf'): return self.y2
        return self.filt_max

    @property
    def min_val(self):
        if self.count == 0: return math.nan
        if self.filt_min == float('inf'): return self.y2
        return self.filt_min


def is_current_channel(comment_str, ratio_str=None):
    c = str(comment_str or "").strip().upper()
    if any(tag in c for tag in CURRENT_TAGS): return True
    if any(tag in c for tag in VOLTAGE_TAGS): return False
    if ratio_str:
        r = str(ratio_str).strip()
        if r and r not in IGNORED_RATIO_LITERALS:
            try:
                val = float(r)
                if abs(abs(val) - 1.0) > 1e-6 and abs(val) > 1e-6:
                    return True
            except ValueError: pass
    return False


def invert_ratio_sign(ratio_text):
    text = str(ratio_text or "").strip()
    if not text: return "-1"
    if text.startswith("-"): return text[1:].strip() or "1"
    elif text.startswith("+"): return f"-{text[1:].strip() or '1'}"
    return f"-{text}"


def set_system_clipboard_native(text):
    """
    直接将纯文本推入 Windows 系统全局内存（64位指针严格定义 + finally强制防死锁释放机制）。
    免疫一切 Tkinter 焦点丢失与惰性延迟渲染，保证毫秒级可靠入剪贴板。
    """
    if sys.platform != "win32": return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 显式声明 64 位 API 形参与返回值类型，防止指针高位被截断为 32 位整型造成访问违规崩溃
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p

        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p

        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_int

        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_int

        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = ctypes.c_int

        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p

        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = ctypes.c_int

        data = text.encode("utf-16le") + b"\x00\x00"

        opened = False
        for _ in range(10):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.01)

        if not opened:
            return False

        try:
            user32.EmptyClipboard()
            h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if h_mem:
                p_mem = kernel32.GlobalLock(h_mem)
                if p_mem:
                    ctypes.memmove(p_mem, data, len(data))
                    kernel32.GlobalUnlock(h_mem)
                    user32.SetClipboardData(CF_UNICODETEXT, h_mem)
                    return True
            return False
        finally:
            # 无论成功与否，强制关闭并释放剪贴板锁，杜绝任何剪贴板死锁发生
            user32.CloseClipboard()
    except Exception:
        return False

# ==================== 主控控制台应用程序 ====================

class ArtDaqApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ART-DAQ USB3132A 差分采集系统 v4.6 (零延迟秒粘贴·工频极值对齐版)")
        self.root.geometry("1800x850")
        self.root.configure(bg="#ffffff")

        self.driver = ArtDaqDriver()
        self.h_dev1 = None
        self.h_dev2 = None

        self.dev1_var = tk.StringVar()
        self.dev2_var = tk.StringVar(value="[未连接 / 无 (单机8路)]")
        self.interval_var = tk.StringVar(value="4us")
        self.case_name = tk.StringVar(value="")

        self.is_connected = False
        self.channel_keys = [f"1-{unit}-{ch}" for unit in (1, 2) for ch in range(1, 9)]
        self.channel_metadata = {
            key: {
                "unit": int(key.split("-")[1]),
                "ch": int(key.split("-")[2]),
                "display": f"CH{key.split('-')[1]}_{key.split('-')[2]}",
                "pin_label": f"AI{int(key.split('-')[2])-1}+,AI{int(key.split('-')[2])+7}-"
            } for key in self.channel_keys
        }

        self.gui_val_cache = {}
        self.gui_stat_cache = {}
        self._lbl_text_cache = {}
        self.is_ma_channel = {}
        self.selected_keys = set()
        self.last_selected_key = None
        self.widget_to_key = {}
        self.is_dragging = False
        self.drag_start_key = None
        self.drag_base_selection = set()
        self.drag_last_key = None

        self.log_queue = deque(maxlen=2000)
        self.log_lock = threading.Lock()

        self.total_frames = 0
        self.last_clipboard_text = ""
        self.active_channels = []
        self.channel_limits_cache = {}
        self.last_test_stats = {}

        self.monitor_thread = None
        self.stop_monitor_event = threading.Event()
        self.stats = {key: ChannelStats() for key in self.channel_keys}

        self.tested_channels = set()
        self.over_ranged_channels = set()
        self.alerted_over_channels = set()
        self.over_channels_to_upgrade = set()
        self.pending_optimal_ranges = {}
        self.pending_polarity_invert = set()

        self.dt_start = None
        self.dt_end = None
        self.timer_running = False
        self.start_time = 0.0
        self.elapsed_time = 0.0
        self._save_timer_id = None
        self.init_completed = False

        self.channel_vars = {}
        for key in self.channel_keys:
            self.channel_vars[key] = {
                "comment": tk.StringVar(),
                "range": tk.StringVar(value="±1V"),
                "ratio": tk.StringVar(value=""),
                "lbl_val": None,
                "lbl_avg": None,
                "lbl_max": None,
                "lbl_min": None,
                "ent_comment": None,
                "ent_ratio": None,
                "lbl_ch": None,
                "cmb_range": None,
                "row_bg": "#ffffff"
            }

        self.create_widgets()
        self.load_local_config()
        self._update_channel_limits_cache()

        self.dev1_var.trace_add("write", self._schedule_save)
        self.dev2_var.trace_add("write", self._schedule_save)
        self.interval_var.trace_add("write", self._on_interval_var_changed)
        self.case_name.trace_add("write", self._schedule_save)
        for key, vars_dict in self.channel_vars.items():
            vars_dict["comment"].trace_add("write", self._schedule_save)
            vars_dict["range"].trace_add("write", self._on_range_changed_trace)
            vars_dict["ratio"].trace_add("write", lambda *a, k=key: self._on_ratio_changed_trace(k))

        self.init_completed = True
        self.refresh_ports_on_click()
        self.root.after(100, self._flush_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_save)

    # ---------------- UI 构建 ----------------

    def _create_ios_entry(self, parent, textvar, width, is_monospace=False):
        font = ("Segoe UI", 10, "bold") if is_monospace else ("Microsoft YaHei UI", 10)
        return tk.Entry(
            parent, textvariable=textvar, width=width, font=font, bd=0,
            bg="#ffffff", fg="#1c1c1e", highlightthickness=1,
            highlightbackground="#e5e5ea", highlightcolor="#007aff", insertbackground="#1c1c1e"
        )

    def create_widgets(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TLabel", font=("Microsoft YaHei UI", 10, "bold"), background="#ffffff", foreground="#1c1c1e")

        # 固化 TCombobox 样式，根治 clam 主题在 focus 状态下文字变白的 Bug
        style.configure("TCombobox",
                        foreground="#1c1c1e",
                        fieldbackground="#ffffff",
                        background="#f2f2f7",
                        arrowcolor="#8e8e93",
                        bordercolor="#e5e5ea",
                        lightcolor="#e5e5ea",
                        darkcolor="#e5e5ea")
        style.map("TCombobox",
                  fieldbackground=[("readonly", "#ffffff"), ("disabled", "#f2f2f7")],
                  selectbackground=[("readonly", "#bae6fd"), ("focus", "#bae6fd")],
                  selectforeground=[("readonly", "#1c1c1e"), ("focus", "#1c1c1e")],
                  foreground=[("disabled", "#aeaeb2"), ("readonly", "#1c1c1e"), ("focus", "#1c1c1e"), ("!disabled", "#1c1c1e")])

        conn_frame = tk.Frame(self.root, bg="#ffffff", highlightbackground="#e5e5ea", highlightthickness=1, bd=0)
        conn_frame.pack(fill="x", padx=15, pady=8)

        tk.Label(conn_frame, text=" ⚡ ART-DAQ USB3132A 差分采集系统 (极值工频对齐版) ", font=("Microsoft YaHei UI", 11, "bold"), bg="#ffffff", fg="#007aff").grid(row=0, column=0, columnspan=12, sticky="w", padx=10, pady=4)

        tk.Label(conn_frame, text="设备1 (CH1组):", bg="#ffffff", fg="#1c1c1e", font=("Microsoft YaHei UI", 10, "bold")).grid(row=1, column=0, padx=4, pady=6, sticky="w")
        self.dev1_combo = ttk.Combobox(conn_frame, textvariable=self.dev1_var, width=24, font=("Segoe UI", 9), state="readonly", postcommand=self.refresh_ports_on_click)
        self.dev1_combo.grid(row=1, column=1, padx=4, pady=6)

        tk.Label(conn_frame, text="设备2 (CH2组):", bg="#ffffff", fg="#1c1c1e", font=("Microsoft YaHei UI", 10, "bold")).grid(row=1, column=2, padx=4, pady=6, sticky="w")
        self.dev2_combo = ttk.Combobox(conn_frame, textvariable=self.dev2_var, width=24, font=("Segoe UI", 9), state="readonly", postcommand=self.refresh_ports_on_click)
        self.dev2_combo.grid(row=1, column=3, padx=4, pady=6)

        tk.Label(conn_frame, text="采样间隔:", bg="#ffffff", fg="#8e8e93", font=("Microsoft YaHei UI", 10)).grid(row=1, column=4, padx=4, pady=6, sticky="w")

        # 采样间隔下拉框
        self.interval_combo = ttk.Combobox(conn_frame, textvariable=self.interval_var, values=INTERVAL_LIST, width=8, font=("Segoe UI", 10), state="readonly")
        self.interval_combo.grid(row=1, column=5, padx=4, pady=6)
        self.interval_combo.bind("<<ComboboxSelected>>", self._on_interval_selected)

        self.btn_connect = tk.Button(
            conn_frame, text=" 连接设备 ", bg="#007aff", fg="white",
            font=("Microsoft YaHei UI", 10, "bold"), relief="flat", bd=0, cursor="hand2", padx=10, pady=3,
            command=self.toggle_connection
        )
        self.btn_connect.grid(row=1, column=6, padx=8, pady=6)

        self.lbl_status = tk.Label(conn_frame, text="未连接", fg="#ff3b30", font=("Microsoft YaHei UI", 10, "bold"), bg="#ffffff")
        self.lbl_status.grid(row=1, column=7, padx=6, pady=6)

        btn_opts = {
            "font": ("Microsoft YaHei UI", 9, "bold"), "relief": "flat", "padx": 10, "pady": 3,
            "fg": "#007aff", "bg": "#f2f2f7", "activebackground": "#e5e5ea", "activeforeground": "#0056b3",
            "bd": 0, "cursor": "hand2"
        }
        self.btn_import = tk.Button(conn_frame, text=" 📂 导入配置 ", command=self.import_config_file, **btn_opts)
        self.btn_import.grid(row=1, column=8, padx=4, pady=6)

        self.btn_export = tk.Button(conn_frame, text=" 💾 导出配置 ", command=self.export_config_file, **btn_opts)
        self.btn_export.grid(row=1, column=9, padx=4, pady=6)

        self.btn_batch = tk.Button(conn_frame, text=" 🔧 集中处理 ", command=self.open_batch_config_dialog, **btn_opts)
        self.btn_batch.grid(row=1, column=10, padx=4, pady=6)

        matrix_outer = tk.Frame(self.root, bg="#ffffff")
        matrix_outer.pack(fill="x", padx=15, pady=4)

        left_matrix = tk.Frame(matrix_outer, bg="#ffffff", highlightbackground="#e5e5ea", highlightthickness=1, bd=0)
        left_matrix.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=4)

        right_matrix = tk.Frame(matrix_outer, bg="#ffffff", highlightbackground="#e5e5ea", highlightthickness=1, bd=0)
        right_matrix.pack(side="right", fill="both", expand=True, padx=(5, 0), pady=4)

        def draw_header(parent, title):
            tk.Label(parent, text=title, font=("Microsoft YaHei UI", 11, "bold"), fg="#007aff", bg="#ffffff").grid(row=0, column=0, columnspan=8, pady=5)
            tk.Label(parent, text="通道 (差分接线)", font=("Microsoft YaHei UI", 9, "bold"), fg="#8e8e93", bg="#ffffff", width=16).grid(row=1, column=0, sticky="w", padx=2)
            tk.Label(parent, text="首注释 (多选及Excel粘)", font=("Microsoft YaHei UI", 9, "bold"), fg="#8e8e93", bg="#ffffff", width=18).grid(row=1, column=1, sticky="w", padx=2)
            tk.Label(parent, text="硬件量程", font=("Microsoft YaHei UI", 9, "bold"), fg="#8e8e93", bg="#ffffff", width=8).grid(row=1, column=2, sticky="w", padx=2)
            tk.Label(parent, text="转换比(1V/R*1k)", font=("Microsoft YaHei UI", 9, "bold"), fg="#8e8e93", bg="#ffffff", width=12).grid(row=1, column=3, sticky="w", padx=2)
            tk.Label(parent, text="实测值", font=("Microsoft YaHei UI", 9, "bold"), bg="#007aff", fg="white", width=12).grid(row=1, column=4, padx=2)
            tk.Label(parent, text="平均值 (Avg)", font=("Microsoft YaHei UI", 9, "bold"), bg="#f2f2f7", fg="#8e8e93", width=11).grid(row=1, column=5, padx=2)
            tk.Label(parent, text="最大值", font=("Microsoft YaHei UI", 9, "bold"), bg="#f2f2f7", fg="#8e8e93", width=11).grid(row=1, column=6, padx=2)
            tk.Label(parent, text="最小值", font=("Microsoft YaHei UI", 9, "bold"), bg="#f2f2f7", fg="#8e8e93", width=11).grid(row=1, column=7, padx=2)

        draw_header(left_matrix, "ART-DAQ #1 (CH1_1 ~ CH1_8, 差分 AI0~AI7 / AI8~AI15)")
        draw_header(right_matrix, "ART-DAQ #2 (CH2_1 ~ CH2_8, 差分 AI0~AI7 / AI8~AI15)")

        self.widget_to_key.clear()

        for unit, matrix_frame in [(1, left_matrix), (2, right_matrix)]:
            prefix = f"CH{unit}"
            for ch in range(1, 9):
                key = f"1-{unit}-{ch}"
                row_bg = "#ffffff" if ch % 2 == 0 else "#f8f9fa"
                self.channel_vars[key]["row_bg"] = row_bg
                pin_info = f"AI{ch-1}+,AI{ch+7}-"

                lbl_ch = tk.Label(matrix_frame, text=f"{prefix}_{ch} ({pin_info})", font=("Segoe UI", 9, "bold"), bg=row_bg, fg="#1c1c1e", cursor="hand2", anchor="w")
                lbl_ch.grid(row=ch+1, column=0, sticky="nsew", pady=2, padx=2)

                ent_comment = self._create_ios_entry(matrix_frame, self.channel_vars[key]["comment"], width=18)
                ent_comment.grid(row=ch+1, column=1, sticky="nsew", padx=2, pady=2)

                cmb_range = ttk.Combobox(matrix_frame, textvariable=self.channel_vars[key]["range"], values=RANGE_LIST, width=7, font=("Segoe UI", 9), state="readonly")
                cmb_range.grid(row=ch+1, column=2, sticky="nsew", padx=2, pady=2)
                cmb_range.bind("<<ComboboxSelected>>", lambda e, k=key: self.on_range_combobox_selected(k))

                ent_ratio = self._create_ios_entry(matrix_frame, self.channel_vars[key]["ratio"], width=12, is_monospace=True)
                ent_ratio.grid(row=ch+1, column=3, sticky="nsew", padx=2, pady=2)

                self.channel_vars[key]["ent_comment"] = ent_comment
                self.channel_vars[key]["ent_ratio"] = ent_ratio
                self.channel_vars[key]["lbl_ch"] = lbl_ch
                self.channel_vars[key]["cmb_range"] = cmb_range

                self.widget_to_key[lbl_ch] = key
                self.widget_to_key[ent_comment] = key
                self.widget_to_key[ent_ratio] = key
                self.widget_to_key[cmb_range] = key

                for w in (lbl_ch, ent_comment, ent_ratio):
                    w.bind("<ButtonPress-1>", lambda e, k=key: self.on_drag_start(k, e))
                    w.bind("<B1-Motion>", self.on_drag_motion)
                    w.bind("<ButtonRelease-1>", self.on_drag_end)

                ent_comment.bind("<KeyRelease>", lambda e, k=key: self.on_comment_keyrelease(k, e))
                ent_comment.bind("<Control-c>", lambda e, k=key: self.on_comment_copy(k, e))
                ent_comment.bind("<Control-v>", lambda e, k=key: self.on_comment_paste(k, e))
                ent_comment.bind("<Delete>", lambda e, k=key: self.on_comment_delete(k, e))
                ent_comment.bind("<BackSpace>", lambda e, k=key: self.on_comment_delete(k, e))

                ent_ratio.bind("<KeyRelease>", lambda e, k=key: self.on_ratio_keyrelease(k, e))
                ent_ratio.bind("<Control-c>", lambda e, k=key: self.on_ratio_copy(k, e))
                ent_ratio.bind("<Control-v>", lambda e, k=key: self.on_ratio_paste(k, e))
                ent_ratio.bind("<Delete>", lambda e, k=key: self.on_ratio_delete(k, e))
                ent_ratio.bind("<BackSpace>", lambda e, k=key: self.on_ratio_delete(k, e))

                lbl_val = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=12, bd=0)
                lbl_val.grid(row=ch+1, column=4, sticky="nsew", padx=2, pady=2)
                self.channel_vars[key]["lbl_val"] = lbl_val

                lbl_avg = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=11, bd=0)
                lbl_avg.grid(row=ch+1, column=5, sticky="nsew", padx=2, pady=2)
                self.channel_vars[key]["lbl_avg"] = lbl_avg

                lbl_max = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=11, bd=0)
                lbl_max.grid(row=ch+1, column=6, sticky="nsew", padx=2, pady=2)
                self.channel_vars[key]["lbl_max"] = lbl_max

                lbl_min = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=11, bd=0)
                lbl_min.grid(row=ch+1, column=7, sticky="nsew", padx=2, pady=2)
                self.channel_vars[key]["lbl_min"] = lbl_min

        control_frame = tk.Frame(self.root, bg="#ffffff", highlightbackground="#e5e5ea", highlightthickness=1, bd=0)
        control_frame.pack(fill="x", padx=15, pady=4)

        self.lbl_timer = tk.Label(control_frame, text="00:00.0", font=("Segoe UI", 28, "bold"), bg="#f2f2f7", fg="#1c1c1e", width=12, bd=0)
        self.lbl_timer.pack(side="left", padx=12, pady=6)

        self.btn_start = tk.Button(
            control_frame, text="▶ 开始采集 (Start)", bg="#34c759", fg="white",
            font=("Microsoft YaHei UI", 11, "bold"), relief="flat", height=2, width=18,
            state="disabled", cursor="hand2", bd=0, activebackground="#2c9e47", activeforeground="white",
            command=self.start_acquisition
        )
        self.btn_start.pack(side="left", expand=True, padx=12)

        self.btn_copy = tk.Button(
            control_frame, text="📋 一键复制数据", bg="#5e5ce6", fg="white",
            font=("Microsoft YaHei UI", 11, "bold"), relief="flat", height=2, width=18,
            cursor="hand2", bd=0, activebackground="#4d4cb8", activeforeground="white",
            command=self.copy_clipboard_data
        )
        self.btn_copy.pack(side="left", expand=True, padx=12)

        self.btn_stop = tk.Button(
            control_frame, text="■ 停止并保存 (Stop)", bg="#ff3b30", fg="white",
            font=("Microsoft YaHei UI", 11, "bold"), relief="flat", height=2, width=18,
            state="disabled", cursor="hand2", bd=0, activebackground="#e03126", activeforeground="white",
            command=self.stop_acquisition
        )
        self.btn_stop.pack(side="right", expand=True, padx=12)

        log_frame = tk.Frame(self.root, bg="#ffffff", highlightbackground="#e5e5ea", highlightthickness=1, bd=0)
        log_frame.pack(fill="both", expand=True, padx=15, pady=6)

        tk.Label(log_frame, text=" 实时程控日志及看板 ", font=("Microsoft YaHei UI", 10, "bold"), bg="#ffffff", fg="#8e8e93").pack(anchor="w", padx=10, pady=3)
        self.log_area = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 10), bg="#ffffff", fg="#1c1c1e", bd=0, highlightthickness=1, highlightbackground="#e5e5ea")
        self.log_area.pack(fill="both", expand=True, padx=10, pady=4)

    # ---------------- 下拉框事件与防空白处理 ----------------

    def _on_interval_selected(self, event=None):
        self.root.focus_set()
        self._schedule_save()

    def _on_interval_var_changed(self, *args):
        v = self.interval_var.get()
        if not v or v not in INTERVAL_LIST:
            self.interval_var.set("4us")
        self._schedule_save()

    # ---------------- 设备通信与连接管理 ----------------

    def refresh_ports_on_click(self):
        device_entries = []
        if self.driver:
            art_devs = self.driver.list_devices()
            for _, sn_str in art_devs:
                device_entries.append(f"ART-DAQ (SN: {sn_str})")

        if serial:
            try:
                for p in serial.tools.list_ports.comports():
                    device_entries.append(f"{p.device} (SN: {p.serial_number})" if p.serial_number else p.device)
            except Exception: pass

        if not device_entries:
            device_entries = ["无可用设备"]

        self.dev1_combo['values'] = device_entries
        self.dev2_combo['values'] = ["[未连接 / 无 (单机8路)]"] + device_entries

        cur1 = self.dev1_var.get()
        if not cur1 or cur1 not in device_entries:
            self.dev1_var.set(device_entries[0])

        cur2 = self.dev2_var.get()
        if not cur2:
            self.dev2_var.set(device_entries[1] if len(device_entries) >= 2 and device_entries[1] != "无可用设备" else "[未连接 / 无 (单机8路)]")

    def toggle_connection(self):
        if self.is_connected:
            self._disconnect_device()
        else:
            threading.Thread(target=self._bg_connect, daemon=True).start()

    def _extract_sn_or_port(self, text):
        if not text or "无可用" in text or "未连接" in text:
            return None, None
        sn_match = re.search(r'SN:\s*([A-Za-z0-9_]+)', text)
        sn = sn_match.group(1) if sn_match else None
        com_match = re.search(r'(COM\d+)', text, re.IGNORECASE)
        com = com_match.group(1) if com_match else None
        return sn, com

    def _bg_connect(self):
        dev1_txt = self.dev1_var.get()
        dev2_txt = self.dev2_var.get()
        sn1, com1 = self._extract_sn_or_port(dev1_txt)
        sn2, com2 = self._extract_sn_or_port(dev2_txt)

        self.write_log("正在建立设备通信链路...")
        success1 = False
        if sn1 and self.driver and sn1.isdigit():
            self.h_dev1 = self.driver.open_device(int(sn1))
            if self.h_dev1:
                success1 = True
                self.write_log(f"[连接成功] 设备1已挂载 ART-DAQ (SN: {sn1})，绑定 CH1_1 ~ CH1_8。")
        elif com1:
            self.write_log(f"[提示] 设备1串口 {com1} 就绪。")
            success1 = True

        success2 = False
        if sn2 and self.driver and sn2.isdigit():
            self.h_dev2 = self.driver.open_device(int(sn2))
            if self.h_dev2:
                success2 = True
                self.write_log(f"[连接成功] 设备2已挂载 ART-DAQ (SN: {sn2})，绑定 CH2_1 ~ CH2_8。")

        if success1:
            self.root.after(0, self._on_connection_ready, sn1, sn2 if success2 else None)
        else:
            self.write_log("[错误] 未能成功连接设备，请刷新设备列表后重试！")
            self._disconnect_device()

    def _on_connection_ready(self, sn1, sn2):
        self.is_connected = True
        self._update_channel_limits_cache()

        status_text = f"已连接 (SN: {sn1})" if not sn2 else f"已连接 (双机: {sn1}, {sn2})"
        self.lbl_status.config(text=status_text, fg="#34c759")
        self.btn_connect.config(text=" 断开连接 ", bg="#f2f2f7", fg="#007aff")
        self.btn_start.config(state="normal")
        self.dev1_combo.config(state="disabled")
        self.dev2_combo.config(state="disabled")
        self.btn_import.config(state="disabled")
        self.btn_batch.config(state="normal")
        self.write_log(f"[连接就绪] 通信链路建立完毕，当前设定采样间隔: {self.interval_var.get()}")

    def _disconnect_device(self):
        if self.timer_running:
            self.stop_acquisition()
        if self.driver:
            if self.h_dev1:
                self.driver.close_device(self.h_dev1)
                self.h_dev1 = None
            if self.h_dev2:
                self.driver.close_device(self.h_dev2)
                self.h_dev2 = None

        self.is_connected = False
        self.timer_running = False
        self.lbl_status.config(text="未连接", fg="#ff3b30")
        self.btn_connect.config(text=" 连接设备 ", bg="#007aff", fg="white")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="disabled")
        self.dev1_combo.config(state="readonly")
        self.dev2_combo.config(state="readonly")
        self.btn_import.config(state="normal")
        self.btn_batch.config(state="normal")
        self.write_log("已安全断开并释放硬件采集句柄。")

    # ---------------- 变动跟踪与量程处理 ----------------

    def _on_range_changed_trace(self, *args):
        self._schedule_save()
        if getattr(self, "init_completed", False):
            self._update_channel_limits_cache()

    def _on_ratio_changed_trace(self, key):
        self._schedule_save()
        if getattr(self, "init_completed", False):
            self._update_channel_limits_cache()

    def on_range_combobox_selected(self, key):
        if not getattr(self, "init_completed", False): return
        selected_range = self.channel_vars[key]["range"].get()
        targets = [key]
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            self.init_completed = False
            for k in self.selected_keys:
                if k != key:
                    self.channel_vars[k]["range"].set(selected_range)
                    targets.append(k)
            self.init_completed = True

        for k in targets:
            self.pending_optimal_ranges.pop(k, None)
            self.over_channels_to_upgrade.discard(k)
            self.over_ranged_channels.discard(k)
            self.tested_channels.add(k)

        self._schedule_save()
        self._update_channel_limits_cache()

    def _update_channel_limits_cache(self):
        self.is_ma_channel = {}
        self.channel_limits_cache.clear()
        for key in self.channel_keys:
            r_str = self.channel_vars[key]["range"].get()
            ratio_str = self.channel_vars[key]["ratio"].get().strip()
            comment_str = self.channel_vars[key]["comment"].get().strip()

            limit_volt = RANGE_LIMITS_VOLTS.get(r_str, 1.0)
            ratio_val = None
            if ratio_str:
                try:
                    v = float(ratio_str)
                    if abs(v) > 0: ratio_val = v
                except ValueError: pass

            is_curr = is_current_channel(comment_str, ratio_str)
            self.is_ma_channel[key] = is_curr
            self.channel_limits_cache[key] = {
                "limit_volt": limit_volt,
                "limit_volt_thresh": limit_volt * 0.97,
                "ratio_val": ratio_val if ratio_val is not None else 1.0,
                "is_scaled": (ratio_val is not None),
                "is_current": is_curr
            }

    # ---------------- 实时采集与监测工作线程 ----------------

    def start_acquisition(self):
        self.btn_connect.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.btn_batch.config(state="disabled")
        self.total_frames = 0
        self._reset_gui_val_labels()

        # 1. 负均值自动归正极性
        if getattr(self, "pending_polarity_invert", None):
            for key in tuple(self.pending_polarity_invert):
                vars_dict = self.channel_vars[key]
                comment = vars_dict["comment"].get().strip()
                if not comment: continue
                cur_ratio = vars_dict["ratio"].get().strip()
                new_ratio = invert_ratio_sign(cur_ratio)
                vars_dict["ratio"].set(new_ratio)
                self.write_log(f"[极性自动归正] {self.channel_metadata[key]['display']} ({comment}) 实测均值为负，反转变比: {cur_ratio or '1'} → {new_ratio}")
            self.pending_polarity_invert.clear()

        # 2. 应用自适应推荐量程
        if self.pending_optimal_ranges:
            for key, target_range in tuple(self.pending_optimal_ranges.items()):
                vars_dict = self.channel_vars[key]
                comment = vars_dict["comment"].get().strip()
                if not comment: continue
                curr_range = vars_dict["range"].get()
                if target_range != curr_range:
                    vars_dict["range"].set(target_range)
                    self.write_log(f"[量程自适应更新] {self.channel_metadata[key]['display']} ({comment}) 调整至: {target_range} (原: {curr_range})")

        self.over_channels_to_upgrade.clear()
        self.pending_optimal_ranges.clear()
        self.alerted_over_channels.clear()

        self.dt_start = datetime.now()
        self.active_channels = []
        for key in self.channel_keys:
            comment = self.channel_vars[key]["comment"].get().strip()
            if comment:
                meta = self.channel_metadata[key]
                lim_info = self.channel_limits_cache[key]
                self.active_channels.append({
                    "key": key,
                    "unit": meta["unit"],
                    "ch": meta["ch"],
                    "ch_idx": meta["ch"] - 1,
                    "comment": comment,
                    "range": self.channel_vars[key]["range"].get(),
                    "ratio_val": lim_info["ratio_val"],
                    "limit_volt": lim_info["limit_volt"],
                    "limit_volt_thresh": lim_info["limit_volt_thresh"],
                    "is_current": lim_info["is_current"],
                    "is_scaled": lim_info["is_scaled"],
                })

        if not self.active_channels:
            self._abort_acquisition_start("未填写任何有效通道注释，请至少启用一个通道。")
            return

        if not self.driver or not self.driver.dll or not self.h_dev1:
            self._abort_acquisition_start("设备1无有效硬件句柄，无法启动采集。")
            return

        active_by_unit = {
            1: [c for c in self.active_channels if c["unit"] == 1],
            2: [c for c in self.active_channels if c["unit"] == 2],
        }
        if active_by_unit[2] and not self.h_dev2:
            self._abort_acquisition_start("CH2 组启用了通道，但未连接设备2。请连接设备2或清空 CH2 注释。")
            return

        scan_channels_by_unit = {
            1: sorted({c["ch_idx"] for c in active_by_unit[1]}) or list(range(MAX_DIFF_CHANNELS)),
            2: sorted({c["ch_idx"] for c in active_by_unit[2]}) if self.h_dev2 else [],
        }
        for chan in self.active_channels:
            scan_list = scan_channels_by_unit[chan["unit"]]
            chan["scan_idx"] = scan_list.index(chan["ch_idx"]) if chan["ch_idx"] in scan_list else -1

        interval_text = self.interval_var.get().strip()
        max_scan_len = max(len(scan_channels_by_unit[1]), len(scan_channels_by_unit[2]) if self.h_dev2 else 0)
        max_scan_len = max(1, max_scan_len)

        # 硬件始终以物理最高吞吐率全时抗混叠运行，消除不同间隔采样的数据失真
        hardware_scan_rate = MAX_AI_TOTAL_RATE_HZ / max_scan_len
        actual_phys_dt_us = max_scan_len * 4.0

        for key in self.channel_keys:
            self.stats[key].reset(hardware_scan_rate)

        task_buffer_samples = READ_BLOCK_MAX_SAMPLES_PER_CH
        try:
            indices1 = scan_channels_by_unit[1]
            r_codes_1 = [RANGE_CODE_MAP.get(self.channel_vars[f"1-1-{idx + 1}"]["range"].get(), 3) for idx in indices1]
            ok1, actual_rate1, error1 = self.driver.setup_differential_ai(
                self.h_dev1, r_codes_1, channel_indices=indices1,
                rate_hz=hardware_scan_rate, samps_per_chan=task_buffer_samples,
            )
            if not ok1:
                self._abort_acquisition_start(f"设备1硬件任务启动失败：{error1}")
                return

            indices2 = scan_channels_by_unit[2]
            if self.h_dev2 and indices2:
                r_codes_2 = [RANGE_CODE_MAP.get(self.channel_vars[f"1-2-{idx + 1}"]["range"].get(), 3) for idx in indices2]
                ok2, actual_rate2, error2 = self.driver.setup_differential_ai(
                    self.h_dev2, r_codes_2, channel_indices=indices2,
                    rate_hz=hardware_scan_rate, samps_per_chan=task_buffer_samples,
                )
                if not ok2:
                    self._abort_acquisition_start(f"设备2硬件任务启动失败：{error2}")
                    return
        except Exception as exc:
            self._abort_acquisition_start(f"驱动初始化调用异常：{exc}")
            return

        read_chunk = max(4, min(READ_BLOCK_MAX_SAMPLES_PER_CH, (int(actual_rate1 * 0.04) // 4) * 4))
        self.dt_start = datetime.now()
        self.timer_running = True
        self.stop_monitor_event.clear()
        self.start_time = time.monotonic()
        self.btn_stop.config(state="normal")

        self.monitor_thread = threading.Thread(
            target=self._bg_realtime_optimized_worker,
            args=(actual_rate1, read_chunk, scan_channels_by_unit),
            daemon=True,
        )
        self.monitor_thread.start()
        self._update_timer_loop()

        # 精简无歧义日志输出
        self.write_log(
            f"【采样启动】记录模式: {interval_text} | 扫描通道数: {max_scan_len}路差分 | "
            f"采样率: {actual_rate1/1000:.2f} kS/s (单通道周期: {actual_phys_dt_us:.1f}us)"
        )

    def _abort_acquisition_start(self, reason):
        self.timer_running = False
        self.stop_monitor_event.set()
        if self.driver and self.driver.dll:
            for handle in (self.h_dev1, self.h_dev2):
                if handle:
                    try: self.driver.dll.ACTS2200_AI_StopTask(handle)
                    except Exception: pass
                    try: self.driver.dll.ACTS2200_AI_ReleaseTask(handle)
                    except Exception: pass
        self.write_log(f"[启动失败] {reason}")
        self._restore_ui_after_task()
        messagebox.showerror("ART-DAQ 启动错误", reason)

    def _update_timer_loop(self):
        if self.timer_running:
            self.elapsed_time = time.monotonic() - self.start_time
            mins = int(self.elapsed_time // 60)
            secs = int(self.elapsed_time % 60)
            tenths = int((self.elapsed_time * 10) % 10)
            try:
                self.lbl_timer.config(text=f"{mins:02d}:{secs:02d}.{tenths:d}")
                with self.log_lock:
                    vals = dict(self.gui_val_cache)
                    stat_updates = dict(self.gui_stat_cache)
                for k, v in vals.items():
                    if k in self.channel_vars and self.channel_vars[k]["lbl_val"]:
                        self._update_gui_val_label(k, v)
                for k, (avg_v, max_v, min_v) in stat_updates.items():
                    self._update_gui_stat_labels(k, avg_v, max_v, min_v)
            except Exception: pass
            self.root.after(100, self._update_timer_loop)

    def _bg_realtime_optimized_worker(self, sample_rate_per_channel, read_chunk, scan_channels_by_unit):
        scan_counts = {unit: len(scan_channels_by_unit.get(unit, [])) for unit in (1, 2)}
        buf_size1 = max(1, scan_counts[1] * read_chunk)
        buf_size2 = max(1, scan_counts[2] * read_chunk)
        c_buf1 = (ctypes.c_double * buf_size1)()
        c_buf2 = (ctypes.c_double * buf_size2)()
        actual_read1 = ctypes.c_uint32(0)
        actual_read2 = ctypes.c_uint32(0)
        active_chans = self.active_channels
        last_gui_stat_update = time.monotonic()
        read_timeout = max(0.2, (float(read_chunk) / sample_rate_per_channel) * 3.0)
        has_np = (np is not None)

        while self.timer_running and not self.stop_monitor_event.is_set():
            n1 = 0
            n2 = 0
            try:
                if self.h_dev1 and scan_counts[1] > 0:
                    actual_read1.value = 0
                    ret1 = self.driver.dll.ACTS2200_AI_ReadAnalog(
                        self.h_dev1, c_buf1, read_chunk, ctypes.byref(actual_read1), read_timeout, 0
                    )
                    if ret1 and actual_read1.value > 0:
                        n1 = min(int(actual_read1.value), read_chunk)

                if self.h_dev2 and scan_counts[2] > 0:
                    actual_read2.value = 0
                    ret2 = self.driver.dll.ACTS2200_AI_ReadAnalog(
                        self.h_dev2, c_buf2, read_chunk, ctypes.byref(actual_read2), read_timeout, 0
                    )
                    if ret2 and actual_read2.value > 0:
                        n2 = min(int(actual_read2.value), read_chunk)
            except Exception as exc:
                self.write_log(f"[采集异常中断] {exc}")
                self.root.after(0, self.stop_acquisition)
                break

            if n1 == 0 and n2 == 0:
                self.stop_monitor_event.wait(0.001)
                continue

            curr_vals = {}
            mat1 = np.ctypeslib.as_array(c_buf1)[:n1 * scan_counts[1]].reshape((n1, scan_counts[1])) if (has_np and n1 > 0) else None
            mat2 = np.ctypeslib.as_array(c_buf2)[:n2 * scan_counts[2]].reshape((n2, scan_counts[2])) if (has_np and n2 > 0) else None

            for chan in active_chans:
                unit = chan["unit"]
                scan_idx = chan.get("scan_idx", -1)
                if scan_idx < 0: continue
                n_avail = n1 if unit == 1 else n2
                if n_avail <= 0: continue

                k = chan["key"]
                ratio_val = chan["ratio_val"]
                is_scaled = chan["is_scaled"]
                limit_volt = chan["limit_volt"]
                thresh = chan["limit_volt_thresh"]
                st = self.stats[k]

                if has_np:
                    mat = mat1 if unit == 1 else mat2
                    col = mat[:, scan_idx]
                    calc_col = col * ratio_val if is_scaled else col
                    st.add_block(calc_col)
                    curr_vals[k] = float(st.y2)
                    peak_abs = float(np.max(np.abs(col)))
                    raw_last = float(col[-1])
                else:
                    buf = c_buf1 if unit == 1 else c_buf2
                    scan_width = scan_counts[unit]
                    peak_abs = 0.0
                    raw_last = 0.0
                    for s_i in range(n_avail):
                        raw_v = float(buf[s_i * scan_width + scan_idx])
                        if not math.isfinite(raw_v): continue
                        calc_v = raw_v * ratio_val if is_scaled else raw_v
                        st.add(calc_v)
                        abs_raw = abs(raw_v)
                        if abs_raw > peak_abs: peak_abs = abs_raw
                        raw_last = raw_v
                    curr_vals[k] = float(st.y2)

                if peak_abs >= limit_volt:
                    st.mark_over()
                    if k not in self.alerted_over_channels:
                        self.alerted_over_channels.add(k)
                        self.over_channels_to_upgrade.add(k)
                        self.over_ranged_channels.add(k)
                        target_r = RANGE_NEXT_MAP.get(chan["range"], chan["range"])
                        self.pending_optimal_ranges[k] = target_r
                        self.write_log(f"[量程饱和] {self.channel_metadata[k]['display']} 输入 {raw_last:.3g}V 达到上限，已规划升档: {target_r}")
                elif peak_abs > thresh:
                    if k not in self.alerted_over_channels:
                        self.alerted_over_channels.add(k)
                        self.over_channels_to_upgrade.add(k)
                        self.over_ranged_channels.add(k)
                        self.pending_optimal_ranges[k] = RANGE_NEXT_MAP.get(chan["range"], chan["range"])

            self.total_frames += 1
            if curr_vals:
                with self.log_lock:
                    self.gui_val_cache.update(curr_vals)

            now = time.monotonic()
            if now - last_gui_stat_update >= 0.25:
                stat_snap = {chan["key"]: (self.stats[chan["key"]].avg, self.stats[chan["key"]].max_val, self.stats[chan["key"]].min_val) for chan in active_chans}
                with self.log_lock:
                    self.gui_stat_cache.update(stat_snap)
                last_gui_stat_update = now

    def _update_gui_val_label(self, key, val):
        lbl = self.channel_vars[key]["lbl_val"]
        row_bg = self.channel_vars[key]["row_bg"]
        if not lbl: return
        if val is None:
            text, fg, bg = "--", "#8e8e93", row_bg
        elif math.isnan(val):
            text, fg, bg = "OVER", "#ffffff", "#ff3b30"
        else:
            is_ma = self.is_ma_channel.get(key, False)
            fg = "#ff3b30" if val < 0 else ("#34c759" if is_ma else "#007aff")
            text = f"{val:.3f} mA" if is_ma else f"{val:.4f} V"
            bg = row_bg

        cache_k = f"{key}_val"
        if self._lbl_text_cache.get(cache_k) != text:
            lbl.config(text=text, fg=fg, bg=bg)
            self._lbl_text_cache[cache_k] = text

    def _update_gui_stat_labels(self, key, avg, max_val, min_val):
        is_ma = self.is_ma_channel.get(key, False)
        fmt = ".3f" if is_ma else ".4f"
        unit = " mA" if is_ma else " V"
        row_bg = self.channel_vars[key]["row_bg"]

        for lbl_key, v in (("lbl_avg", avg), ("lbl_max", max_val), ("lbl_min", min_val)):
            lbl = self.channel_vars[key][lbl_key]
            if not lbl: continue
            text = "--" if (v is None or math.isnan(v)) else f"{v:{fmt}}{unit}"
            fg = "#8e8e93" if (v is None or math.isnan(v)) else ("#ff3b30" if v < 0 else "#1c1c1e")
            cache_k = f"{key}_{lbl_key}"
            if self._lbl_text_cache.get(cache_k) != text:
                lbl.config(text=text, fg=fg, bg=row_bg)
                self._lbl_text_cache[cache_k] = text

    def _batch_update_gui_stats(self, stats_list):
        for k, avg_val, max_val, min_val in stats_list:
            if k in self.channel_vars:
                self._update_gui_stat_labels(k, avg_val, max_val, min_val)

    def stop_acquisition(self):
        """点击停止采集，主线程以 0 毫秒极速就地完成统计并推入 Windows 系统剪贴板。"""
        self.timer_running = False
        self.stop_monitor_event.set()
        try: self.btn_stop.config(state="disabled")
        except Exception: pass
        self.dt_end = datetime.now()

        # 【核心时序重构】：主线程瞬间完成数据统计并秒推系统剪贴板，彻底消除 Alt+Tab 粘贴空白
        self._save_collected_data()

        self.write_log(">>> 采集结束，正在释放硬件连接句柄...")
        threading.Thread(target=self._bg_stop_and_save_task, daemon=True).start()

    def _bg_stop_and_save_task(self):
        try:
            if self.monitor_thread and self.monitor_thread.is_alive():
                self.monitor_thread.join(timeout=0.6)
            self.monitor_thread = None

            if self.driver:
                for h in (self.h_dev1, self.h_dev2):
                    if h:
                        try:
                            self.driver.dll.ACTS2200_AI_StopTask(h)
                            self.driver.dll.ACTS2200_AI_ReleaseTask(h)
                        except Exception: pass
        except Exception as e:
            self.write_log(f"[释放句柄异常] {e}")
        finally:
            self.root.after(0, self._restore_ui_after_task)

    def _save_collected_data(self):
        active_channels = getattr(self, "active_channels", [])
        if not active_channels: return

        elapsed_sec = (self.dt_end - self.dt_start).total_seconds() if (self.dt_start and self.dt_end) else 0.0
        self.write_log(
            f"【采集完成】测试时长: {elapsed_sec:.3f}s | 设定记录间隔: {self.interval_var.get()} | 累计读取: {self.total_frames} 帧"
        )

        stats_final_updates = []
        stats = {}
        for chan in active_channels:
            k = chan["key"]
            st = self.stats[k]
            avg_val = st.avg if st.count > 0 else math.nan
            max_val = st.max_val if st.count > 0 else math.nan
            min_val = st.min_val if st.count > 0 else math.nan

            stats_final_updates.append((k, avg_val, max_val, min_val))
            stats[k] = {"avg": avg_val, "max": max_val, "min": min_val}

            # 极性自动反转判断
            if not math.isnan(avg_val) and avg_val < -1e-4:
                self.pending_polarity_invert.add(k)
            else:
                self.pending_polarity_invert.discard(k)

            # 电压档位自适应推荐
            cur_range = chan["range"]
            if not chan["is_current"] and (k not in self.over_channels_to_upgrade) and not math.isnan(avg_val):
                v_abs = abs(avg_val)
                if v_abs < 0.05:
                    self.pending_optimal_ranges.pop(k, None)
                else:
                    if v_abs <= 1.0: target_r = "±1V"
                    elif v_abs <= 2.0: target_r = "±2V"
                    elif v_abs <= 5.0: target_r = "±5V"
                    else: target_r = "±10V"
                    if target_r != cur_range:
                        self.pending_optimal_ranges[k] = target_r
                        self.write_log(f"[量程自适应规划] {self.channel_metadata[k]['display']} 均值 {avg_val:.3f}V，下次将切换至: {target_r}")
                    else:
                        self.pending_optimal_ranges.pop(k, None)

        self.last_test_stats = dict(stats)
        self._batch_update_gui_stats(stats_final_updates)

        if self.pending_optimal_ranges:
            for k, tgt_r in self.pending_optimal_ranges.items():
                self.channel_vars[k]["range"].set(tgt_r)
            self._save_timer_id = self.root.after(100, self.save_local_config)

        # 构建完全对齐日置采集仪软件格式的剪贴板纯文本
        date_trigger = f"{self.dt_start.year}/{self.dt_start.month:02d}/{self.dt_start.day:02d}"
        time_trigger_sec = self.dt_start.strftime("%H:%M:%S") + "s"
        time_trigger_ms = self.dt_start.strftime("%H:%M:%S.%f")[:-4] + "s ~"
        date_end = f"{self.dt_end.year}/{self.dt_end.month:02d}/{self.dt_end.day:02d}"
        time_end_ms = self.dt_end.strftime("%H:%M:%S.%f")[:-4] + "s"

        lines = [
            f"触发时间\t{date_trigger}\t{time_trigger_sec}",
            f"运算范围\t{date_trigger}\t{time_trigger_ms}\t\t{date_end}\t{time_end_ms}",
            "Channel\t平均值\t最大值\t最小值"
        ]
        active_keys = {c["key"] for c in active_channels}
        for key in self.channel_keys:
            if key in active_keys:
                st_info = stats.get(key)
                if not st_info or math.isnan(st_info["avg"]):
                    fmt_str = f"{key}\tOVER\tOVER\tOVER"
                else:
                    fmt_str = f"{key}\t{st_info['avg']:.3f}\t{st_info['max']:.3f}\t{st_info['min']:.3f}"
                lines.append(fmt_str)

        clipboard_text = "\n".join(lines)
        self._copy_to_clipboard(clipboard_text)

    def _copy_to_clipboard(self, text):
        self.last_clipboard_text = text
        # 优先使用 Windows 原生 Win32 API 直接写入全局内存
        if set_system_clipboard_native(text):
            self.write_log("[自动复制] 统计结果已写入系统剪贴板（可在 Excel 中直接 Ctrl+V 粘贴分列）")
            return

        # 跨平台兜底
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            self.write_log("[自动复制] 统计结果已写入系统剪贴板（可在 Excel 中直接 Ctrl+V 粘贴分列）")
        except Exception: pass

    def copy_clipboard_data(self):
        if getattr(self, "last_clipboard_text", ""):
            if set_system_clipboard_native(self.last_clipboard_text):
                self.write_log("[手动复制] 测试统计数据已写入系统剪贴板")
                return
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(self.last_clipboard_text)
                self.write_log("[手动复制] 测试统计数据已写入系统剪贴板")
            except Exception: pass
        else:
            messagebox.showwarning("复制失败", "当前无有效统计数据，请先执行一次有效采集。")

    def _reset_gui_val_labels(self):
        self._lbl_text_cache.clear()
        for key in self.channel_keys:
            cv = self.channel_vars[key]
            if cv["lbl_val"]: cv["lbl_val"].config(text="--", fg="#8e8e93")
            if cv["lbl_avg"]: cv["lbl_avg"].config(text="--", fg="#8e8e93")
            if cv["lbl_max"]: cv["lbl_max"].config(text="--", fg="#8e8e93")
            if cv["lbl_min"]: cv["lbl_min"].config(text="--", fg="#8e8e93")

    def _restore_ui_after_task(self):
        self.btn_connect.config(state="normal")
        self.btn_start.config(state="normal" if self.is_connected else "disabled")
        self.btn_stop.config(state="disabled")
        self.dev1_combo.config(state="readonly" if not self.is_connected else "disabled")
        self.dev2_combo.config(state="readonly" if not self.is_connected else "disabled")
        self.interval_combo.config(state="readonly")
        self.btn_import.config(state="normal" if not self.is_connected else "disabled")
        self.btn_batch.config(state="normal")

    # ---------------- 集中配置窗口 ----------------

    def open_batch_config_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("复制通道参数")
        dialog.geometry("540x650")
        dialog.configure(bg="#ffffff")
        dialog.grab_set()
        dialog.transient(self.root)

        param_vars = {
            "comment": tk.BooleanVar(value=False),
            "range": tk.BooleanVar(value=True),
            "ratio": tk.BooleanVar(value=True),
        }

        source_options = []
        for unit in (1, 2):
            for ch in range(1, 9):
                key = f"1-{unit}-{ch}"
                comment = self.channel_vars[key]["comment"].get().strip()
                source_options.append(f"CH {unit}-{ch} ({comment})" if comment else f"CH {unit}-{ch}")

        source_var = tk.StringVar(value=source_options[0])

        src_top_frame = tk.Frame(dialog, bg="#ffffff")
        src_top_frame.pack(fill="x", padx=15, pady=12)

        tk.Label(src_top_frame, text="复制源:", font=("Microsoft YaHei UI", 10, "bold"), fg="#007aff", bg="#ffffff").pack(side="left", padx=5)
        src_combo = ttk.Combobox(src_top_frame, textvariable=source_var, values=source_options, font=("Segoe UI", 10), state="readonly", width=40)
        src_combo.pack(side="right", padx=10, fill="x", expand=True)

        param_frame = tk.LabelFrame(dialog, text=" 独立复制参数选择 ", font=("Microsoft YaHei UI", 10, "bold"), bg="#f2f2f7", fg="#1c1c1e", bd=1, relief="solid")
        param_frame.pack(fill="x", padx=15, pady=5)

        chk_container = tk.Frame(param_frame, bg="#ffffff", bd=0)
        chk_container.pack(side="left", fill="both", expand=True, padx=15, pady=10)

        tk.Checkbutton(chk_container, text="首注释", variable=param_vars["comment"], bg="#ffffff", fg="#1c1c1e", font=("Microsoft YaHei UI", 10), anchor="w").pack(fill="x", padx=15, pady=3)
        tk.Checkbutton(chk_container, text="硬件量程", variable=param_vars["range"], bg="#ffffff", fg="#1c1c1e", font=("Microsoft YaHei UI", 10), anchor="w").pack(fill="x", padx=15, pady=3)
        tk.Checkbutton(chk_container, text="转换比", variable=param_vars["ratio"], bg="#ffffff", fg="#1c1c1e", font=("Microsoft YaHei UI", 10), anchor="w").pack(fill="x", padx=15, pady=3)

        btn_param_sidebar = tk.Frame(param_frame, bg="#f2f2f7")
        btn_param_sidebar.pack(side="right", fill="y", padx=15, pady=10)

        btn_style = {"font": ("Microsoft YaHei UI", 9), "relief": "flat", "bd": 0, "width": 11, "bg": "#f2f2f7", "fg": "#007aff", "cursor": "hand2"}
        tk.Button(btn_param_sidebar, text="选择全部", command=lambda: [v.set(True) for v in param_vars.values()], **btn_style).pack(pady=3)
        tk.Button(btn_param_sidebar, text="取消选择", command=lambda: [v.set(False) for v in param_vars.values()], **btn_style).pack(pady=3)
        tk.Button(btn_param_sidebar, text="反选", command=lambda: [v.set(not v.get()) for v in param_vars.values()], **btn_style).pack(pady=3)

        target_lf = tk.LabelFrame(dialog, text=" 复制目标通道 ", font=("Microsoft YaHei UI", 10, "bold"), bg="#f2f2f7", fg="#1c1c1e", bd=1, relief="solid")
        target_lf.pack(fill="both", expand=True, padx=15, pady=5)

        list_container = tk.Frame(target_lf, bg="#f2f2f7")
        list_container.pack(side="left", fill="both", expand=True, padx=15, pady=10)

        scrollbar = ttk.Scrollbar(list_container, orient="vertical")
        target_listbox = tk.Listbox(
            list_container, selectmode="extended", yscrollcommand=scrollbar.set,
            font=("Segoe UI", 10), bd=0, highlightthickness=0, bg="#ffffff", fg="#1c1c1e",
            selectbackground="#007aff", selectforeground="#ffffff"
        )
        scrollbar.config(command=target_listbox.yview)
        target_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for unit in (1, 2):
            for ch in range(1, 9):
                key = f"1-{unit}-{ch}"
                comment_text = self.channel_vars[key]["comment"].get().strip()
                target_listbox.insert(tk.END, f"CH {unit}-{ch} ({comment_text})" if comment_text else f"CH {unit}-{ch}")

        btn_target_sidebar = tk.Frame(target_lf, bg="#f2f2f7")
        btn_target_sidebar.pack(side="right", fill="y", padx=15, pady=10)

        tk.Button(btn_target_sidebar, text="选择全部", command=lambda: target_listbox.select_set(0, tk.END), **btn_style).pack(pady=3)
        tk.Button(btn_target_sidebar, text="取消选择", command=lambda: target_listbox.select_clear(0, tk.END), **btn_style).pack(pady=3)

        def invert_target_selection():
            for i in range(target_listbox.size()):
                if target_listbox.selection_includes(i): target_listbox.select_clear(i)
                else: target_listbox.select_set(i)
        tk.Button(btn_target_sidebar, text="反选", command=invert_target_selection, **btn_style).pack(pady=3)

        bottom_bar = tk.Frame(dialog, bg="#ffffff")
        bottom_bar.pack(fill="x", padx=15, pady=15)

        def commit_batch_copy():
            src_display = source_var.get()
            try:
                parts = src_display.split(" ")[1].split("-")
                src_key = f"1-{parts[0]}-{parts[1]}"
            except Exception:
                messagebox.showerror("错误", "无法解析源通道")
                return

            if not any(v.get() for v in param_vars.values()):
                messagebox.showwarning("提示", "请至少勾选一项要复制的参数！")
                return

            selected_indices = target_listbox.curselection()
            if not selected_indices:
                messagebox.showwarning("提示", "请至少选择一个目标通道！")
                return

            self.init_completed = False
            for idx in selected_indices:
                t_key = self.channel_keys[idx]
                if t_key == src_key: continue
                if param_vars["comment"].get():
                    self.channel_vars[t_key]["comment"].set(self.channel_vars[src_key]["comment"].get())
                if param_vars["range"].get():
                    self.channel_vars[t_key]["range"].set(self.channel_vars[src_key]["range"].get())
                if param_vars["ratio"].get():
                    self.channel_vars[t_key]["ratio"].set(self.channel_vars[src_key]["ratio"].get())

            self.init_completed = True
            self.save_local_config()
            self._update_channel_limits_cache()
            self.write_log(f"[集中配置] 已将源通道 [{src_display.split(' ')[1]}] 的参数成功覆盖应用至 {len(selected_indices)} 个通道。")
            dialog.destroy()

        tk.Button(bottom_bar, text=" 📋 应用复制 ", bg="#007aff", fg="white", font=("Microsoft YaHei UI", 10, "bold"), relief="flat", width=12, command=commit_batch_copy, cursor="hand2").pack(side="right", padx=10)
        tk.Button(bottom_bar, text=" 取消 ", bg="#f2f2f7", fg="#1c1c1e", font=("Microsoft YaHei UI", 10), relief="flat", width=10, command=dialog.destroy, cursor="hand2").pack(side="right", padx=5)

    # ---------------- 配置文件存储与恢复 ----------------

    def import_config_file(self):
        filepath = filedialog.askopenfilename(title="导入通道配置文件", filetypes=[("JSON Config Files", "*.json")])
        if filepath:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    self._apply_config_dict(config)
                self.save_local_config()
                self.write_log(f"[配置导入] 成功导入: {os.path.basename(filepath)}")
            except Exception as e:
                self.write_log(f"[配置导入失败] {e}")

    def export_config_file(self):
        filepath = filedialog.asksaveasfilename(title="导出通道配置文件", defaultextension=".json", filetypes=[("JSON Config Files", "*.json")])
        if filepath:
            try:
                config = self._generate_config_dict()
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
                self.write_log(f"[配置导出] 成功保存: {os.path.basename(filepath)}")
            except Exception as e:
                self.write_log(f"[配置导出失败] {e}")

    def _apply_config_dict(self, config):
        if "device1" in config: self.dev1_var.set(config.get("device1", ""))
        if "device2" in config: self.dev2_var.set(config.get("device2", "[未连接 / 无 (单机8路)]"))
        if "interval" in config:
            inv = config.get("interval", "4us")
            self.interval_var.set(inv if inv in INTERVAL_LIST else "4us")
        if "soc" in config: self.case_name.set(config.get("soc", ""))

        channels_data = config.get("channels", {})
        for key, val in channels_data.items():
            if key in self.channel_vars:
                self.channel_vars[key]["comment"].set(val.get("comment", ""))
                rng = val.get("range", "±1V")
                self.channel_vars[key]["range"].set(rng if rng in RANGE_LIST else "±1V")
                self.channel_vars[key]["ratio"].set(str(val.get("ratio", "")).strip())
        self._update_channel_limits_cache()

    def _generate_config_dict(self):
        config = {
            "device1": self.dev1_var.get(),
            "device2": self.dev2_var.get(),
            "interval": self.interval_var.get(),
            "soc": self.case_name.get().strip(),
            "channels": {}
        }
        for key, vars_dict in self.channel_vars.items():
            config["channels"][key] = {
                "comment": vars_dict["comment"].get().strip(),
                "range": vars_dict["range"].get(),
                "ratio": vars_dict["ratio"].get().strip()
            }
        return config

    def save_local_config(self):
        try:
            config = self._generate_config_dict()
            with open("artdaq_config.json", "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception: pass

    def load_local_config(self):
        for fname in ["artdaq_config.json", "hioki_config.json"]:
            if os.path.exists(fname):
                try:
                    with open(fname, "r", encoding="utf-8") as f:
                        self._apply_config_dict(json.load(f))
                    return
                except Exception: pass

    def _schedule_save(self, *args):
        if not getattr(self, "init_completed", False): return
        if self._save_timer_id is not None:
            self.root.after_cancel(self._save_timer_id)
        self._save_timer_id = self.root.after(300, self.save_local_config)

    def on_close_save(self):
        self.save_local_config()
        self._disconnect_device()
        self.root.destroy()

    # ---------------- 多选拖拽与剪贴板处理 ----------------

    def on_drag_start(self, key, event):
        self.is_dragging = True
        self.drag_start_key = key
        self.drag_last_key = key
        if event.state & 0x0004:
            if key in self.selected_keys: self.selected_keys.remove(key)
            else: self.selected_keys.add(key)
            self.drag_base_selection = set(self.selected_keys)
        elif event.state & 0x0001:
            if not self.last_selected_key: self.last_selected_key = key
            self._select_range(self.last_selected_key, key)
            self.drag_base_selection = set(self.selected_keys)
        else:
            self.selected_keys = {key}
            self.last_selected_key = key
            self.drag_base_selection = {key}
        self._update_selection_visuals()

    def on_drag_motion(self, event):
        if not self.is_dragging: return
        w = self.root.winfo_containing(event.x_root, event.y_root)
        if w in self.widget_to_key:
            curr_key = self.widget_to_key[w]
            if curr_key != self.drag_last_key:
                self.drag_last_key = curr_key
                self._select_range(self.drag_start_key, curr_key, base_selection=self.drag_base_selection)

    def on_drag_end(self, event):
        self.is_dragging = False

    def _select_range(self, start_key, end_key, base_selection=None):
        try:
            s_idx = self.channel_keys.index(start_key)
            e_idx = self.channel_keys.index(end_key)
            low, high = min(s_idx, e_idx), max(s_idx, e_idx)
            rng = set(self.channel_keys[low:high + 1])
            self.selected_keys = set(base_selection).union(rng) if base_selection else rng
            self._update_selection_visuals()
        except ValueError: pass

    def _update_selection_visuals(self):
        for key in self.channel_keys:
            cv = self.channel_vars[key]
            bg_c = "#e0f2fe" if key in self.selected_keys else cv["row_bg"]
            if cv["ent_comment"]: cv["ent_comment"].config(bg=bg_c)
            if cv["ent_ratio"]: cv["ent_ratio"].config(bg=bg_c)
            if cv["lbl_ch"]: cv["lbl_ch"].config(bg=bg_c)

    def on_comment_keyrelease(self, key, event):
        self._sync_selected_values(key, event, "comment")

    def on_ratio_keyrelease(self, key, event):
        self._sync_selected_values(key, event, "ratio")

    def _sync_selected_values(self, key, event, field_name):
        if (event.state & 0x0004) or (event.state & 0x0008) or (event.keysym in IGNORED_NAV_KEYSYMS):
            return
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            val = self.channel_vars[key][field_name].get()
            for k in self.selected_keys:
                if k != key:
                    self.channel_vars[k][field_name].set(val)
            self._schedule_save()

    def on_comment_copy(self, key, event):
        self._handle_copy_sync(key, "comment")
        return None

    def on_ratio_copy(self, key, event):
        self._handle_copy_sync(key, "ratio")
        return None

    def _handle_copy_sync(self, key, field_name):
        targets = [k for k in self.channel_keys if k in self.selected_keys] if len(self.selected_keys) > 1 else [key]
        text = "\n".join([self.channel_vars[k][field_name].get() for k in targets])
        if not set_system_clipboard_native(text):
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
            except Exception: pass

    def on_comment_paste(self, key, event):
        return self._handle_paste_sync(key, "comment", event)

    def on_ratio_paste(self, key, event):
        return self._handle_paste_sync(key, "ratio", event)

    def _handle_paste_sync(self, key, field_name, event):
        try: clip = self.root.clipboard_get()
        except Exception: return None
        lines = [line.strip() for line in clip.splitlines() if line.strip()]
        if not lines: return None
        if len(lines) > 1:
            try:
                start_idx = self.channel_keys.index(key)
                for i, l_val in enumerate(lines):
                    target_idx = start_idx + i
                    if target_idx < len(self.channel_keys):
                        self.channel_vars[self.channel_keys[target_idx]][field_name].set(l_val)
                self._schedule_save()
                return "break"
            except ValueError: pass
        elif len(self.selected_keys) > 1 and key in self.selected_keys:
            for k in self.selected_keys:
                self.channel_vars[k][field_name].set(lines[0])
            self._schedule_save()
            return "break"
        return None

    def on_comment_delete(self, key, event):
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            for k in self.selected_keys:
                self.channel_vars[k]["comment"].set("")
            self._schedule_save()
            return "break"
        return None

    def on_ratio_delete(self, key, event):
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            for k in self.selected_keys:
                self.channel_vars[k]["ratio"].set("")
            self._schedule_save()
            return "break"
        return None

    def write_log(self, msg):
        now_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with self.log_lock:
            self.log_queue.append(f"[{now_str}] {msg}")

    def _flush_log_queue(self):
        msgs = []
        with self.log_lock:
            while self.log_queue:
                msgs.append(self.log_queue.popleft())
        if msgs:
            self.log_area.insert(tk.END, "\n".join(msgs) + "\n")
            self.log_area.see(tk.END)
        self.root.after(100, self._flush_log_queue)


if __name__ == "__main__":
    root = tk.Tk()
    app = ArtDaqApp(root)
    root.mainloop()
