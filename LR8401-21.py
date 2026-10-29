"""
HIOKI LR8401-21 / LR8450 自动化测试控制台 v23.0 (64位原生安全·零延迟秒粘贴版)
"""

import sys
import os
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
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "缺少运行依赖", 
        "请先在终端运行以下命令安装依赖：\npip install pyserial"
    )
    sys.exit()

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

# 映射日置电压量程至 SCPI 浮点参数及标准枚举字面量 (LR8401 / LR8450 统一标准)
RANGE_MAP = {
    "10mV": "0.01",
    "20mV": "0.02",
    "100mV": "0.1",
    "200mV": "0.2",
    "1V": "1.0",
    "2V": "2.0",
    "10V": "10.0",
    "20V": "20.0",
    "100V": "100.0"
}
RANGE_SCPI_ENUM = {
    "10mV": "10MV",
    "20mV": "20MV",
    "100mV": "100MV",
    "200mV": "200MV",
    "1V": "1V",
    "2V": "2V",
    "10V": "10V",
    "20V": "20V",
    "100V": "100V"
}
RANGE_LIST = list(RANGE_MAP.keys())
RANGE_INDEX = {r: i for i, r in enumerate(RANGE_LIST)}
RANGE_NUMERIC_MAP = {float(v): k for k, v in RANGE_MAP.items()}
RANGE_NEXT_MAP = {current: RANGE_LIST[i + 1] for i, current in enumerate(RANGE_LIST[:-1])}
RANGE_LIMITS_VOLTS = {k: float(v) for k, v in RANGE_MAP.items()}

# 预计算量程门限常量，消除循环内部重复浮点运算
CURRENT_THRESHOLDS = (
    (0.010 * 0.97, "10mV"),
    (0.020 * 0.97, "20mV"),
    (0.100 * 0.97, "100mV"),
    (0.200 * 0.97, "200mV"),
    (1.000 * 0.97, "1V"),
    (2.000 * 0.97, "2V"),
    (10.000 * 0.97, "10V"),
    (20.000 * 0.97, "20V"),
)

INTERVAL_SCPI_MAP = {
    "10ms": {"sec": "0.01", "enum": "10MS", "sec_num": 0.01},
    "20ms": {"sec": "0.02", "enum": "20MS", "sec_num": 0.02},
    "50ms": {"sec": "0.05", "enum": "50MS", "sec_num": 0.05},
    "100ms": {"sec": "0.1", "enum": "100MS", "sec_num": 0.1},
    "200ms": {"sec": "0.2", "enum": "200MS", "sec_num": 0.2},
    "500ms": {"sec": "0.5", "enum": "500MS", "sec_num": 0.5},
    "1s": {"sec": "1.0", "enum": "1S", "sec_num": 1.0},
    "2s": {"sec": "2.0", "enum": "2S", "sec_num": 2.0},
    "5s": {"sec": "5.0", "enum": "5S", "sec_num": 5.0},
    "10s": {"sec": "10.0", "enum": "10S", "sec_num": 10.0},
}

# 预编译标签与按键静态集合，实现 O(1) 瞬时查找与零重复分配
CURRENT_TAGS = ("(MA)", "[MA]", "_MA", " MA", "CUR", "CURRENT", "SHUNT")
VOLTAGE_TAGS = ("(V)", "[V]", "_V", " V", "VOLT", "STANDBY", "VDD", "VCC", "AVDD", "VDDIO")
IGNORED_RATIO_LITERALS = frozenset(("1", "+1", "-1", "0", "+0", "-0"))
INTERVAL_SECONDS_MAP = {k: v["sec_num"] for k, v in INTERVAL_SCPI_MAP.items()}
IGNORED_NAV_KEYSYMS = frozenset((
    "Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R",
    "Caps_Lock", "Tab", "Escape", "Return", "Left", "Right", "Up", "Down",
    "Home", "End", "Prior", "Next", "Insert", "Pause", "Print", "Num_Lock",
    "Scroll_Lock", "Menu"
))
GMEM_MOVEABLE = 0x0002
CF_UNICODETEXT = 13


def parse_interval_readback_sec(res):
    if not res:
        return None
    s = str(res).strip().strip('"')
    if " " in s:
        s = s.rsplit(" ", 1)[-1].strip('"')
    u = s.upper().replace(" ", "")
    if "MS" in u:
        try:
            return float(u.replace("MS", "")) / 1000.0
        except ValueError:
            pass
    elif "S" in u:
        try:
            return float(u.replace("S", ""))
        except ValueError:
            pass
    try:
        return float(u)
    except ValueError:
        return None


class ChannelStats:
    """
    高紧凑、零字典开销的高性能统计累加器
    采用 Neumaier 补偿求和算法，绝对消除长时间浮点累加漂移
    """
    __slots__ = ('sum', 'comp', 'count', 'max_val', 'min_val', 'raw_peak', 'over_count')

    def __init__(self):
        self.reset()

    def reset(self):
        self.sum = 0.0
        self.comp = 0.0
        self.count = 0
        self.max_val = -float('inf')
        self.min_val = float('inf')
        self.raw_peak = 0.0
        self.over_count = 0

    def add(self, val, raw_volt):
        self.count += 1
        t = self.sum + val
        if abs(self.sum) >= abs(val):
            self.comp += (self.sum - t) + val
        else:
            self.comp += (val - t) + self.sum
        self.sum = t

        if val > self.max_val:
            self.max_val = val
        if val < self.min_val:
            self.min_val = val
        if raw_volt > self.raw_peak:
            self.raw_peak = raw_volt

    def mark_over(self):
        self.over_count += 1

    @property
    def avg(self):
        return (self.sum + self.comp) / self.count if self.count > 0 else math.nan


def clean_scpi_response(res_str):
    if not res_str:
        return ""
    res_str = str(res_str).strip()
    if " " in res_str:
        parts = res_str.split(" ", 1)
        if parts[0].startswith(":") or (parts[0].isalpha() and len(parts[0]) > 4 and parts[0].upper() not in ("OVER", "NAN", "INF", "O.R")):
            return parts[1]
    return res_str


def parse_voltage_range_volts(val_str):
    if val_str is None:
        return None
    cleaned = clean_scpi_response(str(val_str)).strip().strip('"')
    if "," in cleaned:
        cleaned = cleaned.rsplit(",", 1)[-1].strip().strip('"')
    norm = cleaned.upper().replace(" ", "")
    if not norm:
        return None

    if norm.endswith("MV"):
        try:
            return float(norm[:-2]) * 0.001
        except ValueError:
            pass
    elif norm.endswith("V"):
        try:
            return float(norm[:-1])
        except ValueError:
            pass
    try:
        return float(norm)
    except ValueError:
        pass
    return None


def format_range_for_log(value):
    v = parse_voltage_range_volts(value)
    if v is None:
        return "无响应" if not value else str(value).strip()
    for expected_val, label in RANGE_NUMERIC_MAP.items():
        if abs(v - expected_val) <= max(abs(expected_val) * 1e-3, 1e-6):
            return label
    if v < 1.0:
        mv_text = f"{v * 1000:.6f}".rstrip("0").rstrip(".")
        return f"{mv_text}mV"
    volt_text = f"{v:.6f}".rstrip("0").rstrip(".")
    return f"{volt_text}V"


def is_scpi_range_match(response, expected):
    v_actual = parse_voltage_range_volts(response)
    v_expected = parse_voltage_range_volts(expected)
    if v_actual is None or v_expected is None:
        return False
    tolerance = max(abs(v_expected) * 1e-3, 1e-6)
    return abs(v_actual - v_expected) <= tolerance


def parse_interval_seconds(interval_text):
    if interval_text in INTERVAL_SECONDS_MAP:
        return INTERVAL_SECONDS_MAP[interval_text]
    text = str(interval_text).strip().lower()
    if text in INTERVAL_SECONDS_MAP:
        return INTERVAL_SECONDS_MAP[text]
    try:
        if text.endswith("ms"):
            val = float(text[:-2])
            if val <= 0: raise ValueError
            return val * 0.001
        if text.endswith("s"):
            val = float(text[:-1])
            if val <= 0: raise ValueError
            return val
        val = float(text)
        if val <= 0: raise ValueError
        return val
    except (TypeError, ValueError):
        raise ValueError(f"非法采样间隔: {interval_text}")


def get_scpi_range(range_text):
    return RANGE_MAP.get(range_text, "0.01")


def get_scpi_enum_range(range_text):
    return RANGE_SCPI_ENUM.get(range_text, range_text.upper())


def get_next_range(current_range):
    return RANGE_NEXT_MAP.get(current_range, current_range)


def _build_range_command_args(range_text):
    scpi_range = get_scpi_range(range_text)
    enum_range = get_scpi_enum_range(range_text)
    candidates = [("numeric", scpi_range)]
    try:
        candidates.append(("nr3", f"{float(scpi_range):.1E}"))
    except ValueError:
        pass
    candidates.append(("enum", enum_range))
    candidates.append(("label", range_text))

    deduped = []
    seen = set()
    for kind, arg in candidates:
        normalized = arg.upper().replace(" ", "")
        if normalized not in seen:
            seen.add(normalized)
            deduped.append((kind, arg))
    return deduped

RANGE_COMMAND_ARGS_CACHE = {r: _build_range_command_args(r) for r in RANGE_LIST}

def get_range_command_args(range_text, is_lr8450=False):
    return RANGE_COMMAND_ARGS_CACHE.get(range_text, _build_range_command_args(range_text))


def is_current_channel(comment_str, ratio_str=None):
    """
    权威判定通道是否属于电流测量通道 (根据物理注释标签与换算比)
    """
    c = str(comment_str or "").strip().upper()
    if any(tag in c for tag in CURRENT_TAGS):
        return True
    if any(tag in c for tag in VOLTAGE_TAGS):
        return False
    if ratio_str:
        r = str(ratio_str).strip()
        if r and r not in IGNORED_RATIO_LITERALS:
            try:
                val = float(r)
                if abs(val) > 1.5:
                    return True
            except ValueError:
                pass
    return False


def get_optimal_range_for_channel(raw_peak_volt, is_current=False):
    if raw_peak_volt is None or math.isnan(raw_peak_volt):
        return "10mV" if is_current else "1V"
    
    v = abs(float(raw_peak_volt))
    if is_current:
        for limit, r_str in CURRENT_THRESHOLDS:
            if v <= limit:
                return r_str
        return "100V"
    else:
        if v <= 1.0:
            return "1V"
        elif v <= 2.0:
            return "2V"
        elif v <= 10.0:
            return "10V"
        elif v <= 20.0:
            return "20V"
        else:
            return "100V"


def invert_ratio_sign(ratio_text):
    """
    智能取反转换比符号：
    空值或 1 变为 -1；带负号的去掉负号变成正值；正值加上负号
    """
    text = str(ratio_text or "").strip()
    if not text:
        return "-1"
    if text.startswith("-"):
        val_str = text[1:].strip()
        return val_str if val_str else "1"
    elif text.startswith("+"):
        val_str = text[1:].strip()
        return f"-{val_str}" if val_str else "-1"
    else:
        return f"-{text}"


def parse_single_chan_response(res_str):
    if not res_str:
        return None, "EMPTY"
    s = res_str.strip(' "\r\n') if isinstance(res_str, str) else str(res_str).strip(' "\r\n')
    if not s:
        return None, "EMPTY"
    if " " in s:
        s = s.rsplit(" ", 1)[-1].strip('"')
    if "," in s:
        s = s.rsplit(",", 1)[-1].strip('"')
    try:
        val = float(s)
        return (math.nan, "OVER") if abs(val) >= 1.0e+9 else (val, "VALID")
    except ValueError:
        u = s.upper()
        return (math.nan, "OVER") if ("OVER" in u or "O.R" in u) else (math.nan, "INVALID")


def set_system_clipboard_native(text):
    """
    直接将纯文本推入 Windows 系统全局内存（64位指针严格定义 + finally强制防死锁释放机制）。
    免疫一切 Tkinter 焦点丢失与惰性延迟渲染，保证毫秒级可靠写入剪贴板。
    """
    if sys.platform != "win32":
        return False
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


