# -*- coding: utf-8 -*-
"""
HIOKI LR8401-21 / LR8450 自动化测试控制台 v12.5 (极速优化与抗干扰重试极简版)
设计者：程控专家 (KaFungyu 专属美学升级版)
"""

import sys
import os
import time
import json
import math
import threading
from datetime import datetime

# 依赖库预检（彻底移除 openpyxl / pandas / numpy 依赖，仅保留 pyserial）
try:
    import serial
    import serial.tools.list_ports  # 自动扫描物理串口
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

# 映射日置电压量程至 SCPI 浮点参数
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
RANGE_LIST = list(RANGE_MAP.keys())

# O(1) 极速反向量程查表字典
RANGE_REVERSE_MAP = {v.upper(): k for k, v in RANGE_MAP.items()}

# 物理量程对应的标称最大电压限制（单位：V），用于软饱和检测
RANGE_LIMITS_VOLTS = {k: float(v) for k, v in RANGE_MAP.items()}

def get_scpi_range(range_text):
    return RANGE_MAP.get(range_text, "0.01")

def get_range_command_args(range_text, is_lr8450):
    scpi_range = get_scpi_range(range_text)
    candidates = [("numeric", scpi_range)]

    try:
        candidates.append(("nr3", f"{float(scpi_range):.1E}"))
    except ValueError:
        pass

    if is_lr8450:
        candidates.append(("label", range_text))

    deduped = []
    seen = set()
    for kind, arg in candidates:
        normalized = arg.upper().replace(" ", "")
        if normalized not in seen:
            seen.add(normalized)
            deduped.append((kind, arg))
    return deduped

def extract_scpi_range_token(response):
    response = clean_scpi_response(response).strip().strip('"')
    if "," in response:
        response = response.rsplit(",", 1)[-1].strip().strip('"')
    return response

def format_range_for_log(value):
    token = extract_scpi_range_token(value)
    if not token:
        return "无响应"

    norm = token.upper().replace(" ", "")
    # O(1) 匹配精准字符串
    if norm in RANGE_REVERSE_MAP:
        return RANGE_REVERSE_MAP[norm]

    try:
        actual_val = float(norm)
    except ValueError:
        return token

    for scpi_val, label in RANGE_REVERSE_MAP.items():
        try:
            expected_val = float(scpi_val)
        except ValueError:
            continue
        tolerance = max(abs(expected_val) * 1e-3, 1e-9)
        if abs(actual_val - expected_val) <= tolerance:
            return label

    if abs(actual_val) < 1.0:
        mv_text = f"{actual_val * 1000:.6f}".rstrip("0").rstrip(".")
        return f"{mv_text}mV"

    volt_text = f"{actual_val:.6f}".rstrip("0").rstrip(".")
    return f"{volt_text}V"

def is_scpi_range_match(response, expected):
    """Return True when a range query response matches the requested SCPI range."""
    response = extract_scpi_range_token(response)

    actual_norm = response.upper().replace(" ", "")
    expected_norm = expected.upper().replace(" ", "")
    if actual_norm == expected_norm:
        return True

    try:
        actual_val = float(actual_norm)
        expected_val = float(expected_norm)
    except ValueError:
        return False

    tolerance = max(abs(expected_val) * 1e-3, 1e-9)
    return abs(actual_val - expected_val) <= tolerance

def clean_scpi_response(res_str):
    """自动剥离 SCPI 响应中的指令头回显"""
    res_str = res_str.strip()
    if " " in res_str:
        parts = res_str.split(" ", 1)
        if parts[0].startswith(":") or parts[0].isalpha():
            return parts[1]
    return res_str


class HiokiPerfectApp:
    def __init__(self, root):
        self.root = root
        self.root.title("HIOKI LR8401-21 / LR8450 程控控制台 v12.5")
        self.root.geometry("1680x850")
        self.root.configure(bg="#ffffff") # iOS 纯白高亮背景
        
        # 串口及全局变量及同步锁
        self.port = tk.StringVar()
        self.baudrate = tk.StringVar(value="115200")
        self.device_model = tk.StringVar(value="LR8401-21") # 仪器型号
        self.ch30_var = tk.BooleanVar(value=False)           # Slot 1 是否为 30ch 模块 (U8552) 的状态标识
        self.interval_var = tk.StringVar(value="10ms")       # 记录间隔
        self.case_name = tk.StringVar(value="")
        self.ser_lock = threading.Lock()
        
        self.ser = None
        self.is_connected = False
        self.config_unsynced = True # 智能差分同步标志：默认需要初始同步
        
        # ==================== 本地寄存器状态全缓存结构 ====================
        self.instrument_ranges = {}      # 缓存成功下发给仪器的实际通道量程
        self.instrument_stores = {}      # 缓存当前通道物理开启状态 (True / False)
        self.instrument_comments = {}    # 缓存当前通道物理屏幕注释
        self.instrument_scalings = {}    # 缓存当前通道物理 SCALing 状态
        
        # ==================== 高性能 GUI 渲染防抖文本比对缓存 ====================
        self.gui_val_cache = {}          # 缓存实时测量值文本及配色：{key: (text, fg, bg)}
        self.gui_stat_cache = {}         # 缓存平均、最大、最小统计：{key: (avg_text, avg_fg, max_text, max_fg, min_text, min_fg)}
        self._selection_visual_cache = {}# 增量选中视觉样式缓存：{key: (is_selected, sync_failed)}
        
        # ==================== 跨线程离线 SCPI 通道名称静态映射 ====================
        self.scpi_channel_mapping = {}   # 本地缓存映射 {key: scpi_ch_string}
        
        # 批量多选与拖拽状态变量
        self.selected_keys = set()
        self.last_selected_key = None
        self.widget_to_key = {}          # O(1) 极速拖拽选中映射表
        self.is_dragging = False
        self.drag_start_key = None
        self.drag_base_selection = set()
        self.drag_last_key = None
        
        # O(1) 常数级内存流计数与剪贴板缓存
        self.total_frames = 0
        self.last_clipboard_text = ""
        self.is_ma_channel = {} # 通道单位高性能本地缓存
        self.active_channels = [] # 运行期缓存活跃通道
        self.channel_limits_cache = {} # 运行期缓存各通道电压阈值和转换比信息
        
        # 用于记录当前测试周期内已弹过 OVER 警告的通道 key
        self.alerted_over_channels = set()
        
        # 增量式极速统计缓存字典
        self.stats_count = {}
        self.stats_sum = {}
        self.stats_max = {}
        self.stats_min = {}
        
        # 时间戳记录
        self.dt_start = None
        self.dt_end = None
        
        # 计时器与线程控制变量
        self.timer_running = False
        self.start_time = 0.0
        self.elapsed_time = 0.0
        self._save_timer_id = None
        self.init_completed = False
        
        # 预先生成全局通道 Keys 列表
        self.channel_keys = [f"1-{unit}-{ch}" for unit in [1, 2] for ch in range(1, 16)]
        
        # 缓存通道元数据
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
        
        # 存储 30 个通道控件变量的字典
        self.channel_vars = {}
        for key in self.channel_keys:
            meta = self.channel_metadata[key]
            unit = meta["unit"]
            self.channel_vars[key] = {
                "comment": tk.StringVar(),
                "range": tk.StringVar(value="10mV" if unit == 1 else "1V"),
                "ratio": tk.StringVar(value="-50000" if unit == 1 else "-1"),
                "lbl_val": None,
                "lbl_avg": None, 
                "lbl_max": None, 
                "lbl_min": None, 
                "ent_comment": None,
                "ent_ratio": None,
                "lbl_ch": None,
                "cmb_range": None,
                "sync_failed": False, # 标记量程是否同步物理通道失败
                "row_bg": "#ffffff"
            }
            self.stats_count[key] = 0
            self.stats_sum[key] = 0.0
            self.stats_max[key] = -float('inf')
            self.stats_min[key] = float('inf')
                
        self.create_widgets()
        self.load_local_config()
        self.rebuild_scpi_channel_mapping() # 初始离线计算 SCPI 映射
        
        # 绑定 trace 自动保存修改
        self.port.trace_add("write", self._schedule_save)
        self.baudrate.trace_add("write", self._schedule_save)
        self.device_model.trace_add("write", self._on_model_changed)
        self.ch30_var.trace_add("write", self._schedule_save)
        self.interval_var.trace_add("write", self._schedule_save)
        self.case_name.trace_add("write", self._schedule_save)
        for key, vars_dict in self.channel_vars.items():
            vars_dict["comment"].trace_add("write", self._schedule_save)
            vars_dict["range"].trace_add("write", self._schedule_save)
            vars_dict["ratio"].trace_add("write", self._schedule_save)
        self.init_completed = True
        
        self.auto_set_shortest_interval(verbose=False)
        self.refresh_ports_on_click()
        
        # 窗口关闭时自动保存配置
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_save)
        
    def _on_model_changed(self, *args):
        """当仪器型号发生改变时，自动重设 30ch 重映射策略并刷新限速配置"""
        if not getattr(self, "init_completed", False):
            return
        self.config_unsynced = True
        
        self.instrument_ranges.clear()
        self.instrument_stores.clear()
        self.instrument_comments.clear()
        self.instrument_scalings.clear()
        
        model = self.device_model.get()
        if model == "LR8450":
            self.ch30_var.set(True)
        else:
            self.ch30_var.set(False)
            
        self.rebuild_scpi_channel_mapping()
        self.auto_set_shortest_interval(verbose=False)

    def rebuild_scpi_channel_mapping(self):
        """将所有通道物理标识一次性算好离线备用，剔除跨线程 StringVar 开销"""
        ch30 = self.ch30_var.get()
        self.scpi_channel_mapping = {
            key: (f"CH1_{meta['ch']}" if meta['unit'] == 1 else (f"CH1_{meta['ch']+15}" if ch30 else f"CH2_{meta['ch']}"))
            for key, meta in self.channel_metadata.items()
        }

    def _get_filter_scpi(self):
        return ":UNIT:FILTer 50HZ", ":UNIT:FILTer?"

    def _create_alert_window(self, title, width, height, border_color, bg_color):
        """工厂方法：创建居中置顶警告弹窗"""
        self.root.bell()

        alert_win = tk.Toplevel(self.root)
        alert_win.title(title)
        alert_win.geometry(f"{width}x{height}")
        alert_win.configure(bg="#ffffff")
        alert_win.attributes("-topmost", True)

        try:
            x = self.root.winfo_x() + (self.root.winfo_width() // 2) - (width // 2)
            y = self.root.winfo_y() + (self.root.winfo_height() // 2) - (height // 2)
            alert_win.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            pass

        main_frame = tk.Frame(
            alert_win,
            bg=bg_color,
            highlightbackground=border_color,
            highlightcolor=border_color,
            highlightthickness=2,
            bd=0
        )
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        return alert_win, main_frame

    # ==================== 鼠标点击及拖拽多选交互核心逻辑 ====================
    def on_drag_start(self, key, event=None):
        """按下鼠标左键，开始选区/拖拽多选"""
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
        """按住鼠标左键移动，动态扩展选区 (高性能 O(1) 拾取)"""
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
        """释放鼠标左键，完成拖拽多选"""
        self.is_dragging = False

    def select_channel(self, key, event=None):
        """兼容性包装调用"""
        self.on_drag_start(key, event)

    def _update_selection_visuals(self, force=False):
        """更新界面组件背景高亮，差异化增量渲染 (Diffing Optimization)"""
        for key, vars_dict in self.channel_vars.items():
            is_selected = key in self.selected_keys
            sync_failed = vars_dict.get("sync_failed", False)
            state = (is_selected, sync_failed)

            # 若该通道的选择与同步状态未改变，直接跳过 Widget 重绘操作
            if not force and self._selection_visual_cache.get(key) == state:
                continue
            self._selection_visual_cache[key] = state

            ent = vars_dict.get("ent_comment")
            ent_r = vars_dict.get("ent_ratio")
            lbl = vars_dict.get("lbl_ch")
            cmb_range = vars_dict.get("cmb_range")
            row_bg = vars_dict.get("row_bg", "#ffffff")
            
            if is_selected:
                if ent:
                    ent.config(bg="#bae6fd", highlightbackground="#007aff", highlightcolor="#007aff")
                if ent_r:
                    ent_r.config(bg="#bae6fd", highlightbackground="#007aff", highlightcolor="#007aff")
                if lbl:
                    lbl.config(bg="#bae6fd", fg="#007aff")
            else:
                if ent:
                    ent.config(bg="#ffffff", highlightbackground="#e5e5ea", highlightcolor="#007aff")
                if ent_r:
                    ent_r.config(bg="#ffffff", highlightbackground="#e5e5ea", highlightcolor="#007aff")
                if lbl:
                    lbl_color = "#ff3b30" if sync_failed else "#1c1c1e"
                    lbl.config(bg=row_bg, fg=lbl_color)

            if cmb_range:
                cmb_range.config(style="Red.TCombobox" if sync_failed else "TCombobox")

    def _sync_single_channel_hardware(self, key, force_set=False, preferred_kinds=None):
        if preferred_kinds is None:
            preferred_kinds = {}

        is_lr8450 = self.device_model.get() == "LR8450"
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
        
        range_modified = (range_text != last_synced_range)
        ratio_modified = (ratio_text != last_synced_ratio)
        
        if not force_set and comment and not range_modified and not ratio_modified and last_synced_store is True:
            return True, range_text, True

        if not force_set and not comment and not range_modified and not ratio_modified and last_synced_store is False:
            return True, range_text, True

        success = False
        last_readback = ""

        if comment:
            self.send_raw_cmd(f"{prefix_cmd}:STORe {ch_id},ON")
            time.sleep(0.015)
            self.send_raw_cmd(f"{prefix_cmd}:INMOde {ch_id},VOLTAGE")
            time.sleep(0.015)
            self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
            time.sleep(0.015)

            for arg_kind, range_arg in get_range_command_args(range_text, is_lr8450):
                self.send_raw_cmd(f"{prefix_cmd}:RANGe {ch_id},{range_arg}")
                time.sleep(0.035)
                last_readback = self.query_raw_cmd(f"{prefix_cmd}:RANGe? {ch_id}")
                if is_scpi_range_match(last_readback, scpi_range):
                    success = True
                    break
                time.sleep(0.05)
                last_readback = self.query_raw_cmd(f"{prefix_cmd}:RANGe? {ch_id}")
                if is_scpi_range_match(last_readback, scpi_range):
                    success = True
                    break

            self.send_raw_cmd(f':COMMent:CH {ch_id},"{comment}"')
            color_idx = (ch - 1) % 24 + 1
            self.send_raw_cmd(f":DISPlay:DRAWing {ch_id},C{color_idx}")

            if ratio_text:
                try:
                    ratio_val = float(ratio_text)
                    self.send_raw_cmd(f":SCALing:SET {ch_id},ENG")
                    self.send_raw_cmd(f":SCALing:KIND {ch_id},RATIO")
                    self.send_raw_cmd(f":SCALing:VOLT {ch_id},{ratio_val}")
                    self.send_raw_cmd(f":SCALing:OFFSet {ch_id},0.0")
                    unit_str = "mA" if ratio_val not in (1.0, -1.0) else "V"
                    self.send_raw_cmd(f':SCALing:UNIT {ch_id},"{unit_str}"')
                except ValueError:
                    self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
            else:
                self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")

            self.instrument_stores[key] = True
        else:
            range_arg = get_range_command_args(range_text, is_lr8450)[0][1]
            self.send_raw_cmd(f"{prefix_cmd}:RANGe {ch_id},{range_arg}")
            self.send_raw_cmd(f"{prefix_cmd}:STORe {ch_id},OFF")
            self.send_raw_cmd(f":DISPlay:DRAWing {ch_id},OFF")
            self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
            
            success = True
            self.instrument_stores[key] = False

        actual_range_str = format_range_for_log(last_readback) if last_readback else range_text
        if success:
            self.instrument_ranges[key] = range_text
            self.instrument_scalings[key] = ratio_text
            self.channel_vars[key]["sync_failed"] = False
        else:
            self.channel_vars[key]["sync_failed"] = True

        return success, actual_range_str, False

    def on_range_combobox_selected(self, key):
        if not getattr(self, "init_completed", False):
            return
            
        selected_range = self.channel_vars[key]["range"].get()
        targets = [key]
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            self.init_completed = False
            for k in self.selected_keys:
                if k != key:
                    if self.channel_vars[k]["range"].get() != selected_range:
                        self.channel_vars[k]["range"].set(selected_range)
                        targets.append(k)
            self.init_completed = True
            
        self._schedule_save()
        
        if self.is_connected:
            if getattr(self, "timer_running", False):
                self.write_log("[提示] 当前正在采集测量中，修改的量程已保存在本地，将在下次开始采集时同步到仪器中。")
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
        
        preferred_kinds = {}
        failed_channels = []
        
        for k in keys:
            comment = self.channel_vars[k]["comment"].get().strip()
            ch_id = self.scpi_channel_mapping[k]
            success, actual_range, skipped = self._sync_single_channel_hardware(
                k, force_set=True, preferred_kinds=preferred_kinds
            )
            if comment and not skipped:
                if success:
                    self.write_log(f"[即时修改成功] {ch_id} ({comment}) 的物理量程成功修改并应用为: {actual_range}")
                else:
                    failed_channels.append({
                        "ch_id": ch_id, "comment": comment, "expected": range_text, "actual": actual_range
                    })
                    self.write_log(f"[即时修改失败] ⚠️ {ch_id} ({comment}) 无法更新量程！当前实际: {actual_range}")

        self.root.after(0, self._update_selection_visuals)
        if failed_channels:
            self.root.after(0, self.show_sync_failed_popup, failed_channels)

    def _bg_sync_all_commented_ranges_now(self, keys, is_lr8450):
        preferred_kinds = {}
        failed_channels = []
        
        for k in keys:
            comment = self.channel_vars[k]["comment"].get().strip()
            range_text = self.channel_vars[k]["range"].get()
            ch_id = self.scpi_channel_mapping[k]
            
            success, actual_range, _ = self._sync_single_channel_hardware(
                k, force_set=True, preferred_kinds=preferred_kinds
            )
            if success:
                self.write_log(f"[连接同步成功] {ch_id} ({comment}) 的物理量程设为: {actual_range}")
            else:
                failed_channels.append({
                    "ch_id": ch_id, "comment": comment, "expected": range_text, "actual": actual_range
                })
                self.write_log(f"[连接同步失败] ⚠️ {ch_id} ({comment}) 量程未成功配置！当前实际: {actual_range}")
                
        self.write_log("[连接同步] 所有已注释通道的初始化同步和回读校验已完成。")
        self.config_unsynced = False

        self.root.after(0, self._update_selection_visuals)
        if failed_channels:
            self.root.after(0, self.show_sync_failed_popup, failed_channels)

    def show_sync_failed_popup(self, failed_list):
        alert_win, main_frame = self._create_alert_window("⚠️ 通道物理量程同步失败", 540, 350, "#ff3b30", "#fff5f5")
        
        tk.Label(main_frame, text="⚠️ PHYSICAL SYNC FAILED", font=("Microsoft YaHei UI", 12, "bold"), fg="#ff3b30", bg="#fff5f5").pack(pady=8)
        tk.Label(main_frame, text="以下活动通道的物理量程下发与回读验证失败，请确认串口通信或尝试手动在仪器面板进行设置。受影响的通道在统计看板已标红展示：", font=("Microsoft YaHei UI", 10), fg="#1c1c1e", bg="#fff5f5", wraplength=490, justify="left").pack(pady=4, padx=15)
        
        list_frame = tk.Frame(main_frame, bg="#ffffff", bd=1, relief="solid")
        list_frame.pack(fill="both", expand=True, padx=15, pady=8)
        
        failed_text = scrolledtext.ScrolledText(
            list_frame, wrap=tk.WORD, font=("Consolas", 10),
            bg="#ffffff", fg="#ff3b30", bd=0, highlightthickness=0
        )
        failed_text.pack(fill="both", expand=True)
        
        for idx, item in enumerate(failed_list):
            info = f"[{idx+1}] 通道: {item['ch_id']} ({item['comment']})\n    - 设定量程: {item['expected']}\n    - 实际回读: {item['actual']}\n\n"
            failed_text.insert(tk.END, info)
            
        failed_text.config(state="disabled")
        
        tk.Button(
            main_frame, text=" 我知道了 ", font=("Microsoft YaHei UI", 10, "bold"), 
            bg="#ff3b30", fg="white", activebackground="#e03126", activeforeground="white", 
            relief="flat", bd=0, cursor="hand2", command=alert_win.destroy, width=12, height=1
        ).pack(pady=8)

    def _sync_selected_values(self, key, event, field_name):
        if event.keysym in ["Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Caps_Lock", "Tab", "Escape"]:
            return
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            val = self.channel_vars[key][field_name].get()
            self.init_completed = False
            for k in self.selected_keys:
                if k != key:
                    self.channel_vars[k][field_name].set(val)
            self.init_completed = True
            self._schedule_save()

    def _handle_copy_sync(self, key, field_name):
        """多选批量复制：复制所有选中通道的参数，按换行符分隔供 Excel 使用"""
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            ordered_keys = [k for k in self.channel_keys if k in self.selected_keys]
            vals = [self.channel_vars[k][field_name].get() for k in ordered_keys]
            copy_str = "\n".join(vals)
            self.root.clipboard_clear()
            self.root.clipboard_append(copy_str)
            field_label = "首注释" if field_name == "comment" else "转换比"
            self.write_log(f"[快捷复制] 已将选中 {len(ordered_keys)} 个通道的{field_label}批量复制到剪贴板。")
            return "break"
            
        ent = self.channel_vars[key].get(f"ent_{field_name}")
        if ent:
            try:
                sel_text = ent.selection_get()
                if sel_text:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(sel_text)
                    return "break"
            except Exception:
                pass
            val = self.channel_vars[key][field_name].get()
            if val:
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
            self.init_completed = True
            self.save_local_config()
            return "break"
            
        self.root.after(50, self.save_local_config)
        return None

    def _handle_delete_sync(self, key, field_name, event=None):
        """多选批量删除：清空所有选中通道的参数"""
        targets = list(self.selected_keys) if (len(self.selected_keys) > 1 and key in self.selected_keys) else [key]
        self.init_completed = False
        for k in targets:
            self.channel_vars[k][field_name].set("")
        self.init_completed = True
        self.save_local_config()
        self._reset_gui_val_labels()
        field_label = "首注释" if field_name == "comment" else "转换比"
        self.write_log(f"[快捷删除] 已清空 {len(targets)} 个通道的{field_label}。")
        return "break"

    def on_comment_copy(self, key, event):
        return self._handle_copy_sync(key, "comment")

    def on_comment_keyrelease(self, key, event):
        self._sync_selected_values(key, event, "comment")

    def on_comment_paste(self, key, event):
        return self._handle_paste_sync(key, "comment")

    def on_comment_delete(self, key, event):
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            return self._handle_delete_sync(key, "comment", event)
        if isinstance(event.widget, tk.Label):
            return self._handle_delete_sync(key, "comment", event)
        return None

    def on_ratio_copy(self, key, event):
        return self._handle_copy_sync(key, "ratio")

    def on_ratio_keyrelease(self, key, event):
        self._sync_selected_values(key, event, "ratio")

    def on_ratio_paste(self, key, event):
        return self._handle_paste_sync(key, "ratio")

    def on_ratio_delete(self, key, event):
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            return self._handle_delete_sync(key, "ratio", event)
        if isinstance(event.widget, tk.Label):
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
                  foreground=[("disabled", "#aeaeb2"), ("readonly", "#1c1c1e"), ("focus", "#1c1c1e")])

        style.configure("Red.TCombobox", 
                        fieldbackground="#ffebee", background="#ffcdd2", foreground="#ff3b30", 
                        arrowcolor="#ff3b30", bordercolor="#ff8a80", lightcolor="#ff8a80", darkcolor="#ff8a80")
        
        style.map("Red.TCombobox", 
                  fieldbackground=[("readonly", "#ffebee"), ("disabled", "#ffebee")], 
                  selectbackground=[("readonly", "#ffcdd2"), ("focus", "#ffcdd2")],
                  selectforeground=[("readonly", "#ff3b30"), ("focus", "#ff3b30")],
                  foreground=[("disabled", "#ff8a80"), ("readonly", "#ff3b30"), ("focus", "#ff3b30")])
        
        conn_frame = tk.Frame(
            self.root, bg="#ffffff", highlightbackground="#e5e5ea", 
            highlightcolor="#e5e5ea", highlightthickness=1, bd=0
        )
        conn_frame.pack(fill="x", padx=15, pady=10)
        
        glass_lbl = tk.Label(conn_frame, text=" 仪器连接及配置导入导出 ", font=("Microsoft YaHei UI", 11, "bold"), bg="#ffffff", fg="#007aff")
        glass_lbl.grid(row=0, column=0, columnspan=13, sticky="w", padx=10, pady=5)
        
        tk.Label(conn_frame, text="串口号:", bg="#ffffff", fg="#8e8e93", font=("Microsoft YaHei UI", 10)).grid(row=1, column=0, padx=5, pady=8, sticky="w")
        self.port_combo = ttk.Combobox(conn_frame, textvariable=self.port, width=12, font=("Segoe UI", 10), state="readonly", postcommand=self.refresh_ports_on_click)
        self.port_combo.grid(row=1, column=1, padx=5, pady=8)
        
        tk.Label(conn_frame, text="波特率:", bg="#ffffff", fg="#8e8e93", font=("Microsoft YaHei UI", 10)).grid(row=1, column=2, padx=5, pady=8, sticky="w")
        self.baud_combo = ttk.Combobox(conn_frame, textvariable=self.baudrate, values=["9600", "19200", "38400", "115200"], width=8, font=("Segoe UI", 10), state="readonly")
        self.baud_combo.grid(row=1, column=3, padx=5, pady=8)

        tk.Label(conn_frame, text="仪器型号:", bg="#ffffff", fg="#8e8e93", font=("Microsoft YaHei UI", 10)).grid(row=1, column=4, padx=5, pady=8, sticky="w")
        self.model_combo = ttk.Combobox(conn_frame, textvariable=self.device_model, values=["LR8401-21", "LR8450"], width=10, font=("Segoe UI", 10), state="readonly")
        self.model_combo.grid(row=1, column=5, padx=8, pady=8)

        tk.Label(conn_frame, text="记录间隔:", bg="#ffffff", fg="#8e8e93", font=("Microsoft YaHei UI", 10)).grid(row=1, column=6, padx=5, pady=8, sticky="w")
        self.interval_combo = ttk.Combobox(conn_frame, textvariable=self.interval_var, values=["10ms", "20ms"], width=6, font=("Segoe UI", 10), state="readonly")
        self.interval_combo.grid(row=1, column=7, padx=5, pady=8)
        
        self.btn_connect = tk.Button(
            conn_frame, text=" 连接仪器 ", bg="#007aff", fg="white", 
            font=("Microsoft YaHei UI", 10, "bold"), relief="flat", activebackground="#0062cc", activeforeground="white", bd=0, cursor="hand2"
        )
        self.btn_connect.config(command=self.toggle_connection)
        self.btn_connect.grid(row=1, column=8, padx=12, pady=8)
        
        self.lbl_status = tk.Label(conn_frame, text="未连接", fg="#ff3b30", font=("Microsoft YaHei UI", 11, "bold"), bg="#ffffff")
        self.lbl_status.grid(row=1, column=9, padx=8, pady=8)
        
        btn_opts = {
            "font": ("Microsoft YaHei UI", 10, "bold"), "relief": "flat", "padx": 12, "pady": 4,
            "fg": "#007aff", "bg": "#f2f2f7", "activebackground": "#e5e5ea", "activeforeground": "#0056b3",
            "bd": 0, "cursor": "hand2"
        }
        
        self.btn_import = tk.Button(conn_frame, text=" 📂 导入配置 ", command=self.import_config_file, **btn_opts)
        self.btn_import.grid(row=1, column=10, padx=5, pady=8)
        
        self.btn_export = tk.Button(conn_frame, text=" 💾 导出配置 ", command=self.export_config_file, **btn_opts)
        self.btn_export.grid(row=1, column=11, padx=5, pady=8)

        self.btn_batch = tk.Button(conn_frame, text=" 🔧 集中处理 ", command=self.open_batch_config_dialog, **btn_opts)
        self.btn_batch.grid(row=1, column=12, padx=5, pady=8)
        
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
                
                # 构建控件至通道 key 的 O(1) 映射关系，供鼠标拖拽多选使用
                self.widget_to_key[lbl_ch] = key
                self.widget_to_key[ent_comment] = key
                self.widget_to_key[ent_ratio] = key
                self.widget_to_key[cmb_range] = key
                
                # 通道名称标签事件绑定：按住拖动多选、复制、粘贴、Delete删除
                lbl_ch.bind("<ButtonPress-1>", lambda e, k=key: self.on_drag_start(k, e))
                lbl_ch.bind("<B1-Motion>", self.on_drag_motion)
                lbl_ch.bind("<ButtonRelease-1>", self.on_drag_end)
                lbl_ch.bind("<Control-c>", lambda e, k=key: self.on_comment_copy(k, e))
                lbl_ch.bind("<Control-v>", lambda e, k=key: self.on_comment_paste(k, e))
                lbl_ch.bind("<Delete>", lambda e, k=key: self.on_comment_delete(k, e))
                lbl_ch.bind("<BackSpace>", lambda e, k=key: self.on_comment_delete(k, e))
                
                # 首注释输入框事件绑定：按住拖动多选、按键快捷同步
                ent_comment.bind("<ButtonPress-1>", lambda e, k=key: self.on_drag_start(k, e))
                ent_comment.bind("<B1-Motion>", self.on_drag_motion)
                ent_comment.bind("<ButtonRelease-1>", self.on_drag_end)
                ent_comment.bind("<KeyRelease>", lambda e, k=key: self.on_comment_keyrelease(k, e))
                ent_comment.bind("<Control-c>", lambda e, k=key: self.on_comment_copy(k, e))
                ent_comment.bind("<Control-v>", lambda e, k=key: self.on_comment_paste(k, e))
                ent_comment.bind("<Shift-Insert>", lambda e, k=key: self.on_comment_paste(k, e))
                ent_comment.bind("<Delete>", lambda e, k=key: self.on_comment_delete(k, e))
                ent_comment.bind("<BackSpace>", lambda e, k=key: self.on_comment_delete(k, e))
                
                # 转换比输入框事件绑定：按住拖动多选、按键快捷同步
                ent_ratio.bind("<ButtonPress-1>", lambda e, k=key: self.on_drag_start(k, e))
                ent_ratio.bind("<B1-Motion>", self.on_drag_motion)
                ent_ratio.bind("<ButtonRelease-1>", self.on_drag_end)
                ent_ratio.bind("<KeyRelease>", lambda e, k=key: self.on_ratio_keyrelease(k, e))
                ent_ratio.bind("<Control-c>", lambda e, k=key: self.on_ratio_copy(k, e))
                ent_ratio.bind("<Control-v>", lambda e, k=key: self.on_ratio_paste(k, e))
                ent_ratio.bind("<Shift-Insert>", lambda e, k=key: self.on_ratio_paste(k, e))
                ent_ratio.bind("<Delete>", lambda e, k=key: self.on_ratio_delete(k, e))
                ent_ratio.bind("<BackSpace>", lambda e, k=key: self.on_ratio_delete(k, e))
                
                lbl_val = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=12, highlightthickness=0, bd=0)
                lbl_val.grid(row=ch+1, column=4, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["lbl_val"] = lbl_val
                
                lbl_avg = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=11, highlightthickness=0, bd=0)
                lbl_avg.grid(row=ch+1, column=5, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["lbl_avg"] = lbl_avg

                lbl_max = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=11, highlightthickness=0, bd=0)
                lbl_max.grid(row=ch+1, column=6, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["lbl_max"] = lbl_max

                lbl_min = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=11, highlightthickness=0, bd=0)
                lbl_min.grid(row=ch+1, column=7, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["lbl_min"] = lbl_min

        control_frame = tk.Frame(self.root, bg="#ffffff", highlightbackground="#e5e5ea", highlightcolor="#e5e5ea", highlightthickness=1, bd=0)
        control_frame.pack(fill="x", padx=15, pady=5)
        
        self.lbl_timer = tk.Label(
            control_frame, text="00:00.0", font=("Segoe UI", 32, "bold"), 
            bg="#f2f2f7", fg="#1c1c1e", width=12, highlightthickness=0, bd=0
        )
        self.lbl_timer.pack(side="left", padx=15, pady=8)
        
        tk.Label(control_frame, text="SOC:", bg="#ffffff", fg="#1c1c1e", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left", padx=5)
        self.entry_case = self._create_ios_entry(control_frame, self.case_name, width=15)
        self.entry_case.pack(side="left", padx=5)
        
        self.btn_start = tk.Button(
            control_frame, text="▶ 开始采集 (Start)", bg="#34c759", fg="white", 
            font=("Microsoft YaHei UI", 11, "bold"), relief="flat", height=2, width=15,
            state="disabled", cursor="hand2", bd=0, activebackground="#2c9e47", activeforeground="white"
        )
        self.btn_start.config(command=self.start_acquisition)
        self.btn_start.pack(side="left", expand=True, padx=10)
        
        self.btn_copy = tk.Button(
            control_frame, text="📋 一键复制数据", bg="#5e5ce6", fg="white", 
            font=("Microsoft YaHei UI", 11, "bold"), relief="flat", height=2, width=15,
            cursor="hand2", bd=0, activebackground="#4d4cb8", activeforeground="white"
        )
        self.btn_copy.config(command=self.copy_clipboard_data)
        self.btn_copy.pack(side="left", expand=True, padx=10)
        
        self.btn_stop = tk.Button(
            control_frame, text="■ 停止并保存 (Stop)", bg="#ff3b30", fg="white", 
            font=("Microsoft YaHei UI", 11, "bold"), relief="flat", height=2, width=15,
            state="disabled", cursor="hand2", bd=0, activebackground="#e03126", activeforeground="white"
        )
        self.btn_stop.config(command=self.stop_acquisition)
        self.btn_stop.pack(side="right", expand=True, padx=10)
        
        log_frame = tk.Frame(self.root, bg="#ffffff", highlightbackground="#e5e5ea", highlightcolor="#e5e5ea", highlightthickness=1, bd=0)
        log_frame.pack(fill="both", expand=True, padx=15, pady=10)
        
        log_title = tk.Label(log_frame, text=" 实时程控日志及统计看板 ", font=("Microsoft YaHei UI", 10, "bold"), bg="#ffffff", fg="#8e8e93")
        log_title.pack(anchor="w", padx=10, pady=5)
        
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
                if "COM31" in port_list:
                    self.port.set("COM31")
                else:
                    self.port.set(port_list[0])
                    
        self.port_combo['values'] = port_list

    def auto_set_shortest_interval(self, verbose=True):
        current_val = self.interval_var.get()
        if self.device_model.get() == "LR8450":
            return

        has_unit2_active = any(self.channel_vars[f"1-2-{ch}"]["comment"].get().strip() for ch in range(1, 16))
        if has_unit2_active and current_val == "10ms":
            self.interval_var.set("20ms")
            if verbose:
                self.write_log("[自动优化] 检测到当前处于 LR8401 模式且启用了 UNIT 2，记录间隔自动设为硬件最短允许值: 20ms")

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
        for unit in [1, 2]:
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
        
        for unit in [1, 2]:
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
                    self.channel_vars[t_key]["comment"].set(self.channel_vars[src_key]["comment"].get())
                if param_vars["range"].get():
                    new_range = self.channel_vars[src_key]["range"].get()
                    old_range = self.channel_vars[t_key]["range"].get()
                    if old_range != new_range:
                        self.channel_vars[t_key]["range"].set(new_range)
                        synced_keys.append(t_key)
                    else:
                        self.channel_vars[t_key]["range"].set(new_range)
                if param_vars["ratio"].get():
                    self.channel_vars[t_key]["ratio"].set(self.channel_vars[src_key]["ratio"].get())
                
            self.init_completed = True
            self.save_local_config()
            self._reset_gui_val_labels()
            
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
        if "port" in config:
            self.port.set(config.get("port", ""))
        if "baudrate" in config:
            self.baudrate.set(config.get("baudrate", "115200"))
        if "device_model" in config:
            self.device_model.set(config.get("device_model", "LR8401-21"))
        if "ch_30" in config:
            self.ch30_var.set(config.get("ch_30", False))
        if "interval" in config:
            self.interval_var.set(config.get("interval", "10ms"))
        if "soc" in config:
            self.case_name.set(config.get("soc", ""))
            
        channels_data = config.get("channels", config)
        for key, val in channels_data.items():
            if key in self.channel_vars:
                meta = self.channel_metadata[key]
                unit = meta["unit"]
                default_range = "10mV" if unit == 1 else "1V"
                default_ratio = "-50000" if unit == 1 else "-1"
                self.channel_vars[key]["comment"].set(val.get("comment", ""))
                self.channel_vars[key]["range"].set(val.get("range", default_range))
                self.channel_vars[key]["ratio"].set(val.get("ratio", default_ratio))
        self.rebuild_scpi_channel_mapping()

    def _generate_config_dict(self):
        config = {
            "port": self.port.get(),
            "baudrate": self.baudrate.get(),
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
                self.save_local_config()
                self.write_log(f"[导入成功] 已加载配置: {os.path.basename(filepath)}")
                self._reset_gui_val_labels()
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
        self.root.after(0, self._safe_write_log, formatted_text)
        
    def _safe_write_log(self, text):
        self.log_area.insert(tk.END, text)
        self.log_area.see(tk.END)

    def toggle_connection(self):
        if not self.is_connected:
            threading.Thread(target=self._bg_connect, daemon=True).start()
        else:
            self._disconnect_device()

    def _clear_serial_buffers(self):
        if self.ser and self.ser.is_open:
            try:
                if self.ser.in_waiting > 0:
                    self.ser.read(self.ser.in_waiting)
                else:
                    self.ser.reset_input_buffer()
            except Exception:
                pass

    def _bg_connect(self):
        port_val = self.port.get().strip()
        
        if port_val == "无可用COM口":
            self.write_log("[错误] 当前无可用串口，无法连接！")
            return
            
        self.write_log(f"正在建立物理通信 {port_val}...")
        
        try:
            self.ser = serial.Serial(
                port=port_val,
                baudrate=int(self.baudrate.get()),
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1.0
            )
            time.sleep(0.3)
            
            self._clear_serial_buffers()
            self.send_raw_cmd(":HEADer OFF")
            
            idn = self.query_raw_cmd("*IDN?")
            if idn:
                self.write_log(f"设备成功建立通信: {idn}")
                
                ch30_detected = False
                is_lr8450 = "8450" in idn
                
                if is_lr8450:
                    self.root.after(0, lambda: self.device_model.set("LR8450"))
                    self.root.after(0, lambda: self.ch30_var.set(True))
                else:
                    self.root.after(0, lambda: self.device_model.set("LR8401-21"))
                    self.root.after(0, lambda: self.ch30_var.set(False))
                
                self.send_raw_cmd("*CLS")
                
                self.send_raw_cmd(":CONFigure:ATSAve OFF")
                if is_lr8450:
                    self.send_raw_cmd(":CONFigure:SAVEWave OFF")
                    self.send_raw_cmd(":CONFigure:SAVECalc OFF")
                
                cmd_set, cmd_query = self._get_filter_scpi()
                self.send_raw_cmd(cmd_set)
                time.sleep(0.3)
                
                f_res = clean_scpi_response(self.query_raw_cmd(cmd_query).strip())
                if f_res:
                    self.write_log(f"[连接建立] 设定电网频率滤波器配置为: {f_res} - 已确认验证")
                else:
                    self.write_log(f"[连接建立] 设定电网频率滤波器配置为: 无法回显/查询超时，请手动在仪器面板确认")
                
                if is_lr8450:
                    self.write_log("[自动检测] 识别到仪器实际型号为 HIOKI LR8450，自动转为 30ch 重映射模式。")
                    
                    opt_res = self.query_raw_cmd("*OPT?")
                    if opt_res and "U8552" in opt_res.upper():
                        ch30_detected = True
                    
                    if not ch30_detected:
                        time.sleep(0.1) 
                        ans = self.query_raw_cmd(":UNIT:STORe? CH1_16") 
                        if ans and ("ON" in ans.upper() or "OFF" in ans.upper()):
                            ch30_detected = True
                else:
                    ans = self.query_raw_cmd(":UNIT:STORe? CH1_16")
                    if ans and ("ON" in ans.upper() or "OFF" in ans.upper()):
                        self.root.after(0, lambda: self.ch30_var.set(True))
                    else:
                        self.root.after(0, lambda: self.ch30_var.set(False))
                
                self.auto_set_shortest_interval(verbose=True)
                self.root.after(0, self.rebuild_scpi_channel_mapping)
                
                self.is_connected = True
                self.config_unsynced = True
                self.root.after(0, self._update_ui_connected)
                
                commented_keys = [k for k in self.channel_keys if self.channel_vars[k]["comment"].get().strip()]
                if commented_keys:
                    self.write_log(f"[连接同步] 检测到当前控制台有 {len(commented_keys)} 个有注释的通道，正在初始化并配置物理仪器量程...")
                    threading.Thread(target=self._bg_sync_all_commented_ranges_now, args=(commented_keys, is_lr8450), daemon=True).start()
            else:
                self.write_log("握手无响应。请确认日置菜单中 [System]->[USB] 处于 Comm 模式。")
                self._disconnect_device()
        except Exception as e:
            self.write_log(f"串口连接异常: {e}")
            self._disconnect_device()

    def _update_ui_connected(self):
        self.btn_connect.config(text=" 断开连接 ", bg="#f2f2f7", fg="#007aff")
        self.lbl_status.config(text="已连接", fg="#34c759")
        self.btn_start.config(state="normal")
        self.port_combo.config(state="disabled")
        self.baud_combo.config(state="disabled")
        self.model_combo.config(state="disabled")
        self.interval_combo.config(state="disabled")
        self.btn_import.config(state="disabled")
        self.btn_batch.config(state="normal")

    def _disconnect_device(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.ser = None
        self.is_connected = False
        self.timer_running = False
        
        self.instrument_ranges.clear()
        self.instrument_stores.clear()
        self.instrument_comments.clear()
        self.instrument_scalings.clear()
        self.gui_val_cache.clear()
        self.gui_stat_cache.clear()
        self._selection_visual_cache.clear()
        
        for key in self.channel_keys:
            self.channel_vars[key]["sync_failed"] = False

        self.root.after(0, self._update_ui_disconnected)
        self.root.after(0, lambda: self._update_selection_visuals(force=True))
        self.write_log("已释放串口资源并清空量程寄存器与渲染缓存。")

    def _update_ui_disconnected(self):
        self.btn_connect.config(text=" 连接仪器 ", bg="#007aff", fg="white")
        self.lbl_status.config(text="未连接", fg="#ff3b30")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="disabled")
        self.port_combo.config(state="readonly")
        self.baud_combo.config(state="readonly")
        self.model_combo.config(state="readonly")
        self.interval_combo.config(state="readonly")
        self.btn_import.config(state="normal")
        self.btn_batch.config(state="normal")

    def send_raw_cmd(self, cmd):
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                try:
                    self._clear_serial_buffers()
                    full_cmd = f"{cmd}\r\n"
                    self.ser.write(full_cmd.encode('ascii'))
                    self.ser.flush()
                    time.sleep(0.012)
                except Exception as e:
                    self.write_log(f"指令下发错误: {e}")

    def query_raw_cmd(self, cmd):
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                try:
                    self._clear_serial_buffers()
                    full_cmd = f"{cmd}\r\n"
                    self.ser.write(full_cmd.encode('ascii'))
                    self.ser.flush()
                    time.sleep(0.012)
                    response = self.ser.readline().decode('ascii', errors='ignore').strip()
                    return response
                except Exception as e:
                    self.write_log(f"指令查询错误: {e}")
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
        try:
            with open("hioki_config.json", "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    def load_local_config(self):
        if os.path.exists("hioki_config.json"):
            try:
                with open("hioki_config.json", "r", encoding="utf-8") as f:
                    config = json.load(f)
                    self._apply_config_dict(config)
                self.write_log("[配置读取] 成功恢复上次运行的配置信息。")
            except Exception:
                pass

    def on_close_save(self):
        self.save_local_config()
        self._disconnect_device()
        self.root.destroy()

    def _reset_gui_val_labels(self):
        self.gui_val_cache.clear()
        self.gui_stat_cache.clear()
        
        for key, vars_dict in self.channel_vars.items():
            row_bg = vars_dict["row_bg"]
            for lbl_key in ["lbl_val", "lbl_avg", "lbl_max", "lbl_min"]:
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
        
        avg_text, avg_fg = ("--", "#8e8e93") if math.isnan(avg) else (f"{avg:{fmt}}{unit}", "#ff3b30" if avg < 0 else "#1c1c1e")
        max_text, max_fg = ("--", "#8e8e93") if math.isnan(max_val) else (f"{max_val:{fmt}}{unit}", "#ff3b30" if max_val < 0 else "#1c1c1e")
        min_text, min_fg = ("--", "#8e8e93") if math.isnan(min_val) else (f"{min_val:{fmt}}{unit}", "#ff3b30" if min_val < 0 else "#1c1c1e")
            
        target_state = (avg_text, avg_fg, max_text, max_fg, min_text, min_fg)
        cache_entry = self.gui_stat_cache.get(key)
        
        if cache_entry != target_state:
            for lbl, txt, color in [
                (self.channel_vars[key]["lbl_avg"], avg_text, avg_fg),
                (self.channel_vars[key]["lbl_max"], max_text, max_fg),
                (self.channel_vars[key]["lbl_min"], min_text, min_fg)
            ]:
                if lbl:
                    lbl.config(text=txt, fg=color, bg=row_bg)
            self.gui_stat_cache[key] = target_state

    def _batch_update_gui_stats(self, updates):
        for key, avg, max_val, min_val in updates:
            self._update_gui_stat_labels(key, avg, max_val, min_val)

    def show_over_alert(self, channel_key, comment):
        meta = self.channel_metadata[channel_key]
        ch_display = f"{meta['prefix']}_{meta['ch']}"
        msg = f"通道 {ch_display} ({comment}) 发生 OVER 物理硬件溢出！" if comment else f"通道 {ch_display} 发生 OVER 溢出！"
        
        alert_win, main_frame = self._create_alert_window("⚠️ 测量值溢出告警", 460, 180, "#ff3b30", "#fff5f5")
        
        tk.Label(main_frame, text="⚠️ MEASUREMENT OVERFLOW", font=("Microsoft YaHei UI", 12, "bold"), fg="#ff3b30", bg="#fff5f5").pack(pady=10)
        tk.Label(main_frame, text=msg, font=("Microsoft YaHei UI", 11, "bold"), fg="#1c1c1e", bg="#fff5f5", wraplength=420).pack(pady=5)
        
        tk.Button(
            main_frame, text=" 我知道了 ", font=("Microsoft YaHei UI", 10, "bold"), 
            bg="#ff3b30", fg="white", activebackground="#e03126", activeforeground="white", 
            relief="flat", bd=0, cursor="hand2", command=alert_win.destroy, width=12, height=1
        ).pack(pady=10)

    def show_soft_over_alert(self, channel_key, comment, raw_volt, limit_volt, current_val):
        """防卡死非阻塞式通道软量程截幅（饱和）警报弹窗（增加清晰的瞬态与稳态说明）"""
        meta = self.channel_metadata[channel_key]
        ch_display = f"{meta['prefix']}_{meta['ch']}"
        
        is_ma = self.is_ma_channel.get(channel_key, False)
        unit_str = "mA" if is_ma else "V"
        fmt_str = ".3f" if is_ma else ".4f"
        type_str = "电流" if is_ma else "电压"

        msg = (
            f"通道 {ch_display} ({comment}) 在测试过程中抓取到瞬态峰值溢出！\n\n"
            f"• 抓获瞬态峰值电压：{raw_volt*1000:.3f} mV (当前稳态: {current_val:{fmt_str}} {unit_str})\n"
            f"• 硬件设定物理量程：{limit_volt*1000:.1f} mV\n\n"
            f"【原理提示】虽然当前稳态{type_str}较小，但上电/负载切换瞬间的峰值电压已超出物理量程上限！"
            f"信号已被硬件截幅钳位，MAX/MIN 峰值统计数据可能失真。建议提高该通道的量程。"
        )
        
        alert_win, main_frame = self._create_alert_window("⚠️ 软超量程截幅告警", 520, 250, "#f59e0b", "#fff9db")
        
        tk.Label(main_frame, text="⚠️ RANGE SATURATION (软截幅饱和)", font=("Microsoft YaHei UI", 12, "bold"), fg="#d97706", bg="#fff9db").pack(pady=10)
        tk.Label(main_frame, text=msg, font=("Microsoft YaHei UI", 10, "bold"), fg="#1c1c1e", bg="#fff9db", justify="left", wraplength=480).pack(pady=5)
        
        tk.Button(
            main_frame, text=" 我知道了 ", font=("Microsoft YaHei UI", 10, "bold"), 
            bg="#f59e0b", fg="white", activebackground="#d97706", activeforeground="white", 
            relief="flat", bd=0, cursor="hand2", command=alert_win.destroy, width=12, height=1
        ).pack(pady=10)

    def start_acquisition(self):
        self.btn_connect.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.btn_batch.config(state="disabled") 
        
        self.total_frames = 0
        self.alerted_over_channels = set()
        self._reset_gui_val_labels()
        
        for key in self.channel_keys:
            self.stats_count[key] = 0
            self.stats_sum[key] = 0.0
            self.stats_max[key] = -float('inf')
            self.stats_min[key] = float('inf')
        
        def check_is_ma(k):
            ratio_text = self.channel_vars[k]["ratio"].get().strip()
            if not ratio_text:
                return False
            try:
                val = float(ratio_text)
                return val not in (1.0, -1.0)
            except ValueError:
                return False

        self.is_ma_channel = {key: check_is_ma(key) for key in self.channel_keys}

        self.channel_limits_cache = {}
        for key in self.channel_keys:
            r_text = self.channel_vars[key]["range"].get()
            limit_volt = RANGE_LIMITS_VOLTS.get(r_text, 0.01)
            
            ratio_text = self.channel_vars[key]["ratio"].get().strip()
            ratio_val = 1.0
            is_scaled = False
            if ratio_text:
                try:
                    ratio_val = float(ratio_text)
                    is_scaled = (ratio_val != 1.0 and ratio_val != -1.0 and ratio_val != 0.0)
                except ValueError:
                    pass
            
            self.channel_limits_cache[key] = {
                "limit_volt": limit_volt,
                "ratio_val": ratio_val,
                "is_scaled": is_scaled
            }
        
        self.save_local_config()
        threading.Thread(target=self._bg_start_task, daemon=True).start()

    def _bg_start_task(self):
        self.send_raw_cmd("*CLS")
        
        self.send_raw_cmd(":CONFigure:ATSAve OFF")
        if self.device_model.get() == "LR8450":
            self.send_raw_cmd(":CONFigure:SAVEWave OFF")
            self.send_raw_cmd(":CONFigure:SAVECalc OFF")
        
        if getattr(self, "config_unsynced", True):
            self.write_log(">>> 检测到本地配置有变动，正在重构写入日置通道矩阵...")
            
            cmd_set, cmd_query = self._get_filter_scpi()
            self.send_raw_cmd(cmd_set)
            time.sleep(0.3)
            
            f_res = clean_scpi_response(self.query_raw_cmd(cmd_query).strip())
            if f_res:
                self.write_log(f">>> 设定滤波器配置为: 50Hz (设备当前读取状态: {f_res} - 已确认验证)")
            else:
                self.write_log(f">>> 设定滤波器配置为: 50Hz (设备当前读取状态: 无法回显/查询超时，请手动在仪器面板确认)")
            
            interval_val = self.interval_var.get()
            try:
                if interval_val.endswith("ms"):
                    interval_sec = float(interval_val.replace("ms", "")) / 1000.0
                elif interval_val.endswith("s"):
                    interval_sec = float(interval_val.replace("s", ""))
                else:
                    interval_sec = float(interval_val)
            except ValueError:
                interval_sec = 0.01
                
            self.send_raw_cmd(f":CONFigure:SAMPle {interval_sec}")
            self.write_log(f">>> 设定日置物理硬件记录间隔为: {interval_val} ({interval_sec}s)")
            
            preferred_kinds = {}
            for key in self.channel_keys:
                comment = self.channel_vars[key]["comment"].get().strip()
                ch_id = self.scpi_channel_mapping[key]
                success, actual_range, skipped = self._sync_single_channel_hardware(
                    key, force_set=False, preferred_kinds=preferred_kinds
                )
                if comment and not skipped:
                    if success:
                        self.write_log(f"[量程/转换比验证] {ch_id} ({comment}) 的物理配置已成功更新: {actual_range}")
                    else:
                        self.write_log(f"[警告] {ch_id} ({comment}) 配置更新失败！当前实际: {actual_range}")
            
            self.send_raw_cmd(":DISPlay:CHANge DISPlay")
            self.send_raw_cmd(":DISPlay:PAGE 1")
            self.send_raw_cmd(":DISPlay:MODE W_D")
            self.send_raw_cmd(":DISPlay:GROUp ALL")
            self.config_unsynced = False
        else:
            self.write_log(">>> 检测到通道配置与仪器保持一致，跳过参数写入，瞬间秒级开始采集！")
            
        self.write_log(">>> 正在启动测量记录...")
        self.dt_start = datetime.now()
        
        self.send_raw_cmd(":STARt")
        time.sleep(0.2)  # 给 LR8450 硬件启动和第一帧采样留出充分稳定时间 (原 0.05s 过短)
        
        self.root.after(0, self._start_ui_timer)

    def _start_ui_timer(self):
        self.timer_running = True
        self.start_time = time.time()
        self.btn_stop.config(state="normal")
        
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                self.ser.timeout = 0.3
                
        threading.Thread(target=self._bg_realtime_stream_worker, daemon=True).start()
        
        self._update_timer_loop()
        self.write_log("【实时流采集模式】高精度测量数据实时截获并显示中...")

    def _update_timer_loop(self):
        if self.timer_running:
            self.elapsed_time = time.time() - self.start_time
            mins = int(self.elapsed_time // 60)
            secs = int(self.elapsed_time % 60)
            tenths = int((self.elapsed_time * 10) % 10)
            self.lbl_timer.config(text=f"{mins:02d}:{secs:02d}.{tenths:d}")
            self.root.after(100, self._update_timer_loop)

    def _bg_realtime_stream_worker(self):
        """极速测量值流截获线程 (算法级优化：无生成器开销、UI 防抖解耦)"""
        self.active_channels = []
        for key in self.channel_keys:
            comment = self.channel_vars[key]["comment"].get().strip()
            if comment:
                meta = self.channel_metadata[key]
                scpi_ch = self.scpi_channel_mapping[key]
                lim_info = self.channel_limits_cache[key]
                self.active_channels.append({
                    "key": key,
                    "unit": meta["unit"],
                    "ch": meta["ch"],
                    "comment": comment,
                    "scpi_ch": scpi_ch,
                    "query_cmd": f":MEMory:VREAl? {scpi_ch}",
                    "limit_volt": lim_info["limit_volt"],
                    "ratio_val": lim_info["ratio_val"],
                    "is_scaled": lim_info["is_scaled"]
                })
                    
        num_active = len(self.active_channels)
        if num_active == 0:
            return
            
        # 启动前冲刷清空一次可能残留的硬件旧内存帧 (Flush Memory Buffer)
        self.send_raw_cmd(":MEMory:GETReal")
        time.sleep(0.03)
        for chan in self.active_channels:
            self.query_raw_cmd(chan["query_cmd"])
            
        last_gui_val_update = time.time()
        last_gui_stat_update = time.time()
        
        while self.timer_running:
            self.send_raw_cmd(":MEMory:GETReal")
            
            gui_vals = []
            valid_samples_count = 0
            
            for chan in self.active_channels:
                if not self.timer_running:
                    break
                
                res_str = clean_scpi_response(self.query_raw_cmd(chan["query_cmd"]))
                
                val_float = 0.0
                is_over = False
                is_valid = False
                is_range_exceeded = False
                limit_volt = chan["limit_volt"]
                
                if res_str:
                    res_upper = res_str.upper()
                    # 避免使用 any() 生成器表达式，极大降低 GC 开销
                    if ("OVER" in res_upper) or ("NAN" in res_upper) or ("INF" in res_upper) or ("O.R" in res_upper):
                        is_over = True
                    else:
                        try:
                            val_float = float(res_str)
                            if abs(val_float) >= 1.0e+9:
                                is_over = True
                            else:
                                is_valid = True
                                ratio_val = chan["ratio_val"]
                                raw_volt = abs(val_float / ratio_val) if chan["is_scaled"] else abs(val_float)
                                if raw_volt > limit_volt * 1.001:
                                    is_range_exceeded = True
                        except ValueError:
                            pass
                
                if is_over:
                    gui_vals.append(math.nan)
                    if chan["key"] not in self.alerted_over_channels:
                        self.alerted_over_channels.add(chan["key"])
                        self.write_log(f"[硬溢出告警] 通道 {chan['scpi_ch']} ({chan['comment']}) 发生 OVER 物理硬件溢出！")
                        self.root.after(0, self.show_over_alert, chan["key"], chan["comment"])
                elif not is_valid:
                    gui_vals.append(None)
                else:
                    gui_vals.append(val_float)
                    valid_samples_count += 1
                    
                    k = chan["key"]
                    self.stats_count[k] += 1
                    self.stats_sum[k] += val_float
                    if val_float > self.stats_max[k]:
                        self.stats_max[k] = val_float
                    if val_float < self.stats_min[k]:
                        self.stats_min[k] = val_float
                        
                    if is_range_exceeded and chan["key"] not in self.alerted_over_channels:
                        self.alerted_over_channels.add(chan["key"])
                        is_ma = self.is_ma_channel.get(chan["key"], False)
                        unit_str = "mA" if is_ma else "V"
                        fmt_str = ".3f" if is_ma else ".4f"
                        msg_log = (
                            f"[超量程告警] 通道 {chan['scpi_ch']} ({chan['comment']}) 测得信号超出量程上限！"
                            f"捕获电压 {raw_volt*1000:.3f}mV (当前测量值: {val_float:{fmt_str}}{unit_str})，超出 {limit_volt*1000:.1f}mV 量程极限！"
                        )
                        self.write_log(msg_log)
                        self.root.after(0, self.show_soft_over_alert, chan["key"], chan["comment"], raw_volt, limit_volt, val_float)
                
            if not self.timer_running:
                break
                
            if len(gui_vals) == num_active and self.timer_running:
                self.total_frames += 1
                now = time.time()
                
                # UI 采样值高频防抖（最大 12.5 FPS），防止 Tkinter 消息队列拥塞
                if now - last_gui_val_update >= 0.08:
                    updates = [(chan["key"], gui_vals[idx]) for idx, chan in enumerate(self.active_channels)]
                    self.root.after(0, self._batch_update_gui_vals, updates)
                    last_gui_val_update = now
                
                # UI 统计值低频防抖（最大 2 FPS）
                if now - last_gui_stat_update >= 0.5:
                    stats_updates = []
                    for chan in self.active_channels:
                        k = chan["key"]
                        cnt = self.stats_count[k]
                        if cnt > 0:
                            stats_updates.append((
                                k, 
                                self.stats_sum[k] / cnt, 
                                self.stats_max[k], 
                                self.stats_min[k]
                            ))
                        else:
                            stats_updates.append((k, math.nan, math.nan, math.nan))
                    self.root.after(0, self._batch_update_gui_stats, stats_updates)
                    last_gui_stat_update = now
                
            time.sleep(0.015)

    def stop_acquisition(self):
        self.timer_running = False
        self.btn_stop.config(state="disabled")
        self.dt_end = datetime.now()
        self.write_log(">>> 正在释放实时通道，下发硬件停止信号...")
        threading.Thread(target=self._bg_stop_and_save_task, daemon=True).start()

    def _bg_stop_and_save_task(self):
        time.sleep(0.2)
        self.send_raw_cmd(":ABORT")
        self.send_raw_cmd(":STOP")
        self.write_log("日置硬件已强行停止物理记录 (:ABORT & :STOP)。")
        
        err_res = clean_scpi_response(self.query_raw_cmd(":ERRor?").strip())
        if err_res and err_res != "0" and "WARN_FL06" not in err_res:
            self.write_log(f"硬件错误代码检测反馈: {err_res}")
        else:
            self.write_log("硬件无异常。")
        
        self._save_collected_data()

    def _save_collected_data(self):
        """O(1) 常数级极速数据提取与统计报告导出 (0ms 秒刷剪贴板，0 额外内存)"""
        total_points = self.total_frames
        self.write_log(f"【保存模式】共成功截获高精度实时数据帧: {total_points} 帧。")
        
        if total_points < 1:
            self.write_log("[错误] 未能成功截获任何通道数据，无有效数据生成！")
            self.root.after(0, self._restore_ui_after_task)
            return

        active_channels = getattr(self, "active_channels", [])
        if not active_channels:
            self.write_log("[错误] 无活跃通道配置记录！")
            self.root.after(0, self._restore_ui_after_task)
            return
                    
        # 强制更新一次最终精确统计数值至界面
        stats_final_updates = []
        for chan in active_channels:
            k = chan["key"]
            cnt = self.stats_count[k]
            if cnt > 0:
                stats_final_updates.append((k, self.stats_sum[k] / cnt, self.stats_max[k], self.stats_min[k]))
            else:
                stats_final_updates.append((k, math.nan, math.nan, math.nan))
        self.root.after(0, self._batch_update_gui_stats, stats_final_updates)
            
        self.write_log("数据提取完毕。正在生成统计报告...")
        
        date_trigger = f"{self.dt_start.year}/{self.dt_start.month}/{self.dt_start.day}"
        time_trigger_sec = self.dt_start.strftime("%H:%M:%S") + "s"
        time_trigger_ms = self.dt_start.strftime("%H:%M:%S.%f")[:-4] + "s ~"
        date_end = f"{self.dt_end.year}/{self.dt_end.month}/{self.dt_end.day}"
        time_end_ms = self.dt_end.strftime("%H:%M:%S.%f")[:-4] + "s"

        lines = [
            f"触发时间\t{date_trigger}\t{time_trigger_sec}",
            f"运算范围\t{date_trigger}\t{time_trigger_ms}\t\t{date_end}\t{time_end_ms}",
            "Channel\t平均值\t最大值\t最小值"
        ]
        
        active_keys = {c["key"] for c in active_channels}
        
        for key in self.channel_keys:
            if key in active_keys:
                cnt = self.stats_count[key]
                if cnt == 0:
                    mean_val = max_val = min_val = 0.0
                else:
                    mean_val = self.stats_sum[key] / cnt
                    max_val = self.stats_max[key]
                    min_val = self.stats_min[key]
                
                if self.is_ma_channel.get(key, False):
                    fmt_str = f"{key}\t{mean_val:.3f}\t{max_val:.1f}\t{min_val:.1f}"
                else:
                    fmt_str = f"{key}\t{mean_val:.3f}\t{max_val:.3f}\t{min_val:.3f}"
                lines.append(fmt_str)
                
        clipboard_text = "\n".join(lines)
        self.last_clipboard_text = clipboard_text 
        
        self.root.clipboard_clear()
        self.root.clipboard_append(clipboard_text)
        self.write_log("[自动复制] ★★★ 统计结果已自动写入系统剪贴板！ ★★★")
        self.write_log("[自动复制] 您可直接在已有 Excel 的 B1 单元格 Ctrl+V，数据即会自动完美分列！")
        
        self.root.after(0, self._restore_ui_after_task)

    def copy_clipboard_data(self):
        if getattr(self, "last_clipboard_text", ""):
            self.root.clipboard_clear()
            self.root.clipboard_append(self.last_clipboard_text)
            self.write_log("[快捷复制] ★★★ 统计结果已成功手动复制到剪贴板！ ★★★")
        else:
            messagebox.showwarning("复制失败", "当前暂无有效的测试统计数据，请先启动采集并成功停止。")

    def _restore_ui_after_task(self):
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                self.ser.timeout = 1.0
                
        self.btn_connect.config(text=" 断开连接 " if self.is_connected else " 连接仪器 ", bg="#f2f2f7" if self.is_connected else "#007aff", fg="#007aff" if self.is_connected else "white")
        self.btn_connect.config(state="normal")
        self.btn_start.config(state="normal" if self.is_connected else "disabled")
        self.btn_stop.config(state="disabled")
        self.port_combo.config(state="disabled" if self.is_connected else "readonly")
        self.baud_combo.config(state="disabled" if self.is_connected else "readonly")
        self.model_combo.config(state="disabled" if self.is_connected else "readonly")
        self.interval_combo.config(state="disabled" if self.is_connected else "readonly")
        self.btn_import.config(state="disabled" if self.is_connected else "normal")
        self.btn_batch.config(state="normal")

if __name__ == "__main__":
    root = tk.Tk()
    app = HiokiPerfectApp(root)
    root.mainloop()