class HiokiPerfectApp:
    def __init__(self, root):
        self.root = root
        self.root.title("HIOKI LR8401-21 / LR8450 程控控制台 v23.0 (极速响应与全功能闭环版)")
        self.root.geometry("1680x850")
        self.root.configure(bg="#ffffff")
        
        self.port = tk.StringVar()
        self.baudrate = tk.StringVar(value="921600")
        self.device_model = tk.StringVar(value="LR8401-21")
        self.ch30_var = tk.BooleanVar(value=False)
        self.interval_var = tk.StringVar(value="10ms")
        self.case_name = tk.StringVar(value="")
        self.ser_lock = threading.Lock()
        
        self.ser = None
        self.is_connected = False
        self.config_unsynced = True
        
        self.instrument_ranges = {}
        self.instrument_stores = {}
        self.instrument_comments = {}
        self.instrument_scalings = {}
        
        self.channel_keys = [f"1-{unit}-{ch}" for unit in (1, 2) for ch in range(1, 16)]
        self.sync_failed_state = {key: False for key in self.channel_keys}
        
        self.gui_val_cache = {}
        self.gui_stat_cache = {}
        self._selection_visual_cache = {}
        
        self.scpi_channel_mapping = {}
        self.scpi_to_key_map = {}
        
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
        self.is_ma_channel = {}
        self.active_channels = []
        self.channel_limits_cache = {}
        self.last_test_stats = {}
        
        self.monitor_thread = None
        self.stop_monitor_event = threading.Event()
        
        self.stats = {key: ChannelStats() for key in self.channel_keys}
        
        # 自适应量程与极性控制状态机
        self.tested_channels = set()           # 已完成过测试的通道集合
        self.over_ranged_channels = set()      # 出现过超量程升档锁定的通道集合
        self.alerted_over_channels = set()     # 本轮测试已弹出超量程告警的通道集合
        self.over_channels_to_upgrade = set()  # 本轮待升档通道集合
        self.pending_optimal_ranges = {}       # 待应用的自适应量程字典
        self.pending_polarity_invert = set()   # 实测平均值为负、下次启动需自动增删负号的通道集合
        
        self.unified_alert_win = None
        self.unified_alert_text = None
        self.unified_alert_items = {}
        
        self.dt_start = None
        self.dt_end = None
        
        self.timer_running = False
        self.start_time = 0.0
        self.elapsed_time = 0.0
        self._save_timer_id = None
        self.init_completed = False
        self._last_copy_time = 0.0
        self._last_text_cache = {}
        
        self.channel_metadata = {}
        for key in self.channel_keys:
            parts = key.split("-")
            unit = int(parts[1])
            ch = int(parts[2])
            self.channel_metadata[key] = {
                "unit": unit,
                "ch": ch,
                "prefix": "CH1" if unit == 1 else "CH2"
            }
        
        self.channel_vars = {}
        for key in self.channel_keys:
            meta = self.channel_metadata[key]
            unit = meta["unit"]
            self.channel_vars[key] = {
                "comment": tk.StringVar(),
                "range": tk.StringVar(value="10mV" if unit == 1 else "1V"),
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
        self.rebuild_scpi_channel_mapping()
        
        self.port.trace_add("write", self._schedule_save)
        self.device_model.trace_add("write", self._on_model_changed)
        self.ch30_var.trace_add("write", self._schedule_save)
        self.interval_var.trace_add("write", self._schedule_save)
        self.case_name.trace_add("write", self._schedule_save)
        for key, vars_dict in self.channel_vars.items():
            vars_dict["comment"].trace_add("write", self._schedule_save)
            vars_dict["range"].trace_add("write", self._on_range_changed_trace)
            vars_dict["ratio"].trace_add("write", lambda *a, k=key: self._on_ratio_changed_trace(k))
        self.init_completed = True
        
        self.auto_set_shortest_interval(verbose=False)
        self.refresh_ports_on_click()
        
        self.root.after(100, self._flush_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_save)

    def _on_range_changed_trace(self, *args):
        self._schedule_save()
        if getattr(self, "init_completed", False):
            self._update_channel_limits_cache()

    def _on_ratio_changed_trace(self, key):
        self._schedule_save()
        if getattr(self, "init_completed", False):
            self._update_channel_limits_cache()
            if not getattr(self, "timer_running", False):
                self.gui_val_cache.clear()
                self.gui_stat_cache.clear()
                if self.is_connected:
                    self.root.after(300, lambda k=key: self._bg_sync_single_ratio_if_idle(k))

    def _bg_sync_single_ratio_if_idle(self, key):
        if not self.is_connected or getattr(self, "timer_running", False):
            return
        threading.Thread(target=self._sync_single_channel_hardware, args=(key, True), daemon=True).start()

    def _on_model_changed(self, *args):
        if not getattr(self, "init_completed", False):
            return
        self.config_unsynced = True
        self.instrument_ranges.clear()
        self.instrument_stores.clear()
        self.instrument_comments.clear()
        self.instrument_scalings.clear()
        for key in self.channel_keys:
            self.sync_failed_state[key] = False
        
        model = self.device_model.get()
        self.ch30_var.set(model == "LR8450")
            
        self.rebuild_scpi_channel_mapping()
        self.auto_set_shortest_interval(verbose=False)

    def rebuild_scpi_channel_mapping(self):
        ch30 = self.ch30_var.get()
        self.scpi_channel_mapping.clear()
        self.scpi_to_key_map.clear()
        for key, meta in self.channel_metadata.items():
            scpi_name = f"CH1_{meta['ch']}" if meta['unit'] == 1 else (f"CH1_{meta['ch']+15}" if ch30 else f"CH2_{meta['ch']}")
            self.scpi_channel_mapping[key] = scpi_name
            self.scpi_to_key_map[scpi_name.upper()] = key

    def _get_filter_scpi(self):
        return ":UNIT:FILTer 50HZ", ":UNIT:FILTer?"

    def _post_unified_alert(self, key, ch_display, comment, alert_type, val_str, limit_str, target_range=""):
        if not self.timer_running or self.stop_monitor_event.is_set():
            return
        tag = f"{ch_display} ({comment})" if comment else ch_display
        if alert_type == "OVER":
            info = f"• {tag}: 发生物理 OVER 溢出 (将自动优化为 {target_range or '更高档位'})"
        else:
            info = f"• {tag}: 瞬态测得 {val_str}，超出 {limit_str} 极限 (将自动优化为 {target_range})"
            
        self.unified_alert_items[key] = info
        self._refresh_unified_alert_window()

    def _refresh_unified_alert_window(self):
        if not self.unified_alert_items or not self.timer_running or self.stop_monitor_event.is_set():
            return

        body_text = "\n".join(self.unified_alert_items.values())

        if self.unified_alert_win is not None and self.unified_alert_win.winfo_exists():
            if self.unified_alert_text is not None:
                self.unified_alert_text.config(state="normal")
                self.unified_alert_text.delete("1.0", tk.END)
                self.unified_alert_text.insert(tk.END, body_text)
                self.unified_alert_text.config(state="disabled")
            self.unified_alert_win.lift()
            self.unified_alert_win.attributes("-topmost", True)
            return

        self.root.bell()
        self.unified_alert_win = tk.Toplevel(self.root)
        self.unified_alert_win.title("⚠️ 测量溢出 / 超量程提示")
        self.unified_alert_win.geometry("540x290")
        self.unified_alert_win.configure(bg="#ffffff")
        self.unified_alert_win.attributes("-topmost", True)

        try:
            x = self.root.winfo_x() + (self.root.winfo_width() // 2) - 270
            y = self.root.winfo_y() + (self.root.winfo_height() // 2) - 145
            self.unified_alert_win.geometry(f"540x290+{x}+{y}")
        except Exception:
            pass

        main_frame = tk.Frame(
            self.unified_alert_win, bg="#fff9f9",
            highlightbackground="#ff3b30", highlightcolor="#ff3b30",
            highlightthickness=2, bd=0
        )
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        tk.Label(
            main_frame, text="⚠️ 测量溢出 / 超量程自动调档提示",
            font=("Microsoft YaHei UI", 11, "bold"), fg="#ff3b30", bg="#fff9f9"
        ).pack(pady=(6, 2))

        tk.Label(
            main_frame, text="以下通道信号超出物理量程上限，下次启动采集时将自动调至最佳匹配档位：",
            font=("Microsoft YaHei UI", 9), fg="#1c1c1e", bg="#fff9f9", justify="left"
        ).pack(anchor="w", padx=12, pady=(0, 4))

        list_frame = tk.Frame(main_frame, bg="#ffffff", bd=1, relief="solid")
        list_frame.pack(fill="both", expand=True, padx=12, pady=4)

        self.unified_alert_text = scrolledtext.ScrolledText(
            list_frame, wrap=tk.WORD, font=("Consolas", 10, "bold"),
            bg="#ffffff", fg="#d97706", bd=0, highlightthickness=0
        )
        self.unified_alert_text.pack(fill="both", expand=True)
        self.unified_alert_text.insert(tk.END, body_text)
        self.unified_alert_text.config(state="disabled")

        def on_ack():
            self._dismiss_unified_alert_window()

        self.unified_alert_win.protocol("WM_DELETE_WINDOW", on_ack)

        tk.Button(
            main_frame, text=" 我知道了 ", font=("Microsoft YaHei UI", 10, "bold"),
            bg="#ff3b30", fg="white", activebackground="#e03126", activeforeground="white",
            relief="flat", bd=0, cursor="hand2", command=on_ack, width=12, height=1
        ).pack(pady=6)

    def _dismiss_unified_alert_window(self):
        if self.unified_alert_win is not None and self.unified_alert_win.winfo_exists():
            try:
                self.unified_alert_win.destroy()
            except Exception:
                pass
            self.unified_alert_win = None
            self.unified_alert_text = None

    def on_drag_start(self, key, event=None):
        self.is_dragging = True
        self.drag_start_key = key
        self.drag_last_key = key
        
        is_ctrl = (event.state & 0x0004) != 0 if event else False
        is_shift = (event.state & 0x0001) != 0 if event else False

        if is_shift and self.last_selected_key in self.channel_keys:
            idx1 = self.channel_keys.index(self.last_selected_key)
            idx2 = self.channel_keys.index(key)
            start_idx, end_idx = min(idx1, idx2), max(idx1, idx2)
            if not is_ctrl:
                self.selected_keys.clear()
            for i in range(start_idx, end_idx + 1):
                self.selected_keys.add(self.channel_keys[i])
            self.drag_base_selection = set(self.selected_keys)
        elif is_ctrl:
            self.drag_base_selection = set(self.selected_keys)
            if key in self.selected_keys:
                self.selected_keys.remove(key)
            else:
                self.selected_keys.add(key)
                self.last_selected_key = key
        else:
            self.drag_base_selection = set()
            self.selected_keys = {key}
            self.last_selected_key = key

        self._update_selection_visuals()
        ent = self.channel_vars[key].get("ent_comment")
        if ent and event and isinstance(event.widget, tk.Label):
            ent.focus_set()

    def on_drag_motion(self, event):
        if not getattr(self, "is_dragging", False) or not self.drag_start_key:
            return

        widget = event.widget.winfo_containing(event.x_root, event.y_root)
        curr_key = self.widget_to_key.get(widget)

        if curr_key and curr_key in self.channel_keys and curr_key != self.drag_last_key:
            self.drag_last_key = curr_key
            idx_start = self.channel_keys.index(self.drag_start_key)
            idx_curr = self.channel_keys.index(curr_key)
            start_idx, end_idx = min(idx_start, idx_curr), max(idx_start, idx_curr)
            range_set = set(self.channel_keys[start_idx : end_idx + 1])

            is_ctrl = (event.state & 0x0004) != 0
            if is_ctrl:
                self.selected_keys = set(self.drag_base_selection) | range_set
            else:
                self.selected_keys = range_set

            self.last_selected_key = curr_key
            self._update_selection_visuals()

    def on_drag_end(self, event=None):
        self.is_dragging = False

    def _update_selection_visuals(self, force=False):
        for key, vars_dict in self.channel_vars.items():
            is_selected = key in self.selected_keys
            sync_failed = self.sync_failed_state.get(key, False)
            state = (is_selected, sync_failed)

            if not force and self._selection_visual_cache.get(key) == state:
                continue
            self._selection_visual_cache[key] = state

            ent = vars_dict.get("ent_comment")
            ent_r = vars_dict.get("ent_ratio")
            lbl = vars_dict.get("lbl_ch")
            cmb_range = vars_dict.get("cmb_range")
            row_bg = vars_dict["row_bg"]
            
            if is_selected:
                if ent: ent.config(bg="#bae6fd", highlightbackground="#007aff", highlightcolor="#007aff")
                if ent_r: ent_r.config(bg="#bae6fd", highlightbackground="#007aff", highlightcolor="#007aff")
                if lbl: lbl.config(bg="#bae6fd", fg="#007aff")
            else:
                if ent: ent.config(bg="#ffffff", highlightbackground="#e5e5ea", highlightcolor="#007aff")
                if ent_r: ent_r.config(bg="#ffffff", highlightbackground="#e5e5ea", highlightcolor="#007aff")
                if lbl:
                    lbl_color = "#ff3b30" if sync_failed else "#1c1c1e"
                    lbl.config(bg=row_bg, fg=lbl_color)

            if cmb_range:
                cmb_range.config(style="Red.TCombobox" if sync_failed else "TCombobox")

    def _sync_single_channel_hardware(self, key, force_set=False):
        """
        硬件通道物理同步：下发物理量程、换算比、工程单位及注释
        """
        is_lr8450 = (self.device_model.get() == "LR8450")
        prefix_cmd = ":UNIT"
        
        ch_id = self.scpi_channel_mapping[key]
        meta = self.channel_metadata[key]
        ch = meta["ch"]
        
        comment = self.channel_vars[key]["comment"].get().strip()
        range_text = self.channel_vars[key]["range"].get()
        ratio_text = self.channel_vars[key]["ratio"].get().strip()
        scpi_range = get_scpi_range(range_text)
        
        last_synced_range = self.instrument_ranges.get(key)
        last_synced_store = self.instrument_stores.get(key)
        last_synced_ratio = self.instrument_scalings.get(key)
        sync_failed_flag = self.sync_failed_state.get(key, False)
        
        range_modified = (range_text != last_synced_range)
        ratio_modified = (ratio_text != last_synced_ratio)
        
        if not force_set and not sync_failed_flag and comment and not range_modified and not ratio_modified and last_synced_store is True:
            return True, range_text, True

        if not force_set and not sync_failed_flag and not comment and not range_modified and not ratio_modified and last_synced_store is False:
            return True, range_text, True

        success = False
        last_readback = ""

        if comment:
            self.send_raw_cmd(f"{prefix_cmd}:STORe {ch_id},ON")
            self.send_raw_cmd(f"{prefix_cmd}:INMOde {ch_id},VOLTAGE")

            cmd_args = get_range_command_args(range_text, is_lr8450)
            for _, range_arg in cmd_args:
                self.send_raw_cmd(f"{prefix_cmd}:RANGe {ch_id},{range_arg}")
                time.sleep(0.005)
                last_readback = self.query_raw_cmd(f"{prefix_cmd}:RANGe? {ch_id}")
                if is_scpi_range_match(last_readback, scpi_range):
                    success = True
                    break
                time.sleep(0.01)
                last_readback = self.query_raw_cmd(f"{prefix_cmd}:RANGe? {ch_id}")
                if is_scpi_range_match(last_readback, scpi_range):
                    success = True
                    break

            self.send_raw_cmd(f':COMMent:CH {ch_id},"{comment}"')
            color_idx = (ch - 1) % 24 + 1
            self.send_raw_cmd(f":DISPlay:DRAWing {ch_id},C{color_idx}")

            is_curr = is_current_channel(comment, ratio_text)
            unit_str = "mA" if is_curr else "V"
            ratio_val = None
            if ratio_text:
                try:
                    val = float(ratio_text)
                    if abs(val) > 0:
                        ratio_val = val
                except ValueError:
                    pass

            if ratio_val is not None:
                self.send_raw_cmd(f":SCALing:SET {ch_id},ENG")
                self.send_raw_cmd(f":SCALing:KIND {ch_id},RATIO")
                self.send_raw_cmd(f":SCALing:UNIT {ch_id},\"{unit_str}\"")
                self.send_raw_cmd(f":SCALing:VOLT {ch_id},{ratio_val}")
                self.send_raw_cmd(f":SCALing:OFFSet {ch_id},0")
            elif is_curr:
                self.send_raw_cmd(f":SCALing:SET {ch_id},ENG")
                self.send_raw_cmd(f":SCALing:KIND {ch_id},RATIO")
                self.send_raw_cmd(f":SCALing:UNIT {ch_id},\"mA\"")
                self.send_raw_cmd(f":SCALing:VOLT {ch_id},1")
                self.send_raw_cmd(f":SCALing:OFFSet {ch_id},0")
            else:
                self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")

            self.instrument_stores[key] = True
        else:
            range_arg = get_range_command_args(range_text, is_lr8450)[0][1]
            self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
            self.send_raw_cmd(f"{prefix_cmd}:RANGe {ch_id},{range_arg}")
            self.send_raw_cmd(f"{prefix_cmd}:STORe {ch_id},OFF")
            self.send_raw_cmd(f":DISPlay:DRAWing {ch_id},OFF")
            
            success = True
            self.instrument_stores[key] = False

        actual_range_str = format_range_for_log(last_readback) if last_readback else range_text
        if success:
            self.instrument_ranges[key] = range_text
            self.instrument_scalings[key] = ratio_text
            self.sync_failed_state[key] = False
        else:
            self.sync_failed_state[key] = True

        return success, actual_range_str, False

    def on_range_combobox_selected(self, key):
        if not getattr(self, "init_completed", False):
            return
            
        selected_range = self.channel_vars[key]["range"].get()
        targets = [key]
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            self.init_completed = False
            for k in self.selected_keys:
                if k != key and self.channel_vars[k]["range"].get() != selected_range:
                    self.channel_vars[k]["range"].set(selected_range)
                    targets.append(k)
            self.init_completed = True
            
        for k in targets:
            self.pending_optimal_ranges.pop(k, None)
            self.over_channels_to_upgrade.discard(k)
            self.over_ranged_channels.discard(k)
            self.tested_channels.add(k)
            
        self.config_unsynced = True
        self._schedule_save()
        self._update_channel_limits_cache()
        
        if self.is_connected:
            if getattr(self, "timer_running", False):
                self.write_log("[即时修改] 采集测量中手动修改的量程已保存锁定，下次开始采集时将优先应用。")
            else:
                threading.Thread(
                    target=self._bg_batch_set_ranges_now, 
                    args=(targets, selected_range), 
                    daemon=True
                ).start()

    def _bg_batch_set_ranges_now(self, keys, range_text):
        has_commented = any(self.channel_vars[k]["comment"].get().strip() for k in keys)
        if has_commented:
            self.write_log(f"[即时修改] 正在同步已修改量程的有效通道为 {range_text}...")
        
        for k in keys:
            comment = self.channel_vars[k]["comment"].get().strip()
            ch_id = self.scpi_channel_mapping[k]
            success, actual_range, skipped = self._sync_single_channel_hardware(k, force_set=True)
            if comment and not skipped:
                if success:
                    self.write_log(f"[即时修改] {ch_id} ({comment}) 量程成功应用为: {actual_range} ✓")
                else:
                    self.write_log(f"[即时修改失败] ⚠️ {ch_id} ({comment}) 无法更新量程！当前实际: {actual_range}")

        self.root.after(0, self._update_selection_visuals)

    def _bg_sync_all_commented_ranges_now(self, keys, is_lr8450=None):
        for k in keys:
            comment = self.channel_vars[k]["comment"].get().strip()
            ch_id = self.scpi_channel_mapping[k]
            success, actual_range, _ = self._sync_single_channel_hardware(k, force_set=True)
            if success:
                self.write_log(f"[连接同步] {ch_id} ({comment}) 的物理量程设为: {actual_range} ✓")
                
        self.write_log("[连接同步] 所有有效通道配置已完成硬件同步与回读验证 ✓ (仪器面板转换比与单位已同步对齐)")
        self.config_unsynced = False
        self.root.after(0, self._update_selection_visuals)
        self.root.after(0, self._restore_ui_after_task)

    def _sync_selected_values(self, key, event, field_name):
        if time.monotonic() - getattr(self, "_last_copy_time", 0.0) < 0.35:
            return
        if (event.state & 0x0004) or (event.state & 0x0008):
            return
        if (event.keysym in IGNORED_NAV_KEYSYMS) or (event.keysym.startswith("F") and event.keysym[1:].isdigit()):
            return

        if len(self.selected_keys) > 1 and key in self.selected_keys:
            val = self.channel_vars[key][field_name].get()
            cache_key = f"{key}_{field_name}"
            if getattr(self, "_last_text_cache", {}).get(cache_key) == val:
                return
            if not hasattr(self, "_last_text_cache"):
                self._last_text_cache = {}
            self._last_text_cache[cache_key] = val

            self.init_completed = False
            for k in self.selected_keys:
                if k != key:
                    self.channel_vars[k][field_name].set(val)
                    self._last_text_cache[f"{k}_{field_name}"] = val
            self.init_completed = True
            self._schedule_save()

    def _handle_copy_sync(self, key, field_name):
        self._last_copy_time = time.monotonic()
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            ordered_keys = [k for k in self.channel_keys if k in self.selected_keys]
            vals = [self.channel_vars[k][field_name].get() for k in ordered_keys]
            copy_str = "\n".join(vals)
            if not set_system_clipboard_native(copy_str):
                try:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(copy_str)
                except Exception:
                    pass
            field_label = "首注释" if field_name == "comment" else "转换比"
            self.write_log(f"[快捷复制] 已将选中 {len(ordered_keys)} 个通道的{field_label}批量复制到剪贴板。")
            return "break"
            
        ent = self.channel_vars[key].get(f"ent_{field_name}")
        if ent:
            try:
                sel_text = ent.selection_get()
                if sel_text:
                    if not set_system_clipboard_native(sel_text):
                        self.root.clipboard_clear()
                        self.root.clipboard_append(sel_text)
                    return "break"
            except Exception:
                pass
            val = self.channel_vars[key][field_name].get()
            if val:
                if not set_system_clipboard_native(val):
                    self.root.clipboard_clear()
                    self.root.clipboard_append(val)
                return "break"
        return None

    def _handle_paste_sync(self, key, field_name):
        try:
            clipboard = self.root.clipboard_get()
        except Exception:
            return None
            
        lines = [line.strip('\r') for line in clipboard.split('\n')]
        if lines and lines[-1] == "":
            lines.pop()
            
        if not lines:
            return None
            
        keys_list = self.channel_keys
        
        if len(self.selected_keys) > 1 and len(lines) == 1:
            self.init_completed = False
            for k in self.selected_keys:
                self.channel_vars[k][field_name].set(lines[0])
                self._last_text_cache[f"{k}_{field_name}"] = lines[0]
            self.init_completed = True
            self.save_local_config()
            return "break"
            
        if len(lines) > 1:
            start_idx = keys_list.index(key)
            self.init_completed = False
            for i, line in enumerate(lines):
                if start_idx + i < len(keys_list):
                    target_key = keys_list[start_idx + i]
                    self.channel_vars[target_key][field_name].set(line)
                    self._last_text_cache[f"{target_key}_{field_name}"] = line
            self.init_completed = True
            self.save_local_config()
            return "break"
            
        self.root.after(50, self.save_local_config)
        return None

    def _handle_delete_sync(self, key, field_name, event=None):
        targets = list(self.selected_keys) if (len(self.selected_keys) > 1 and key in self.selected_keys) else [key]
        self.init_completed = False
        for k in targets:
            self.channel_vars[k][field_name].set("")
            self._last_text_cache[f"{k}_{field_name}"] = ""
        self.init_completed = True
        self.save_local_config()
        self._reset_gui_val_labels()
        field_label = "首注释" if field_name == "comment" else "转换比"
        self.write_log(f"[快捷删除] 已清空 {len(targets)} 个通道的{field_label}。")
        return "break"

    def on_comment_copy(self, key, event): return self._handle_copy_sync(key, "comment")
    def on_comment_keyrelease(self, key, event): self._sync_selected_values(key, event, "comment")
    def on_comment_paste(self, key, event): return self._handle_paste_sync(key, "comment")
    def on_comment_delete(self, key, event):
        if (len(self.selected_keys) > 1 and key in self.selected_keys) or isinstance(event.widget, tk.Label):
            return self._handle_delete_sync(key, "comment", event)
        return None

    def on_ratio_copy(self, key, event): return self._handle_copy_sync(key, "ratio")
    def on_ratio_keyrelease(self, key, event): self._sync_selected_values(key, event, "ratio")
    def on_ratio_paste(self, key, event): return self._handle_paste_sync(key, "ratio")
    def on_ratio_delete(self, key, event):
        if (len(self.selected_keys) > 1 and key in self.selected_keys) or isinstance(event.widget, tk.Label):
            return self._handle_delete_sync(key, "ratio", event)
        return None

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
        style.configure("TCombobox", 
                        fieldbackground="#ffffff", background="#f2f2f7", foreground="#1c1c1e", 
                        arrowcolor="#8e8e93", bordercolor="#e5e5ea", lightcolor="#e5e5ea", darkcolor="#e5e5ea")
        style.map("TCombobox", 
                  fieldbackground=[("readonly", "#ffffff"), ("disabled", "#f2f2f7")], 
                  selectbackground=[("readonly", "#bae6fd"), ("focus", "#bae6fd")],
                  selectforeground=[("readonly", "#1c1c1e"), ("focus", "#1c1c1e")],
                  foreground=[("disabled", "#aeaeb2"), ("readonly", "#1c1c1e"), ("focus", "#1c1c1e"), ("!disabled", "#1c1c1e")])

        style.configure("Red.TCombobox", 
                        fieldbackground="#ffebee", background="#ffcdd2", foreground="#ff3b30", 
                        arrowcolor="#ff3b30", bordercolor="#ff8a80", lightcolor="#ff8a80", darkcolor="#ff8a80")
        style.map("Red.TCombobox", 
                  fieldbackground=[("readonly", "#ffebee"), ("disabled", "#ffebee")], 
                  selectbackground=[("readonly", "#ffcdd2"), ("focus", "#ffcdd2")],
                  selectforeground=[("readonly", "#ff3b30"), ("focus", "#ff3b30")],
                  foreground=[("disabled", "#ff8a80"), ("readonly", "#ff3b30"), ("focus", "#ff3b30"), ("!disabled", "#ff3b30")])
        
        conn_frame = tk.Frame(self.root, bg="#ffffff", highlightbackground="#e5e5ea", highlightcolor="#e5e5ea", highlightthickness=1, bd=0)
        conn_frame.pack(fill="x", padx=15, pady=10)
        
        tk.Label(conn_frame, text=" 仪器连接及配置导入导出 ", font=("Microsoft YaHei UI", 11, "bold"), bg="#ffffff", fg="#007aff").grid(row=0, column=0, columnspan=11, sticky="w", padx=10, pady=5)
        
        tk.Label(conn_frame, text="串口号:", bg="#ffffff", fg="#8e8e93", font=("Microsoft YaHei UI", 10)).grid(row=1, column=0, padx=5, pady=8, sticky="w")
        self.port_combo = ttk.Combobox(conn_frame, textvariable=self.port, width=12, font=("Segoe UI", 10), state="readonly", postcommand=self.refresh_ports_on_click)
        self.port_combo.grid(row=1, column=1, padx=5, pady=8)

        tk.Label(conn_frame, text="仪器型号:", bg="#ffffff", fg="#8e8e93", font=("Microsoft YaHei UI", 10)).grid(row=1, column=2, padx=5, pady=8, sticky="w")
        self.model_combo = ttk.Combobox(conn_frame, textvariable=self.device_model, values=["LR8401-21", "LR8450"], width=10, font=("Segoe UI", 10), state="readonly")
        self.model_combo.grid(row=1, column=3, padx=5, pady=8)

        tk.Label(conn_frame, text="记录间隔:", bg="#ffffff", fg="#8e8e93", font=("Microsoft YaHei UI", 10)).grid(row=1, column=4, padx=5, pady=8, sticky="w")
        self.interval_combo = ttk.Combobox(
            conn_frame, textvariable=self.interval_var, 
            values=["10ms", "20ms", "50ms", "100ms", "200ms", "500ms", "1s", "2s", "5s", "10s"], 
            width=7, font=("Segoe UI", 10), state="readonly"
        )
        self.interval_combo.grid(row=1, column=5, padx=5, pady=8)
        self.interval_combo.bind("<<ComboboxSelected>>", self.on_interval_combobox_selected)
        
        self.btn_connect = tk.Button(
            conn_frame, text=" 连接仪器 ", bg="#007aff", fg="white", 
            font=("Microsoft YaHei UI", 10, "bold"), relief="flat", activebackground="#0062cc", activeforeground="white", bd=0, cursor="hand2",
            command=self.toggle_connection
        )
        self.btn_connect.grid(row=1, column=6, padx=12, pady=8)
        
        self.lbl_status = tk.Label(conn_frame, text="未连接", fg="#ff3b30", font=("Microsoft YaHei UI", 11, "bold"), bg="#ffffff")
        self.lbl_status.grid(row=1, column=7, padx=8, pady=8)
        
        btn_opts = {
            "font": ("Microsoft YaHei UI", 10, "bold"), "relief": "flat", "padx": 12, "pady": 4,
            "fg": "#007aff", "bg": "#f2f2f7", "activebackground": "#e5e5ea", "activeforeground": "#0056b3",
            "bd": 0, "cursor": "hand2"
        }
        self.btn_import = tk.Button(conn_frame, text=" 📂 导入配置 ", command=self.import_config_file, **btn_opts)
        self.btn_import.grid(row=1, column=8, padx=5, pady=8)
        
        self.btn_export = tk.Button(conn_frame, text=" 💾 导出配置 ", command=self.export_config_file, **btn_opts)
        self.btn_export.grid(row=1, column=9, padx=5, pady=8)

        self.btn_batch = tk.Button(conn_frame, text=" 🔧 集中处理 ", command=self.open_batch_config_dialog, **btn_opts)
        self.btn_batch.grid(row=1, column=10, padx=5, pady=8)
        
        matrix_outer_frame = tk.Frame(self.root, bg="#ffffff")
        matrix_outer_frame.pack(fill="x", padx=15, pady=5)
        
        left_matrix = tk.Frame(matrix_outer_frame, bg="#ffffff", highlightbackground="#e5e5ea", highlightcolor="#e5e5ea", highlightthickness=1, bd=0)
        left_matrix.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=5)
        
        right_matrix = tk.Frame(matrix_outer_frame, bg="#ffffff", highlightbackground="#e5e5ea", highlightcolor="#e5e5ea", highlightthickness=1, bd=0)
        right_matrix.pack(side="right", fill="both", expand=True, padx=(5, 0), pady=5)
        
        def draw_header(parent, title):
            tk.Label(parent, text=title, font=("Microsoft YaHei UI", 11, "bold"), fg="#007aff", bg="#ffffff").grid(row=0, column=0, columnspan=8, pady=6)
            tk.Label(parent, text="通道", font=("Microsoft YaHei UI", 10, "bold"), fg="#8e8e93", bg="#ffffff", width=6).grid(row=1, column=0, sticky="w", padx=2)
            tk.Label(parent, text="首注释 (多选及Excel粘)", font=("Microsoft YaHei UI", 10, "bold"), fg="#8e8e93", bg="#ffffff", width=18).grid(row=1, column=1, sticky="w", padx=2)
            tk.Label(parent, text="量程选择", font=("Microsoft YaHei UI", 10, "bold"), fg="#8e8e93", bg="#ffffff", width=10).grid(row=1, column=2, sticky="w", padx=2)
            tk.Label(parent, text="转换比", font=("Microsoft YaHei UI", 10, "bold"), fg="#8e8e93", bg="#ffffff", width=10).grid(row=1, column=3, sticky="w", padx=2)
            tk.Label(parent, text="实时测量值", font=("Microsoft YaHei UI", 10, "bold"), bg="#007aff", fg="white", width=12).grid(row=1, column=4, padx=2)
            tk.Label(parent, text="平均值 (Avg)", font=("Microsoft YaHei UI", 10, "bold"), bg="#f2f2f7", fg="#8e8e93", width=11).grid(row=1, column=5, padx=2)
            tk.Label(parent, text="最大值 (Max)", font=("Microsoft YaHei UI", 10, "bold"), bg="#f2f2f7", fg="#8e8e93", width=11).grid(row=1, column=6, padx=2)
            tk.Label(parent, text="最小值 (Min)", font=("Microsoft YaHei UI", 10, "bold"), bg="#f2f2f7", fg="#8e8e93", width=11).grid(row=1, column=7, padx=2)
            
        draw_header(left_matrix, "UNIT 1 (CH1_1 - CH1_15)")
        draw_header(right_matrix, "UNIT 2 (CH2_1 - CH2_15)")
        
        self.widget_to_key.clear()
        
        for unit, matrix_frame in [(1, left_matrix), (2, right_matrix)]:
            prefix = "CH1" if unit == 1 else "CH2"
            for ch in range(1, 16):
                key = f"1-{unit}-{ch}"
                row_bg = "#ffffff" if ch % 2 == 0 else "#f6f6f9"
                self.channel_vars[key]["row_bg"] = row_bg
                
                lbl_ch = tk.Label(matrix_frame, text=f"{prefix}_{ch}", font=("Segoe UI", 10, "bold"), bg=row_bg, fg="#1c1c1e", cursor="hand2")
                lbl_ch.grid(row=ch+1, column=0, sticky="nsew", pady=1, padx=2)
                
                ent_comment = self._create_ios_entry(matrix_frame, self.channel_vars[key]["comment"], width=18)
                ent_comment.grid(row=ch+1, column=1, sticky="nsew", padx=2, pady=1)
                
                cmb_range = ttk.Combobox(matrix_frame, textvariable=self.channel_vars[key]["range"], values=RANGE_LIST, width=8, font=("Segoe UI", 10), state="readonly")
                cmb_range.grid(row=ch+1, column=2, sticky="nsew", padx=2, pady=1)
                cmb_range.bind("<<ComboboxSelected>>", lambda e, k=key: self.on_range_combobox_selected(k))
                
                ent_ratio = self._create_ios_entry(matrix_frame, self.channel_vars[key]["ratio"], width=10, is_monospace=True)
                ent_ratio.grid(row=ch+1, column=3, sticky="nsew", padx=2, pady=1)

                self.channel_vars[key]["ent_comment"] = ent_comment
                self.channel_vars[key]["ent_ratio"] = ent_ratio
                self.channel_vars[key]["lbl_ch"] = lbl_ch
                self.channel_vars[key]["cmb_range"] = cmb_range
                
                self.widget_to_key[lbl_ch] = key
                self.widget_to_key[ent_comment] = key
                self.widget_to_key[ent_ratio] = key
                self.widget_to_key[cmb_range] = key
                
                lbl_ch.bind("<ButtonPress-1>", lambda e, k=key: self.on_drag_start(k, e))
                lbl_ch.bind("<B1-Motion>", self.on_drag_motion)
                lbl_ch.bind("<ButtonRelease-1>", self.on_drag_end)
                lbl_ch.bind("<Control-c>", lambda e, k=key: self.on_comment_copy(k, e))
                lbl_ch.bind("<Control-v>", lambda e, k=key: self.on_comment_paste(k, e))
                lbl_ch.bind("<Delete>", lambda e, k=key: self.on_comment_delete(k, e))
                lbl_ch.bind("<BackSpace>", lambda e, k=key: self.on_comment_delete(k, e))
                
                ent_comment.bind("<ButtonPress-1>", lambda e, k=key: self.on_drag_start(k, e))
                ent_comment.bind("<B1-Motion>", self.on_drag_motion)
                ent_comment.bind("<ButtonRelease-1>", self.on_drag_end)
                ent_comment.bind("<KeyRelease>", lambda e, k=key: self.on_comment_keyrelease(k, e))
                ent_comment.bind("<Control-c>", lambda e, k=key: self.on_comment_copy(k, e))
                ent_comment.bind("<Control-v>", lambda e, k=key: self.on_comment_paste(k, e))
                ent_comment.bind("<Shift-Insert>", lambda e, k=key: self.on_comment_paste(k, e))
                ent_comment.bind("<Delete>", lambda e, k=key: self.on_comment_delete(k, e))
                ent_comment.bind("<BackSpace>", lambda e, k=key: self.on_comment_delete(k, e))
                
                ent_ratio.bind("<ButtonPress-1>", lambda e, k=key: self.on_drag_start(k, e))
                ent_ratio.bind("<B1-Motion>", self.on_drag_motion)
                ent_ratio.bind("<ButtonRelease-1>", self.on_drag_end)
                ent_ratio.bind("<KeyRelease>", lambda e, k=key: self.on_ratio_keyrelease(k, e))
                ent_ratio.bind("<Control-c>", lambda e, k=key: self.on_ratio_copy(k, e))
                ent_ratio.bind("<Control-v>", lambda e, k=key: self.on_ratio_paste(k, e))
                ent_ratio.bind("<Shift-Insert>", lambda e, k=key: self.on_ratio_paste(k, e))
                ent_ratio.bind("<Delete>", lambda e, k=key: self.on_ratio_delete(k, e))
                ent_ratio.bind("<BackSpace>", lambda e, k=key: self.on_ratio_delete(k, e))
                
                lbl_val = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=12, bd=0)
                lbl_val.grid(row=ch+1, column=4, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["lbl_val"] = lbl_val
                
                lbl_avg = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=11, bd=0)
                lbl_avg.grid(row=ch+1, column=5, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["lbl_avg"] = lbl_avg

                lbl_max = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=11, bd=0)
                lbl_max.grid(row=ch+1, column=6, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["lbl_max"] = lbl_max

                lbl_min = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=11, bd=0)
                lbl_min.grid(row=ch+1, column=7, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["lbl_min"] = lbl_min

        control_frame = tk.Frame(self.root, bg="#ffffff", highlightbackground="#e5e5ea", highlightcolor="#e5e5ea", highlightthickness=1, bd=0)
        control_frame.pack(fill="x", padx=15, pady=5)
        
        self.lbl_timer = tk.Label(
            control_frame, text="00:00.0", font=("Segoe UI", 32, "bold"), 
            bg="#f2f2f7", fg="#1c1c1e", width=12, bd=0
        )
        self.lbl_timer.pack(side="left", padx=15, pady=8)
        
        self.btn_start = tk.Button(
            control_frame, text="▶ 开始采集 (Start)", bg="#34c759", fg="white", 
            font=("Microsoft YaHei UI", 11, "bold"), relief="flat", height=2, width=18,
            state="disabled", cursor="hand2", bd=0, activebackground="#2c9e47", activeforeground="white",
            command=self.start_acquisition
        )
        self.btn_start.pack(side="left", expand=True, padx=15)
        
        self.btn_copy = tk.Button(
            control_frame, text="📋 一键复制数据", bg="#5e5ce6", fg="white", 
            font=("Microsoft YaHei UI", 11, "bold"), relief="flat", height=2, width=18,
            cursor="hand2", bd=0, activebackground="#4d4cb8", activeforeground="white",
            command=self.copy_clipboard_data
        )
        self.btn_copy.pack(side="left", expand=True, padx=15)
        
        self.btn_stop = tk.Button(
            control_frame, text="■ 停止并保存 (Stop)", bg="#ff3b30", fg="white", 
            font=("Microsoft YaHei UI", 11, "bold"), relief="flat", height=2, width=18,
            state="disabled", cursor="hand2", bd=0, activebackground="#e03126", activeforeground="white",
            command=self.stop_acquisition
        )
        self.btn_stop.pack(side="right", expand=True, padx=15)
        
        log_frame = tk.Frame(self.root, bg="#ffffff", highlightbackground="#e5e5ea", highlightcolor="#e5e5ea", highlightthickness=1, bd=0)
        log_frame.pack(fill="both", expand=True, padx=15, pady=10)
        
        tk.Label(log_frame, text=" 实时程控日志及统计看板 ", font=("Microsoft YaHei UI", 10, "bold"), bg="#ffffff", fg="#8e8e93").pack(anchor="w", padx=10, pady=5)
        
        self.log_area = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, font=("Consolas", 11), 
            bg="#ffffff", fg="#1c1c1e", bd=0, highlightthickness=1, highlightbackground="#e5e5ea"
        )
        self.log_area.pack(fill="both", expand=True, padx=10, pady=8)

    def refresh_ports_on_click(self):
        ports = serial.tools.list_ports.comports()
        port_list = [port.device for port in ports]
        
        if not port_list:
            port_list = ["无可用COM口"]
            self.port_combo.config(state="disabled")
            self.port.set("无可用COM口")
        else:
            self.port_combo.config(state="readonly")
            curr = self.port.get()
            if curr not in port_list:
                self.port.set("COM31" if "COM31" in port_list else port_list[0])
                    
        self.port_combo['values'] = port_list

    def _sync_hardware_interval(self, interval_text):
        info = INTERVAL_SCPI_MAP.get(interval_text, {"sec": "0.01", "enum": "10MS", "sec_num": 0.01})
        target_sec = info["sec_num"]
        
        cmd_candidates = [
            f":CONFigure:SAMPle {info['sec']}",
            f":CONFigure:SAMPle {info['enum']}",
            f":CONFigure:SAMPle {interval_text.lower()}",
            f":SAMPle {info['sec']}",
            f":SAMPle {info['enum']}",
            f":CONFigure:INTerval {info['sec']}",
            f":CONFigure:INTerval {info['enum']}",
            f":TIMebase {info['sec']}",
        ]
        query_candidates = [
            ":CONFigure:SAMPle?",
            ":SAMPle?",
            ":CONFigure:INTerval?",
            ":TIMebase?"
        ]
        
        success = False
        last_readback = ""
        
        for cmd in cmd_candidates:
            self.send_raw_cmd(cmd)
            time.sleep(0.01)
            for q in query_candidates:
                rb = self.query_raw_cmd(q, timeout=0.1)
                rb_sec = parse_interval_readback_sec(rb)
                if rb_sec is not None and abs(rb_sec - target_sec) <= max(target_sec * 0.01, 1e-4):
                    success = True
                    last_readback = rb
                    break
            if success:
                break
                
        return success, last_readback

    def on_interval_combobox_selected(self, event=None):
        if not getattr(self, "init_completed", False):
            return
        self.auto_set_shortest_interval(verbose=True)
        self.config_unsynced = True
        self._schedule_save()
        if self.is_connected:
            if getattr(self, "timer_running", False):
                self.write_log("[提示] 采集测量中修改的记录间隔已保存，下次开始采集时将自动全面同步。")
            else:
                interval_text = self.interval_var.get()
                threading.Thread(
                    target=self._bg_set_interval_now,
                    args=(interval_text,),
                    daemon=True
                ).start()

    def _bg_set_interval_now(self, interval_text):
        success, rb = self._sync_hardware_interval(interval_text)
        if success:
            self.write_log(f"[物理即时同步] 仪器物理硬件记录间隔已成功应用为: {interval_text} (回读验证: {rb} ✓)")
        else:
            self.write_log(f"[设置警告] ⚠️ 尝试将物理记录间隔设为 {interval_text} 失败或仪器拒绝！回读状态: {rb or '无响应'}")

    def auto_set_shortest_interval(self, verbose=True):
        if self.device_model.get() == "LR8450":
            return

        current_val = self.interval_var.get()
        has_unit2_active = any(self.channel_vars[f"1-2-{ch}"]["comment"].get().strip() for ch in range(1, 16))
        if has_unit2_active and current_val == "10ms":
            self.interval_var.set("20ms")
            if verbose:
                self.write_log("[硬件限制提示] 日置 LR8401 硬件架构限制：在开启 UNIT 2 通道时最低记录间隔为 20ms！若需使用 10ms 采样率，请清空 UNIT 2 通道；使用 30 通道时请选 LR8450。记录间隔已自动对齐为: 20ms")

    def open_batch_config_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("复制")
        dialog.geometry("540x650")
        dialog.configure(bg="#ffffff")
        dialog.grab_set() 
        dialog.transient(self.root)
        
        param_vars = {
            "comment": tk.BooleanVar(value=True),
            "range": tk.BooleanVar(value=True),
            "ratio": tk.BooleanVar(value=True),
        }
        
        source_options = []
        for unit in (1, 2):
            for ch in range(1, 16):
                key = f"1-{unit}-{ch}"
                comment = self.channel_vars[key]["comment"].get().strip()
                source_options.append(f"CH {unit}-{ch} ({comment})" if comment else f"CH {unit}-{ch}")
                    
        source_var = tk.StringVar(value=source_options[0])
        
        src_top_frame = tk.Frame(dialog, bg="#ffffff")
        src_top_frame.pack(fill="x", padx=15, pady=12)
        
        tk.Label(src_top_frame, text="复制源", font=("Microsoft YaHei UI", 10, "bold"), fg="#007aff", bg="#ffffff").pack(side="left", padx=5)
        src_combo = ttk.Combobox(src_top_frame, textvariable=source_var, values=source_options, font=("Segoe UI", 10), state="readonly", width=42)
        src_combo.pack(side="right", padx=10, fill="x", expand=True)
        
        param_frame = tk.LabelFrame(dialog, text=" 复制参数 ", font=("Microsoft YaHei UI", 10, "bold"), bg="#f2f2f7", fg="#1c1c1e", bd=1, relief="solid")
        param_frame.pack(fill="x", padx=15, pady=5)
        
        chk_container = tk.Frame(param_frame, bg="#ffffff", bd=0)
        chk_container.pack(side="left", fill="both", expand=True, padx=15, pady=10)
        
        tk.Checkbutton(chk_container, text="注释", variable=param_vars["comment"], bg="#ffffff", fg="#1c1c1e", activebackground="#bae6fd", activeforeground="#1c1c1e", font=("Microsoft YaHei UI", 10), anchor="w").pack(fill="x", padx=15, pady=3)
        tk.Checkbutton(chk_container, text="量程", variable=param_vars["range"], bg="#ffffff", fg="#1c1c1e", activebackground="#bae6fd", activeforeground="#1c1c1e", font=("Microsoft YaHei UI", 10), anchor="w").pack(fill="x", padx=15, pady=3)
        tk.Checkbutton(chk_container, text="转换比", variable=param_vars["ratio"], bg="#ffffff", activebackground="#ffffff", activeforeground="#000000", font=("Microsoft YaHei UI", 10), anchor="w").pack(fill="x", padx=15, pady=3)
        
        btn_param_sidebar = tk.Frame(param_frame, bg="#f2f2f7")
        btn_param_sidebar.pack(side="right", fill="y", padx=15, pady=10)
        
        btn_style = {"font": ("Microsoft YaHei UI", 9), "relief": "flat", "bd": 0, "width": 11, "bg": "#f2f2f7", "fg": "#007aff", "activebackground": "#e5e5ea", "activeforeground": "#0f172a", "cursor": "hand2"}
        
        tk.Button(btn_param_sidebar, text="选择全部", command=lambda: [v.set(True) for v in param_vars.values()], **btn_style).pack(pady=3)
        tk.Button(btn_param_sidebar, text="取消选择", command=lambda: [v.set(False) for v in param_vars.values()], **btn_style).pack(pady=3)
        tk.Button(btn_param_sidebar, text="反选", command=lambda: [v.set(not v.get()) for v in param_vars.values()], **btn_style).pack(pady=3)
        
        target_lf = tk.LabelFrame(dialog, text=" 复制目标 ", font=("Microsoft YaHei UI", 10, "bold"), bg="#f2f2f7", fg="#1c1c1e", bd=1, relief="solid")
        target_lf.pack(fill="both", expand=True, padx=15, pady=5)
        
        list_container = tk.Frame(target_lf, bg="#f2f2f7")
        list_container.pack(side="left", fill="both", expand=True, padx=15, pady=10)
        
        scrollbar = ttk.Scrollbar(list_container, orient="vertical")
        target_listbox = tk.Listbox(
            list_container, selectmode="extended", yscrollcommand=scrollbar.set,
            font=("Microsoft YaHei UI", 10), bd=0, highlightthickness=0, bg="#ffffff", fg="#1c1c1e",
            selectbackground="#007aff", selectforeground="#ffffff"
        )
        scrollbar.config(command=target_listbox.yview)
        target_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        for unit in (1, 2):
            for ch in range(1, 16):
                key = f"1-{unit}-{ch}"
                comment_text = self.channel_vars[key]["comment"].get().strip()
                target_listbox.insert(tk.END, f"CH {unit}-{ch} ({comment_text})" if comment_text else f"CH {unit}-{ch}")
                
        btn_target_sidebar = tk.Frame(target_lf, bg="#f2f2f7")
        btn_target_sidebar.pack(side="right", fill="y", padx=15, pady=10)
        
        tk.Button(btn_target_sidebar, text="选择全部", command=lambda: target_listbox.select_set(0, tk.END), **btn_style).pack(pady=3)
        tk.Button(btn_target_sidebar, text="取消选择", command=lambda: target_listbox.select_clear(0, tk.END), **btn_style).pack(pady=3)
        
        def invert_listbox_selection():
            for i in range(target_listbox.size()):
                if target_listbox.selection_includes(i):
                    target_listbox.select_clear(i)
                else:
                    target_listbox.select_set(i)
        tk.Button(btn_target_sidebar, text="反选", command=invert_listbox_selection, **btn_style).pack(pady=3)
        
        bottom_bar = tk.Frame(dialog, bg="#ffffff")
        bottom_bar.pack(fill="x", padx=15, pady=15)
        
        def commit_batch_copy():
            src_display = source_var.get()
            try:
                parts = src_display.split(" ")[1].split("-")
                src_key = f"1-{parts[0]}-{parts[1]}"
            except Exception as e:
                messagebox.showerror("错误", f"无法解析源通道格式: {e}")
                return
                
            if not any(v.get() for v in param_vars.values()):
                messagebox.showwarning("提示", "请至少选择一项要复制的参数！")
                return
                
            selected_indices = target_listbox.curselection()
            if not selected_indices:
                messagebox.showwarning("提示", "请选择至少一个目标通道！")
                return
                
            self.init_completed = False
            synced_keys = []
            for idx in selected_indices:
                t_key = self.channel_keys[idx]
                if t_key == src_key:
                    continue  
                
                if param_vars["comment"].get():
                    c_val = self.channel_vars[src_key]["comment"].get()
                    self.channel_vars[t_key]["comment"].set(c_val)
                    self._last_text_cache[f"{t_key}_comment"] = c_val
                if param_vars["range"].get():
                    new_range = self.channel_vars[src_key]["range"].get()
                    old_range = self.channel_vars[t_key]["range"].get()
                    self.pending_optimal_ranges.pop(t_key, None)
                    self.over_channels_to_upgrade.discard(t_key)
                    self.over_ranged_channels.discard(t_key)
                    self.tested_channels.add(t_key)
                    if old_range != new_range:
                        self.channel_vars[t_key]["range"].set(new_range)
                        synced_keys.append(t_key)
                    else:
                        self.channel_vars[t_key]["range"].set(new_range)
                if param_vars["ratio"].get():
                    r_val = self.channel_vars[src_key]["ratio"].get()
                    self.channel_vars[t_key]["ratio"].set(r_val)
                    self._last_text_cache[f"{t_key}_ratio"] = r_val
                
            self.init_completed = True
            self.config_unsynced = True
            self.save_local_config()
            self._reset_gui_val_labels()
            self._update_channel_limits_cache()
            self.write_log(f"[集中复制] 成功将源 [{src_display.split(' ')[1]}] 的设定参数应用至目标通道。")
            
            if self.is_connected and synced_keys and param_vars["range"].get():
                if getattr(self, "timer_running", False):
                    self.write_log("[提示] 当前正在采集测量中，集中复制的量程将在下次启动测量时同步。")
                else:
                    range_val = self.channel_vars[src_key]["range"].get()
                    threading.Thread(
                        target=self._bg_batch_set_ranges_now, 
                        args=(synced_keys, range_val), 
                        daemon=True
                    ).start()

            dialog.destroy()
            
        tk.Button(bottom_bar, text=" 📋 复制 ", bg="#007aff", fg="white", font=("Microsoft YaHei UI", 10, "bold"), relief="flat", width=12, command=commit_batch_copy, cursor="hand2").pack(side="right", padx=15)
        tk.Button(bottom_bar, text=" 取消 ", bg="#f2f2f7", fg="#007aff", font=("Microsoft YaHei UI", 10, "bold"), relief="flat", width=12, command=dialog.destroy, cursor="hand2").pack(side="right", padx=5)

    def _apply_config_dict(self, config):
        if "port" in config: self.port.set(config.get("port", ""))
        if "device_model" in config: self.device_model.set(config.get("device_model", "LR8401-21"))
        if "ch_30" in config: self.ch30_var.set(config.get("ch_30", False))
        if "interval" in config: self.interval_var.set(config.get("interval", "10ms"))
        if "soc" in config: self.case_name.set(config.get("soc", ""))
            
        channels_data = config.get("channels", config)
        for key, val in channels_data.items():
            if key in self.channel_vars:
                meta = self.channel_metadata[key]
                unit = meta["unit"]
                default_range = "10mV" if unit == 1 else "1V"
                c_val = val.get("comment", "")
                r_val = val.get("ratio", "")
                
                # 转换比标准化处理，完整支持并持久化正负转换比 (包含电压通道 -1 / +1)
                r_val = str(r_val).strip() if r_val is not None else ""

                self.channel_vars[key]["comment"].set(c_val)
                self.channel_vars[key]["range"].set(val.get("range", default_range))
                self.channel_vars[key]["ratio"].set(r_val)
                self._last_text_cache[f"{key}_comment"] = c_val
                self._last_text_cache[f"{key}_ratio"] = r_val
                if val.get("range"):
                    self.tested_channels.add(key)
        self.rebuild_scpi_channel_mapping()
        self._update_channel_limits_cache()

    def _generate_config_dict(self):
        config = {
            "port": self.port.get(),
            "baudrate": "921600",
            "device_model": self.device_model.get(), 
            "ch_30": self.ch30_var.get(),            
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

    def import_config_file(self):
        filepath = filedialog.askopenfilename(
            title="导入通道配置文件",
            filetypes=[("JSON Config Files", "*.json")]
        )
        if filepath:
            try:
                self.init_completed = False
                with open(filepath, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    self._apply_config_dict(config)
                self.init_completed = True
                self.config_unsynced = True
                
                self.pending_optimal_ranges.clear()
                self.pending_polarity_invert.clear()
                self.over_channels_to_upgrade.clear()
                self.over_ranged_channels.clear()
                self.alerted_over_channels.clear()
                self.instrument_ranges.clear()
                self.instrument_stores.clear()
                self.instrument_comments.clear()
                self.instrument_scalings.clear()
                for key in self.channel_keys:
                    self.sync_failed_state[key] = False
                
                self.save_local_config()
                self.write_log(f"[导入成功] 已加载配置: {os.path.basename(filepath)}")
                self._reset_gui_val_labels()
                
                if self.is_connected:
                    if getattr(self, "timer_running", False):
                        self.write_log("[提示] 当前正在采集测量中，导入的配置已保存在本地，将在下次开始采集时同步到仪器。")
                    else:
                        commented_keys = [k for k in self.channel_keys if self.channel_vars[k]["comment"].get().strip()]
                        if commented_keys:
                            self.write_log(f"[导入同步] 正在将导入的 {len(commented_keys)} 个有效通道配置物理下发至日置仪器...")
                            threading.Thread(target=self._bg_sync_all_commented_ranges_now, args=(commented_keys,), daemon=True).start()
            except Exception as e:
                self.init_completed = True
                self.write_log(f"[导入失败] 载入文件出错: {e}")

    def export_config_file(self):
        filepath = filedialog.asksaveasfilename(
            title="导出当前配置文件",
            defaultextension=".json",
            filetypes=[("JSON Config Files", "*.json")]
        )
        if filepath:
            try:
                config = self._generate_config_dict()
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
                self.write_log(f"[导出成功] 当前配置矩阵已保存至: {os.path.basename(filepath)}")
            except Exception as e:
                self.write_log(f"[导出失败] 写入文件出错: {e}")

    def write_log(self, text):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        formatted_text = f"[{timestamp}] {text}\n"
        with self.log_lock:
            self.log_queue.append(formatted_text)

    def _flush_log_queue(self):
        chunks = []
        with self.log_lock:
            while self.log_queue:
                chunks.append(self.log_queue.popleft())
        if chunks:
            self.log_area.insert(tk.END, "".join(chunks))
            line_count = int(self.log_area.index("end-1c").split(".")[0])
            if line_count > 3000:
                self.log_area.delete("1.0", f"{line_count - 2000}.0")
            self.log_area.see(tk.END)
        self.root.after(100, self._flush_log_queue)

    def toggle_connection(self):
        if not self.is_connected:
            connect_config = {
                "port": self.port.get().strip(),
                "baudrate": 921600,
                "device_model": self.device_model.get(),
            }
            threading.Thread(target=self._bg_connect, args=(connect_config,), daemon=True).start()
        else:
            self._disconnect_device()

    def _clear_serial_buffers(self):
        if self.ser and self.ser.is_open:
            try:
                if getattr(self.ser, "in_waiting", 0) > 0:
                    self.ser.read(self.ser.in_waiting)
                else:
                    self.ser.reset_input_buffer()
            except Exception:
                pass

    def _drain_serial_buffer(self):
        if self.ser and self.ser.is_open:
            try:
                self.ser.reset_input_buffer()
                start = time.monotonic()
                while time.monotonic() - start < 0.02:
                    in_w = getattr(self.ser, "in_waiting", 0)
                    if in_w > 0:
                        self.ser.read(in_w)
                        start = time.monotonic()
                    else:
                        time.sleep(0.001)
            except Exception:
                pass

    def _bg_connect(self, connect_config):
        port_val = connect_config["port"]
        probe_bauds = (921600, 115200)
        
        if port_val == "无可用COM口":
            self.write_log("[错误] 当前无可用串口，无法连接！")
            return
            
        for baud in probe_bauds:
            self.write_log(f"正在建立物理通信 {port_val} (波特率 {baud})...")
            try:
                if self.ser and self.ser.is_open:
                    try: self.ser.close()
                    except Exception: pass
                
                self.ser = serial.Serial(
                    port=port_val,
                    baudrate=baud,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.3
                )
                try:
                    self.ser.dtr = True
                    self.ser.rts = True
                except Exception:
                    pass
                
                time.sleep(0.05)
                self._clear_serial_buffers()
                
                self.send_raw_cmd(":HEADer OFF")
                idn = self.query_raw_cmd("*IDN?")
                
                if not idn or "HIOKI" not in idn.upper():
                    time.sleep(0.04)
                    idn = self.query_raw_cmd("*IDN?")
                    
                if idn and ("HIOKI" in idn.upper() or "LR" in idn.upper() or "84" in idn):
                    self.write_log(f"设备成功建立通信 (波特率 {baud}): {idn}")
                    self.root.after(0, lambda b=str(baud): self.baudrate.set(b))
                    
                    ch30_detected = False
                    is_lr8450 = "8450" in idn
                    
                    self.send_raw_cmd("*CLS")
                    self.send_raw_cmd(":CONFigure:ATSAve OFF")
                    if is_lr8450:
                        self.send_raw_cmd(":CONFigure:SAVEWave OFF")
                        self.send_raw_cmd(":CONFigure:SAVECalc OFF")
                    
                    cmd_set, cmd_query = self._get_filter_scpi()
                    self.send_raw_cmd(cmd_set)
                    time.sleep(0.05)
                    
                    f_res = clean_scpi_response(self.query_raw_cmd(cmd_query).strip())
                    if f_res:
                        self.write_log(f"[连接建立] 设定电网频率滤波器配置为: {f_res} - 已确认验证 ✓")
                        
                    if is_lr8450:
                        self.write_log("[自动检测] 识别到仪器实际型号为 HIOKI LR8450，自动转为 30ch 重映射模式。")
                        ch30_detected = True
                    else:
                        ans = self.query_raw_cmd(":UNIT:STORe? CH1_16")
                        if ans and ("ON" in ans.upper() or "OFF" in ans.upper()):
                            ch30_detected = True

                    self.root.after(0, self._on_connection_ready, is_lr8450, ch30_detected)
                    return
            except (serial.SerialException, OSError, Exception) as e:
                self.write_log(f"尝试波特率 {baud} 异常: {e}")
        
        self.write_log("握手无响应。请确认日置菜单中 [System]->[USB] 处于 Comm 模式。")
        self._disconnect_device()

    def _on_connection_ready(self, is_lr8450, ch30_detected):
        self.device_model.set("LR8450" if is_lr8450 else "LR8401-21")
        self.ch30_var.set(ch30_detected if not is_lr8450 else True)

        self.auto_set_shortest_interval(verbose=True)
        self.rebuild_scpi_channel_mapping()

        self.instrument_ranges.clear()
        self.instrument_stores.clear()
        self.instrument_comments.clear()
        self.instrument_scalings.clear()
        for key in self.channel_keys:
            self.sync_failed_state[key] = False

        self._update_channel_limits_cache()
        self.is_connected = True
        self.config_unsynced = True
        self._update_ui_connected()

        commented_keys = [k for k in self.channel_keys if self.channel_vars[k]["comment"].get().strip()]
        if commented_keys:
            self.write_log(f"[连接同步] 检测到当前控制台有 {len(commented_keys)} 个有注释的通道，正在初始化并配置物理仪器量程与转换比...")
            threading.Thread(
                target=self._bg_sync_all_commented_ranges_now,
                args=(commented_keys, is_lr8450),
                daemon=True
            ).start()
        
        threading.Thread(target=self._bg_set_interval_now, args=(self.interval_var.get(),), daemon=True).start()

    def _update_ui_connected(self):
        self.btn_connect.config(text=" 断开连接 ", bg="#f2f2f7", fg="#007aff")
        self.lbl_status.config(text="已连接", fg="#34c759")
        self.btn_start.config(state="normal")
        self.port_combo.config(state="disabled")
        self.model_combo.config(state="disabled")
        self.interval_combo.config(state="readonly")
        self.btn_import.config(state="disabled")
        self.btn_batch.config(state="normal")

    def _disconnect_device(self):
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                try: self.ser.close()
                except Exception: pass
            self.ser = None

        self.is_connected = False
        self.timer_running = False
        self.stop_monitor_event.set()
        self.monitor_thread = None
        
        self.instrument_ranges.clear()
        self.instrument_stores.clear()
        self.instrument_comments.clear()
        self.instrument_scalings.clear()
        self.gui_val_cache.clear()
        self.gui_stat_cache.clear()
        self._selection_visual_cache.clear()
        
        for key in self.channel_keys:
            self.sync_failed_state[key] = False

        self.root.after(0, self._dismiss_unified_alert_window)
        self.root.after(0, self._update_ui_disconnected)
        self.root.after(0, lambda: self._update_selection_visuals(force=True))
        self.write_log("已释放串口资源并清空量程寄存器与渲染缓存。")

    def _update_ui_disconnected(self):
        self.btn_connect.config(text=" 连接仪器 ", bg="#007aff", fg="white")
        self.lbl_status.config(text="未连接", fg="#ff3b30")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="disabled")
        self.port_combo.config(state="readonly")
        self.model_combo.config(state="readonly")
        self.interval_combo.config(state="readonly")
        self.btn_import.config(state="normal")
        self.btn_batch.config(state="normal")

    def send_raw_cmd(self, cmd):
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                try:
                    payload = cmd if isinstance(cmd, bytes) else f"{cmd}\r\n".encode('ascii')
                    self.ser.write(payload)
                    return True
                except Exception as e:
                    self.write_log(f"指令下发错误: {e}")
                    return False
            return False

    def query_raw_cmd(self, cmd, timeout=None):
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                old_timeout = self.ser.timeout
                try:
                    if timeout is not None:
                        self.ser.timeout = max(0.05, float(timeout))
                    payload = cmd if isinstance(cmd, bytes) else f"{cmd}\r\n".encode('ascii')
                    self.ser.write(payload)
                    return self.ser.readline().decode('ascii', errors='ignore').strip()
                except Exception as e:
                    self.write_log(f"指令查询错误: {e}")
                    return ""
                finally:
                    if timeout is not None:
                        self.ser.timeout = old_timeout
            return ""

    def query_raw_fast(self, cmd):
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                try:
                    payload = cmd if isinstance(cmd, bytes) else f"{cmd}\r\n".encode('ascii')
                    self.ser.write(payload)
                    return self.ser.readline().decode('ascii', errors='ignore').strip()
                except Exception:
                    return ""
            return ""

    def _schedule_save(self, *args):
        if not getattr(self, "init_completed", False):
            return
        self.config_unsynced = True
        if self._save_timer_id is not None:
            self.root.after_cancel(self._save_timer_id)
        self._save_timer_id = self.root.after(500, self.save_local_config)

    def save_local_config(self):
        if not getattr(self, "init_completed", False):
            return
        
        if self._save_timer_id is not None:
            self.root.after_cancel(self._save_timer_id)
            self._save_timer_id = None
            
        self.auto_set_shortest_interval(verbose=True)
        config = self._generate_config_dict()
        tmp_path = "hioki_config.json.tmp"
        final_path = "hioki_config.json"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, final_path)
        except Exception as e:
            self.write_log(f"[配置保存失败] {e}")

    def load_local_config(self):
        if os.path.exists("hioki_config.json"):
            try:
                with open("hioki_config.json", "r", encoding="utf-8") as f:
                    config = json.load(f)
                    self._apply_config_dict(config)
                self.write_log("[配置读取] 成功恢复上次运行的配置信息。")
            except Exception as e:
                self.write_log(f"[配置读取失败] {e}")

    def on_close_save(self):
        self.save_local_config()
        self._disconnect_device()
        self.root.destroy()

    def _reset_gui_val_labels(self):
        self.gui_val_cache.clear()
        self.gui_stat_cache.clear()
        
        for vars_dict in self.channel_vars.values():
            row_bg = vars_dict["row_bg"]
            for lbl_key in ("lbl_val", "lbl_avg", "lbl_max", "lbl_min"):
                lbl = vars_dict[lbl_key]
                if lbl:
                    lbl.config(text="--", fg="#8e8e93", bg=row_bg, highlightthickness=0)

    def _update_gui_val_label(self, key, val):
        lbl = self.channel_vars[key]["lbl_val"]
        row_bg = self.channel_vars[key]["row_bg"]
        if lbl:
            if val is None:
                target_text, fg, bg = "--", "#8e8e93", row_bg
            elif math.isnan(val):
                target_text, fg, bg = "OVER", "#ffffff", "#ff3b30"
            else:
                is_ma = self.is_ma_channel.get(key, False)
                fg = "#ff3b30" if val < 0 else ("#34c759" if is_ma else "#007aff")
                target_text = f"{val:.3f} mA" if is_ma else f"{val:.4f} V"
                bg = row_bg
            
            cache_entry = self.gui_val_cache.get(key)
            if cache_entry != (target_text, fg, bg):
                lbl.config(text=target_text, fg=fg, bg=bg)
                self.gui_val_cache[key] = (target_text, fg, bg)

    def _batch_update_gui_vals(self, updates):
        for key, val in updates:
            self._update_gui_val_label(key, val)

    def _update_gui_stat_labels(self, key, avg, max_val, min_val):
        is_ma = self.is_ma_channel.get(key, False)
        fmt = ".3f" if is_ma else ".4f"
        unit = " mA" if is_ma else " V"
        row_bg = self.channel_vars[key]["row_bg"]
        
        avg_text, avg_fg = ("--", "#8e8e93") if (avg is None or math.isnan(avg)) else (f"{avg:{fmt}}{unit}", "#ff3b30" if avg < 0 else "#1c1c1e")
        max_text, max_fg = ("--", "#8e8e93") if (max_val is None or math.isnan(max_val)) else (f"{max_val:{fmt}}{unit}", "#ff3b30" if max_val < 0 else "#1c1c1e")
        min_text, min_fg = ("--", "#8e8e93") if (min_val is None or math.isnan(min_val)) else (f"{min_val:{fmt}}{unit}", "#ff3b30" if min_val < 0 else "#1c1c1e")
            
        target_state = (avg_text, avg_fg, max_text, max_fg, min_text, min_fg)
        cache_entry = self.gui_stat_cache.get(key)
        
        if cache_entry != target_state:
            for lbl, txt, color in (
                (self.channel_vars[key]["lbl_avg"], avg_text, avg_fg),
                (self.channel_vars[key]["lbl_max"], max_text, max_fg),
                (self.channel_vars[key]["lbl_min"], min_text, min_fg)
            ):
                if lbl:
                    lbl.config(text=txt, fg=color, bg=row_bg)
            self.gui_stat_cache[key] = target_state

    def _batch_update_gui_stats(self, updates):
        for key, avg, max_val, min_val in updates:
            self._update_gui_stat_labels(key, avg, max_val, min_val)

    def _update_channel_limits_cache(self):
        self.is_ma_channel = {}
        self.channel_limits_cache = {}
        for key in self.channel_keys:
            comment = self.channel_vars[key]["comment"].get().strip()
            ratio_text = self.channel_vars[key]["ratio"].get().strip()
            is_curr = is_current_channel(comment, ratio_text)
            self.is_ma_channel[key] = is_curr
            
            r_text = self.channel_vars[key]["range"].get()
            limit_volt = RANGE_LIMITS_VOLTS.get(r_text, 0.01)
            ratio_val = 1.0
            is_scaled = False
            if ratio_text:
                try:
                    val = float(ratio_text)
                    if abs(val) > 0:
                        ratio_val = val
                        is_scaled = True
                except ValueError:
                    pass
            
            self.channel_limits_cache[key] = {
                "limit_volt": limit_volt,
                "ratio_val": ratio_val,
                "is_scaled": is_scaled,
                "is_current": is_curr
            }

    def start_acquisition(self):
        """
        开始采集 (Start) 时序逻辑：
        第 1 步：优先执行实测负均值智能极性归正；
        第 2 步：起步初始化适配；
        第 3 步：应用自适应量程与超量程升档目标；
        第 4 步：硬件同步并启动硬件连续记录。
        """
        self.btn_connect.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.btn_batch.config(state="disabled") 
        self.total_frames = 0
        self._reset_gui_val_labels()
        
        # 1. 优先执行：实测平均值为负的通道，自动新增或删除转换比负号 (极性归正)
        if getattr(self, "pending_polarity_invert", None):
            for key in tuple(self.pending_polarity_invert):
                vars_dict = self.channel_vars[key]
                comment = vars_dict["comment"].get().strip()
                if not comment:
                    continue
                cur_ratio = vars_dict["ratio"].get().strip()
                new_ratio = invert_ratio_sign(cur_ratio)
                vars_dict["ratio"].set(new_ratio)
                self._last_text_cache[f"{key}_ratio"] = new_ratio
                ch_display = self.scpi_channel_mapping.get(key, key)
                is_curr = is_current_channel(comment, new_ratio)
                type_name = "电流" if is_curr else "电压"
                self.write_log(
                    f"[极性自动归正] {ch_display} ({comment}) 上次实测{type_name}平均值为负，已自动反转转换比符号: {cur_ratio or '1'} → {new_ratio}"
                )
            self.pending_polarity_invert.clear()

        # 2. 测量类型初次起步适配 (仅对从未设定过的新通道)
        for key in self.channel_keys:
            comment = self.channel_vars[key]["comment"].get().strip()
            if not comment:
                continue
            if key in self.tested_channels or key in self.over_ranged_channels:
                continue
            
            ratio_str = self.channel_vars[key]["ratio"].get().strip()
            is_curr = is_current_channel(comment, ratio_str)
            curr_range = self.channel_vars[key]["range"].get()
            if is_curr:
                if curr_range in ("1V", "2V", "10V", "20V", "100V") and key not in self.pending_optimal_ranges:
                    self.channel_vars[key]["range"].set("10mV")
                    ch_display = self.scpi_channel_mapping.get(key, key)
                    self.write_log(f"[自适应起步] {ch_display} ({comment}) 判定为电流通道，首次自动配置为最高分辨率 10mV 量程")
            else:
                if curr_range in ("10mV", "20mV", "100mV", "200mV") and key not in self.pending_optimal_ranges:
                    self.channel_vars[key]["range"].set("1V")
                    ch_display = self.scpi_channel_mapping.get(key, key)
                    self.write_log(f"[自适应起步] {ch_display} ({comment}) 判定为电压通道，首次自动配置为基准 1V 量程")

        # 3. 待调档应用
        if self.pending_optimal_ranges:
            for key, target_range in tuple(self.pending_optimal_ranges.items()):
                vars_dict = self.channel_vars[key]
                comment = vars_dict["comment"].get().strip()
                if not comment: 
                    continue
                
                curr_range = vars_dict["range"].get()
                if target_range != curr_range:
                    vars_dict["range"].set(target_range)
                    ch_display = self.scpi_channel_mapping.get(key, key)
                    self.write_log(f"[自适应量程生效] {ch_display} ({comment}) 量程已成功更新为: {target_range} (原: {curr_range})")

        for key in self.channel_keys:
            if self.channel_vars[key]["comment"].get().strip():
                self.tested_channels.add(key)
        
        self.over_channels_to_upgrade.clear()
        self.pending_optimal_ranges.clear()
        self.alerted_over_channels.clear()
        self.unified_alert_items.clear()
        self._dismiss_unified_alert_window()

        for key in self.channel_keys:
            self.stats[key].reset()

        self.auto_set_shortest_interval(verbose=False)
        self._update_channel_limits_cache()
        self.save_local_config()
        
        threading.Thread(target=self._bg_start_task, daemon=True).start()

    def _bg_start_task(self):
        self.send_raw_cmd("*CLS")
        self.send_raw_cmd(":CONFigure:ATSAve OFF")
        is_lr8450 = (self.device_model.get() == "LR8450")
        if is_lr8450:
            self.send_raw_cmd(":CONFigure:SAVEWave OFF")
            self.send_raw_cmd(":CONFigure:SAVECalc OFF")

        pending_sync_keys = set()
        for key in self.channel_keys:
            comment = self.channel_vars[key]["comment"].get().strip()
            if comment:
                if (self.sync_failed_state.get(key, False) or 
                    self.instrument_ranges.get(key) != self.channel_vars[key]["range"].get() or
                    self.instrument_stores.get(key) is not True):
                    pending_sync_keys.add(key)

        if getattr(self, "config_unsynced", True):
            self.write_log(">>> 检测到配置有变动，正在物理重构写入通道矩阵与硬件变比...")
            cmd_set, cmd_query = self._get_filter_scpi()
            self.send_raw_cmd(cmd_set)
            time.sleep(0.05)
            
            f_res = clean_scpi_response(self.query_raw_cmd(cmd_query).strip())
            if f_res:
                self.write_log(f">>> 设定滤波器配置为: 50Hz (设备读取状态: {f_res} ✓)")
            
            for key in self.channel_keys:
                comment = self.channel_vars[key]["comment"].get().strip()
                ch_id = self.scpi_channel_mapping[key]
                success, actual_range, skipped = self._sync_single_channel_hardware(key, force_set=False)
                if comment and not skipped:
                    if success:
                        self.write_log(f"[物理配置验证] {ch_id} ({comment}) 物理配置及变比已下发: {actual_range} ✓")
                    else:
                        self.write_log(f"[警告] {ch_id} ({comment}) 配置同步失败！标红待下次自动重试！当前实际: {actual_range}")
            
            self.send_raw_cmd(":DISPlay:CHANge DISPlay")
            self.send_raw_cmd(":DISPlay:PAGE 1")
            self.send_raw_cmd(":DISPlay:MODE W_D")
            self.send_raw_cmd(":DISPlay:GROUp ALL")
            self.config_unsynced = False
        elif pending_sync_keys:
            self.write_log(f">>> 正在对 {len(pending_sync_keys)} 个变动/升档通道进行硬件自动重新同步...")
            for key in pending_sync_keys:
                comment = self.channel_vars[key]["comment"].get().strip()
                ch_id = self.scpi_channel_mapping[key]
                success, actual_range, _ = self._sync_single_channel_hardware(key, force_set=True)
                if success:
                    self.write_log(f"[自动重试同步成功] {ch_id} ({comment}) 的物理配置及量程成功应用: {actual_range} ✓")
                else:
                    self.write_log(f"[自动重试同步失败] ⚠️ {ch_id} ({comment}) 依旧无法写入量程！当前实际: {actual_range}")
        else:
            self.write_log(">>> 通道配置与仪器完全一致，瞬间就绪开始采集！")
            
        self.root.after(0, self._update_selection_visuals)
        self._update_channel_limits_cache()

        interval_val = self.interval_var.get()
        ok_int, rb_int = self._sync_hardware_interval(interval_val)
        if ok_int:
            self.write_log(f">>> 硬件记录间隔已严格同步为: {interval_val} (验证: {rb_int} ✓)")
        else:
            self.write_log(f"[警告] ⚠️ 物理记录间隔 {interval_val} 下发未获确认！当前设备实际回读: {rb_int or '无响应'}")

        self.write_log(">>> 正在启动测量记录 (硬件内部无损连续记录启动)...")
        self.dt_start = datetime.now()
        
        self.send_raw_cmd(":STARt")
        time.sleep(0.08)  # 留足 80ms 硬件继电器闭合与前端电容建立时间
        
        self.root.after(0, self._start_ui_timer)

    def _start_ui_timer(self):
        self.timer_running = True
        self.stop_monitor_event.clear()
        self.start_time = time.monotonic()
        self.btn_stop.config(state="normal")
        
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                self.ser.timeout = 0.15
                
        self.monitor_thread = threading.Thread(target=self._bg_realtime_stream_worker, daemon=True)
        self.monitor_thread.start()
        self._update_timer_loop()
        self.write_log("【硬件全采样对齐模式】仪器内部正在以极高时钟连续将 100% 数据点写入板载存储器，上位机平滑监视中...")

    def _update_timer_loop(self):
        if self.timer_running:
            self.elapsed_time = time.monotonic() - self.start_time
            mins = int(self.elapsed_time // 60)
            secs = int(self.elapsed_time % 60)
            tenths = int((self.elapsed_time * 10) % 10)
            self.lbl_timer.config(text=f"{mins:02d}:{secs:02d}.{tenths:d}")
            self.root.after(100, self._update_timer_loop)

    def _bg_realtime_stream_worker(self):
        self.active_channels = []
        for key in self.channel_keys:
            comment = self.channel_vars[key]["comment"].get().strip()
            if comment:
                meta = self.channel_metadata[key]
                scpi_ch = self.scpi_channel_mapping[key]
                lim_info = self.channel_limits_cache[key]
                r_val = lim_info["ratio_val"]
                self.active_channels.append({
                    "key": key,
                    "unit": meta["unit"],
                    "ch": meta["ch"],
                    "comment": comment,
                    "range": self.channel_vars[key]["range"].get(),
                    "ratio": self.channel_vars[key]["ratio"].get().strip(),
                    "scpi_ch": scpi_ch,
                    "query_bytes": f":MEMory:VREAl? {scpi_ch}\r\n".encode('ascii'),
                    "limit_volt": lim_info["limit_volt"],
                    "limit_volt_thresh": lim_info["limit_volt"] * 1.02,
                    "ratio_val": r_val,
                    "inv_ratio": (1.0 / r_val) if (r_val and r_val != 0) else 1.0,
                    "is_scaled": lim_info["is_scaled"],
                    "is_current": lim_info["is_current"]
                })
                    
        num_active = len(self.active_channels)
        if num_active == 0:
            self.write_log("[提示] 没有已配置注释的有效通道。")
            return

        CMD_GETREAL_BYTES = b":MEMory:GETReal\r\n"
        last_gui_stat_update = time.monotonic()
        warmup_frames = 2  # 启动前 2 帧抑制窗口，消除继电器闭合瞬间的过渡态毛刺

        while self.timer_running and not self.stop_monitor_event.is_set():
            t_loop_start = time.perf_counter()
            self.send_raw_cmd(CMD_GETREAL_BYTES)
            
            gui_vals = []
            frame_valid = True
            
            for chan in self.active_channels:
                if not self.timer_running or self.stop_monitor_event.is_set():
                    break
                
                raw_resp = self.query_raw_fast(chan["query_bytes"])
                val_adc, status = parse_single_chan_response(raw_resp)
                
                k = chan["key"]
                limit_volt = chan["limit_volt"]
                is_curr = chan["is_current"]
                chan_stat = self.stats[k]
                
                if status == "OVER" or (val_adc is not None and math.isnan(val_adc)):
                    gui_vals.append(math.nan)
                    if warmup_frames <= 0:
                        chan_stat.mark_over()
                    if k not in self.alerted_over_channels and self.timer_running and not self.stop_monitor_event.is_set():
                        self.alerted_over_channels.add(k)
                        self.over_channels_to_upgrade.add(k)
                        self.over_ranged_channels.add(k)  # 超量程锁定
                        target_range = get_next_range(chan["range"])
                        self.pending_optimal_ranges[k] = target_range
                        self.write_log(f"[硬溢出告警] 通道 {chan['scpi_ch']} ({chan['comment']}) 发生 OVER 物理硬件溢出！下次采集将自动升档为 {target_range}。")
                        self.root.after(0, self._post_unified_alert, k, chan['scpi_ch'], chan['comment'], "OVER", "OVER", f"{limit_volt:g}V", target_range)
                elif val_adc is None or status in ("INVALID", "EMPTY"):
                    gui_vals.append(None)
                    frame_valid = False
                else:
                    val_calc = val_adc
                    raw_volt = abs(val_adc * chan["inv_ratio"]) if is_curr else abs(val_adc)
                    gui_vals.append(val_calc)
                    
                    if warmup_frames <= 0:
                        chan_stat.add(val_calc, raw_volt)

                    if raw_volt > chan["limit_volt_thresh"] and k not in self.alerted_over_channels and self.timer_running and not self.stop_monitor_event.is_set():
                        self.alerted_over_channels.add(k)
                        self.over_channels_to_upgrade.add(k)
                        self.over_ranged_channels.add(k)  # 超量程锁定
                        
                        optimal_target = get_optimal_range_for_channel(raw_volt, is_current=is_curr)
                        if RANGE_INDEX.get(optimal_target, 0) <= RANGE_INDEX.get(chan["range"], 0):
                            target_range = get_next_range(chan["range"])
                        else:
                            target_range = optimal_target
                        self.pending_optimal_ranges[k] = target_range
                        
                        unit_str = "mA" if is_curr else "V"
                        fmt_str = ".3f" if is_curr else ".4f"
                        cur_str = f"{val_calc:{fmt_str}} {unit_str}"
                        lim_str = f"{limit_volt*1000:.1f} mV" if limit_volt < 1.0 else f"{limit_volt:g} V"
                        msg_log = (
                            f"[超量程告警] 通道 {chan['scpi_ch']} ({chan['comment']}) 测得信号超出量程上限！"
                            f"物理ADC采样电压 {raw_volt*1000:.3f}mV (当前测量值: {cur_str})，超出 {lim_str} 量程极限！下次采集将自动调整为 {target_range}。"
                        )
                        self.write_log(msg_log)
                        self.root.after(0, self._post_unified_alert, k, chan['scpi_ch'], chan['comment'], "SOFT_OVER", cur_str, lim_str, target_range)
                
            if not self.timer_running or self.stop_monitor_event.is_set():
                break
                
            if warmup_frames > 0:
                warmup_frames -= 1

            if frame_valid and len(gui_vals) == num_active and self.timer_running:
                self.total_frames += 1
                updates = [(chan["key"], gui_vals[idx]) for idx, chan in enumerate(self.active_channels)]
                self.root.after(0, self._batch_update_gui_vals, updates)
                
                now = time.monotonic()
                if now - last_gui_stat_update >= 0.3:
                    stats_updates = []
                    for chan in self.active_channels:
                        k = chan["key"]
                        st = self.stats[k]
                        if st.count > 0:
                            stats_updates.append((k, st.avg, st.max_val, st.min_val))
                        else:
                            stats_updates.append((k, math.nan, math.nan, math.nan))
                    self.root.after(0, self._batch_update_gui_stats, stats_updates)
                    last_gui_stat_update = now

            t_elapsed = time.perf_counter() - t_loop_start
            t_sleep = 0.04 - t_elapsed
            if t_sleep > 0.002 and self.timer_running:
                time.sleep(t_sleep)

    def stop_acquisition(self):
        """
        点击停止采集 (Stop) 时序架构：
        【零延迟极速同步】：主线程直接在毫秒内完成统计计算，并由 Win32 原生 API 秒推 Windows 剪贴板！
        彻底杜绝 Alt+Tab 切换到 Excel 发生粘贴空白或旧内容的任何时序倒挂问题！
        """
        self.timer_running = False
        self.stop_monitor_event.set()
        try: self.btn_stop.config(state="disabled")
        except Exception: pass
        self.dt_end = datetime.now()
        
        self._dismiss_unified_alert_window()

        # 主线程立即就地计算并秒级推入 Windows 内核剪贴板
        self._save_collected_data()

        self.write_log(">>> 正在下发硬件停止信号 (:STOP) 并释放物理句柄...")
        threading.Thread(target=self._bg_stop_hardware_worker, daemon=True).start()

    def _bg_stop_hardware_worker(self):
        """
        后台硬件异步停止线程：
        独立负责向串口下发 :ABORT 与 :STOP，完全不阻碍主线程剪贴板与 UI 响应！
        """
        try:
            monitor = self.monitor_thread
            if monitor and monitor.is_alive() and monitor is not threading.current_thread():
                monitor.join(timeout=0.2)
            self.monitor_thread = None

            time.sleep(0.01)
            self.send_raw_cmd(":ABORT")
            self.send_raw_cmd(":STOP")
            self._drain_serial_buffer()

            # 尝试回读板载实际记录点数以做日志审计确认
            resp_pts = self.query_raw_cmd(":MEMory:MAXPoint?", timeout=0.08)
            if resp_pts and resp_pts.isdigit():
                pts_val = int(resp_pts)
                if pts_val > 0:
                    self.write_log(f"[硬件内存确认] 设备板载存储记录点数: {pts_val} 点 ✓")
        except Exception as e:
            self.write_log(f"[停止硬件异常] {e}")
        finally:
            self.root.after(0, self._restore_ui_after_task)

    def _save_collected_data(self):
        """
        毫秒级数据结算与自动分列报表导出：
        1. 极性闭环：实测均值为负则登记，下次点开始自动反转转换比符号；
        2. 电压双向自适应：0.72V 选 2V 自动降为 1V，选 200mV 自动升为 1V；< 0.05V (断电) 维持原量程不变；
        3. 电流单向升档锁定；
        4. 剪贴板严格采用原生制表符格式，确保 Excel B1 单元格 Ctrl+V 自动完美分列！
        """
        active_channels = getattr(self, "active_channels", [])
        if not active_channels:
            self.write_log("[错误] 无活跃通道配置记录！")
            return

        try:
            interval_sec = parse_interval_seconds(self.interval_var.get())
        except ValueError:
            interval_sec = 0.01

        elapsed_sec = (self.dt_end - self.dt_start).total_seconds() if (self.dt_start and self.dt_end) else 0.0
        hw_points_exact = max(1, round(elapsed_sec / interval_sec)) if interval_sec > 0 else self.total_frames
        total_hw_points = hw_points_exact

        self.write_log(
            f"【硬件全采样对齐】物理记录持续: {elapsed_sec:.3f}s | "
            f"采样基准: {self.interval_var.get()} ({interval_sec*1000:g}ms) | "
            f"估算记录点数: {total_hw_points} 点 (100% 原始数据对齐 ✓)"
        )

        stats_final_updates = []
        stats = {}
        for chan in active_channels:
            k = chan["key"]
            st = self.stats[k]
            
            if st.count > 0:
                avg_val = st.avg
                max_val = st.max_val
                min_val = st.min_val
            else:
                avg_val, max_val, min_val = math.nan, math.nan, math.nan

            stats_final_updates.append((k, avg_val, max_val, min_val))
            stats[k] = {
                "points": total_hw_points,
                "valid_count": total_hw_points if not math.isnan(avg_val) else 0,
                "avg": avg_val,
                "max": max_val,
                "min": min_val
            }
            
            # 规则 1：实测平均值为负数，登记下次点开始自动反转符号
            if not math.isnan(avg_val) and avg_val < -1e-5:
                self.pending_polarity_invert.add(k)
            else:
                self.pending_polarity_invert.discard(k)

            # 规则 2：量程智能自适应与超量程保护决策
            cur_range = chan["range"]
            cur_idx = RANGE_INDEX.get(cur_range, 0)
            had_over = (k in self.over_channels_to_upgrade)

            if chan["is_current"]:
                # 电流通道：如果发生超量程或峰值接近上限，只升不降 (单向棘轮)
                if had_over:
                    target_r = get_next_range(cur_range)
                    self.pending_optimal_ranges[k] = target_r
                    self.over_ranged_channels.add(k)
                else:
                    opt_r = get_optimal_range_for_channel(st.raw_peak, is_current=True)
                    if RANGE_INDEX.get(opt_r, 0) > cur_idx:
                        self.pending_optimal_ranges[k] = opt_r
                        self.over_ranged_channels.add(k)
            else:
                # 电压通道：
                # A. 物理超量程溢出，强制升档并锁定
                if had_over:
                    target_r = get_next_range(cur_range)
                    self.pending_optimal_ranges[k] = target_r
                    self.over_ranged_channels.add(k)
                elif not math.isnan(avg_val):
                    v_abs = abs(avg_val)
                    # B. 0V 掉电保护：实测电压接近 0V (< 0.05V，已断电)，保持原自适应量程不变！
                    if v_abs < 0.05:
                        self.pending_optimal_ranges.pop(k, None)
                    else:
                        # C. 正常通电状态，严格根据实测电压平均值双向自适应 (支持自动降档与升档)
                        if v_abs <= 1.0:
                            adaptive_r = "1V"
                        elif v_abs <= 2.0:
                            adaptive_r = "2V"
                        elif v_abs <= 10.0:
                            adaptive_r = "10V"
                        elif v_abs <= 20.0:
                            adaptive_r = "20V"
                        else:
                            adaptive_r = "100V"

                        if adaptive_r != cur_range:
                            self.pending_optimal_ranges[k] = adaptive_r
                            ch_display = self.scpi_channel_mapping.get(k, k)
                            action_name = "自动降档" if RANGE_INDEX[adaptive_r] < cur_idx else "自动升档"
                            self.write_log(
                                f"[电压自适应规划] {ch_display} ({chan['comment']}) 实测平均值为 {avg_val:.3f}V，将{action_name}至最佳量程: {adaptive_r} (原: {cur_range})"
                            )
                        else:
                            self.pending_optimal_ranges.pop(k, None)
                        
        self.last_test_stats = dict(stats)
        self._batch_update_gui_stats(stats_final_updates)

        # 立即在界面下拉框直观更新自适应目标量程
        if self.pending_optimal_ranges:
            for k, tgt_r in self.pending_optimal_ranges.items():
                self.channel_vars[k]["range"].set(tgt_r)
            self._save_timer_id = self.root.after(100, self.save_local_config)

        self.write_log("数据提取完毕。正在生成符合 Logger utility 标准格式的统计报告...")
        
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
        
        # 严格采用原版制表符格式，恢复 Excel 原生自动分列
        for key in self.channel_keys:
            if key in active_keys:
                st_info = stats.get(key)
                if not st_info or math.isnan(st_info["avg"]):
                    fmt_str = f"{key}\tOVER\tOVER\tOVER"
                else:
                    mean_val = st_info["avg"]
                    max_val = st_info["max"]
                    min_val = st_info["min"]
                    fmt_str = f"{key}\t{mean_val:.3f}\t{max_val:.3f}\t{min_val:.3f}"
                lines.append(fmt_str)
                
        clipboard_text = "\n".join(lines)
        self._copy_to_clipboard(clipboard_text)

    def _copy_to_clipboard(self, text):
        self.last_clipboard_text = text
        # 优先使用 Windows 原生 Win32 API 写入物理全局内存，成功则立即返回，彻底杜绝 Tkinter 懒加载与白屏清空冲突
        if set_system_clipboard_native(text):
            self.write_log("[自动复制] ★★★ 统计结果已秒级自动写入系统剪贴板！ ★★★")
            self.write_log("[自动复制] 您可直接在已有 Excel 的 B1 单元格 Ctrl+V，数据即会自动完美分列！")
            return

        # 跨平台环境兜底
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            self.write_log("[自动复制] ★★★ 统计结果已秒级自动写入系统剪贴板！ ★★★")
            self.write_log("[自动复制] 您可直接在已有 Excel 的 B1 单元格 Ctrl+V，数据即会自动完美分列！")
        except Exception:
            pass

    def copy_clipboard_data(self):
        if getattr(self, "last_clipboard_text", ""):
            if set_system_clipboard_native(self.last_clipboard_text):
                self.write_log("[快捷复制] ★★★ 统计结果已成功手动复制到剪贴板！ ★★★")
                return
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(self.last_clipboard_text)
                self.write_log("[快捷复制] ★★★ 统计结果已成功手动复制到剪贴板！ ★★★")
            except Exception:
                pass
        else:
            messagebox.showwarning("复制失败", "当前暂无有效的测试统计数据，请先启动采集并成功停止。")

    def _restore_ui_after_task(self):
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                self.ser.timeout = 0.3
                
        self.btn_connect.config(text=" 断开连接 " if self.is_connected else " 连接仪器 ", bg="#f2f2f7" if self.is_connected else "#007aff", fg="#007aff" if self.is_connected else "white")
        self.btn_connect.config(state="normal")
        self.btn_start.config(state="normal" if self.is_connected else "disabled")
        self.btn_stop.config(state="disabled")
        self.port_combo.config(state="readonly" if not self.is_connected else "disabled")
        self.model_combo.config(state="readonly" if not self.is_connected else "disabled")
        self.interval_combo.config(state="readonly")
        self.btn_import.config(state="normal" if not self.is_connected else "disabled")
        self.btn_batch.config(state="normal")


if __name__ == "__main__":
    root = tk.Tk()
    app = HiokiPerfectApp(root)
    root.mainloop()
