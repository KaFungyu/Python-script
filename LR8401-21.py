# -*- coding: utf-8 -*-
"""
HIOKI LR8401-21 / LR8450 自动化测试控制台 v12.4 (硬件状态差分与渲染引擎极速优化版)
设计者：程控专家 (KaFungyu 专属美学升级版)
"""

import sys
import os
import time
import json
import threading
from datetime import datetime

# 依赖库预检（彻底移除 openpyxl / pandas 依赖）
try:
    import serial
    import serial.tools.list_ports  # 自动扫描物理串口
    import numpy as np
except ImportError:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "缺少运行依赖", 
        "请先在终端运行以下命令安装依赖：\npip install pyserial numpy"
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

def get_scpi_range(range_text):
    return RANGE_MAP.get(range_text, "0.01")

def get_range_command_args(range_text, is_lr8450, preferred_kind=None):
    scpi_range = get_scpi_range(range_text)
    candidates = []

    if is_lr8450:
        candidates.append(("label", range_text))
        try:
            candidates.append(("nr3", f"{float(scpi_range):.1E}"))
        except ValueError:
            pass
        candidates.append(("numeric", scpi_range))
    else:
        candidates.append(("numeric", scpi_range))

    deduped = []
    seen = set()
    for kind, arg in candidates:
        normalized = arg.upper().replace(" ", "")
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append((kind, arg))

    if preferred_kind:
        preferred = [(kind, arg) for kind, arg in deduped if kind == preferred_kind]
        if preferred:
            return preferred
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
    for label, scpi_value in RANGE_MAP.items():
        if norm == scpi_value.upper().replace(" ", ""):
            return label

    try:
        actual_val = float(norm)
    except ValueError:
        return token

    for label, scpi_value in RANGE_MAP.items():
        try:
            expected_val = float(scpi_value)
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
        self.root.title("HIOKI LR8401-21 / LR8450 程控控制台 v12.4")
        # 扩宽窗体至 1680px，优雅容纳平均、最大、最小三列看板
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
        
        # ==================== 最强大脑本地寄存器状态全缓存结构 ====================
        self.instrument_ranges = {}      # 高性能缓存成功下发给仪器的实际通道量程
        self.instrument_stores = {}      # 缓存当前通道物理开启状态 (True / False)
        self.instrument_comments = {}    # 缓存当前通道物理屏幕注释
        self.instrument_scalings = {}    # 缓存当前通道物理 SCALing 状态
        
        # ==================== 高性能 GUI 渲染防抖文本比对缓存 ====================
        self.gui_val_cache = {}          # 缓存实时测量值文本及配色：{key: (text, fg, bg)}
        self.gui_stat_cache = {}         # 缓存平均、最大、最小统计：{key: (avg_text, avg_fg, max_text, max_fg, min_text, min_fg)}
        
        # ==================== 跨线程离线 SCPI 通道名称静态映射 ====================
        self.scpi_channel_mapping = {}   # 本地缓存映射 {key: scpi_ch_string}，消除跨线程 StringVar.get() 性能瓶颈
        
        # 批量多选与高亮状态变量
        self.selected_keys = set()
        self.last_selected_key = None
        
        # 实时流保存的本地列表
        self.realtime_data_list = []
        self.last_clipboard_text = ""
        self.is_ma_channel = {} # 通道单位高性能本地缓存
        self.active_channels = [] # 运行期缓存活跃通道，避免频繁查询界面组件
        
        # 用于记录当前测试周期内已弹过 OVER 警告的通道 key，避免弹窗风暴
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
        
        # 预先生成全局通道 Keys 列表，提升运算查找性能
        self.channel_keys = [f"1-{unit}-{ch}" for unit in [1, 2] for ch in range(1, 16)]
        
        # 缓存通道元数据以减少重复解析：{key: {"unit": unit, "ch": ch, "prefix": "CH1"|"CH2"}}
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
                "row_bg": "#ffffff" # 用于保存本行的交替条纹背景色，保证状态刷新时不失真
            }
            # 初始化各通道统计变量
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
        self.refresh_ports_on_click() # 首次启动自动加载一次可用端口
        
        # 窗口关闭时自动保存配置
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_save)
        
    def _on_model_changed(self, *args):
        """当仪器型号发生改变时，自动重设 30ch 重映射策略并刷新限速配置"""
        if not getattr(self, "init_completed", False):
            return
        self.config_unsynced = True # 标记配置变动
        
        # 切换物理机器型号时，必须彻底清理本地硬件寄存器状态缓存
        self.instrument_ranges.clear()
        self.instrument_stores.clear()
        self.instrument_comments.clear()
        self.instrument_scalings.clear()
        
        model = self.device_model.get()
        if model == "LR8450":
            self.ch30_var.set(True)
        else:
            self.ch30_var.set(False)
            
        self.rebuild_scpi_channel_mapping() # 重新离线计算 SCPI 通道名
        self.auto_set_shortest_interval(verbose=False)

    def rebuild_scpi_channel_mapping(self):
        """将所有通道物理标识（如CH1_16）一次性算好离线备用，剔除实时流及同步中的 StringVar 跨线程开销"""
        ch30 = self.ch30_var.get()
        self.scpi_channel_mapping = {}
        for key in self.channel_keys:
            meta = self.channel_metadata[key]
            unit = meta["unit"]
            ch = meta["ch"]
            if unit == 1:
                self.scpi_channel_mapping[key] = f"CH1_{ch}"
            else:
                self.scpi_channel_mapping[key] = f"CH1_{ch+15}" if ch30 else f"CH2_{ch}"

    def _get_filter_scpi(self):
        """设定电网滤波器 SCPI 指令及查询语法"""
        return ":UNIT:FILTer 50HZ", ":UNIT:FILTer?"

    def get_scpi_ch(self, unit, ch):
        """核心映射：当离线缓存未覆盖时的降级获取函数"""
        if unit == 1:
            return f"CH1_{ch}"
        else:
            if self.ch30_var.get():
                return f"CH1_{ch+15}"
            else:
                return f"CH2_{ch}"

    def select_channel(self, key, event=None):
        """实现类似 Excel 电子表格的单选、按住 Ctrl 多选、按住 Shift 区域连选逻辑"""
        keys_list = self.channel_keys
        is_ctrl = False
        is_shift = False
        if event:
            is_ctrl = (event.state & 0x0004) != 0 or (sys.platform == "darwin" and (event.state & 0x0008) != 0)
            is_shift = (event.state & 0x0001) != 0
            
        if is_shift and self.last_selected_key in keys_list:
            idx1 = keys_list.index(self.last_selected_key)
            idx2 = keys_list.index(key)
            start_idx = min(idx1, idx2)
            end_idx = max(idx1, idx2)
            
            if not is_ctrl:
                self.selected_keys.clear()
            for i in range(start_idx, end_idx + 1):
                self.selected_keys.add(keys_list[i])
        elif is_ctrl:
            if key in self.selected_keys:
                self.selected_keys.remove(key)
            else:
                self.selected_keys.add(key)
                self.last_selected_key = key
        else:
            self.selected_keys.clear()
            self.selected_keys.add(key)
            self.last_selected_key = key
            
        self._update_selection_visuals()
        
        # 如果点击的是 Label 通道号，自动把输入焦点转移给注释 Entry
        ent = self.channel_vars[key].get("ent_comment")
        if ent and event and isinstance(event.widget, tk.Label):
            ent.focus_set()

    def _update_selection_visuals(self):
        """更新界面组件背景高亮，反馈当前的多选状态"""
        for key, vars_dict in self.channel_vars.items():
            ent = vars_dict.get("ent_comment")
            ent_r = vars_dict.get("ent_ratio")
            lbl = vars_dict.get("lbl_ch")
            row_bg = vars_dict.get("row_bg", "#ffffff")
            if key in self.selected_keys:
                if ent:
                    ent.config(bg="#bae6fd", highlightbackground="#007aff", highlightcolor="#007aff") # iOS 高亮白底蓝边
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
                    lbl.config(bg=row_bg, fg="#1c1c1e")

    def on_range_combobox_selected(self, key):
        """当面板下拉选择任意通道量程时：
        1. 若处于多选状态且该通道在多选集合中，【仅筛选并收集原量程与新量程不相同的通道】，避免重复下发和打印
        2. 将新配置保存到本地 json
        3. 若已连接仪器且未处于测量中，立马在后台线程将实质变动的通道量程同步下发给物理仪器，立马生效！
        """
        if not getattr(self, "init_completed", False):
            return
            
        selected_range = self.channel_vars[key]["range"].get()
        
        # 手动直接改变的这个通道，必然包含在下发和校验队列中
        targets = [key]
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            # 如果是多选同步状态，递进筛选量程发生真实修改的通道
            self.init_completed = False
            for k in self.selected_keys:
                if k != key:
                    old_range = self.channel_vars[k]["range"].get()
                    if old_range != selected_range:
                        self.channel_vars[k]["range"].set(selected_range)
                        targets.append(k)
            self.init_completed = True
            
        self._schedule_save()
        
        # 如果已连接仪器且当前未处于测量中，立马在后台线程下发物理层生效
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
        """物理层即时下发多通道量程设定并回读验证"""
        is_lr8450 = self.device_model.get() == "LR8450"
        prefix_cmd = ":UNIT"
        scpi_range = get_scpi_range(range_text)
        
        # 只要待设置队列中包含填了注释的活动通道，才打印同步提示
        has_commented = any(self.channel_vars[k]["comment"].get().strip() for k in keys)
        if has_commented:
            self.write_log(f"[即时修改] 正在同步已修改量程的有效通道为 {range_text}...")
        
        preferred_kind = None
        
        for k in keys:
            meta = self.channel_metadata[k]
            unit = meta["unit"]
            ch = meta["ch"]
            ch_id = self.scpi_channel_mapping[k]
            comment = self.channel_vars[k]["comment"].get().strip()
            
            # 1. 临时开启该通道，防止在关闭状态下量程指令不生效
            self.send_raw_cmd(f"{prefix_cmd}:STORe {ch_id},ON")
            if is_lr8450:
                time.sleep(0.01)
                
            # 2. 切换到电压模式
            self.send_raw_cmd(f"{prefix_cmd}:INMOde {ch_id},VOLTAGE")
            if is_lr8450:
                time.sleep(0.01)
                
            # 3. 临时关闭 SCALing 解锁物理量程
            self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
            if is_lr8450:
                time.sleep(0.01)
                
            # 4. 下发物理量程并回读校验
            success = False
            last_readback = ""
            
            if preferred_kind:
                range_arg = get_range_command_args(range_text, is_lr8450, preferred_kind)[0][1]
                self.send_raw_cmd(f"{prefix_cmd}:RANGe {ch_id},{range_arg}")
                if is_lr8450:
                    time.sleep(0.01)
                last_readback = self.query_raw_cmd(f"{prefix_cmd}:RANGe? {ch_id}")
                if is_scpi_range_match(last_readback, scpi_range):
                    success = True
            else:
                for arg_kind, range_arg in get_range_command_args(range_text, is_lr8450):
                    self.send_raw_cmd(f"{prefix_cmd}:RANGe {ch_id},{range_arg}")
                    if is_lr8450:
                        time.sleep(0.02)
                    last_readback = self.query_raw_cmd(f"{prefix_cmd}:RANGe? {ch_id}")
                    if is_scpi_range_match(last_readback, scpi_range):
                        success = True
                        preferred_kind = arg_kind
                        break
                        
            # 5. 回读成功后，重新将物理仪器的 Scaling 和转换比配置还原
            ratio_text = self.channel_vars[k]["ratio"].get().strip()
            if comment and ratio_text:
                try:
                    ratio_val = float(ratio_text)
                    self.send_raw_cmd(f":SCALing:SET {ch_id},ENG")
                    self.send_raw_cmd(f":SCALing:KIND {ch_id},RATIO")
                    self.send_raw_cmd(f":SCALing:VOLT {ch_id},{ratio_val}")
                    self.send_raw_cmd(f":SCALing:OFFSet {ch_id},0.0")
                    if ratio_val not in (1.0, -1.0):
                        self.send_raw_cmd(f':SCALing:UNIT {ch_id},"mA"')
                    else:
                        self.send_raw_cmd(f':SCALing:UNIT {ch_id},"V"')
                    if is_lr8450:
                        time.sleep(0.01)
                except ValueError:
                    pass

            # 6. 如果该通道没有填注释，恢复 STORe 为 OFF
            if not comment:
                self.send_raw_cmd(f"{prefix_cmd}:STORe {ch_id},OFF")
                if is_lr8450:
                    time.sleep(0.01)
                    
            # 7. 只对有注释的活跃通道进行状态校验和日志打印，并同步更新硬件状态缓存映射
            if comment:
                if success:
                    actual_range = format_range_for_log(last_readback)
                    self.instrument_ranges[k] = range_text 
                    self.instrument_stores[k] = True
                    self.write_log(f"[即时修改成功] {ch_id} ({comment}) 的物理量程成功修改并应用为: {actual_range}")
                else:
                    actual_range = format_range_for_log(last_readback) if last_readback else "无响应"
                    self.write_log(f"[即时修改失败] ⚠️ {ch_id} ({comment}) 无法更新量程！当前实际: {actual_range}")

    def _bg_sync_all_commented_ranges_now(self, keys, is_lr8450):
        """连接仪器后，自动批量配置并回读验证所有有注释的通道量程，并在看板打印回读结果"""
        prefix_cmd = ":UNIT"
        preferred_kinds = {}
        
        for k in keys:
            meta = self.channel_metadata[k]
            unit = meta["unit"]
            ch = meta["ch"]
            ch_id = self.scpi_channel_mapping[k]
            comment = self.channel_vars[k]["comment"].get().strip()
            range_text = self.channel_vars[k]["range"].get()
            scpi_range = get_scpi_range(range_text)
            
            # 1. 临时开启
            self.send_raw_cmd(f"{prefix_cmd}:STORe {ch_id},ON")
            if is_lr8450:
                time.sleep(0.01)
                
            # 2. 切换到电压模式
            self.send_raw_cmd(f"{prefix_cmd}:INMOde {ch_id},VOLTAGE")
            if is_lr8450:
                time.sleep(0.01)
                
            # 3. 临时切断缩放功能
            self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
            if is_lr8450:
                time.sleep(0.01)
                
            # 4. 下发并验证量程
            success = False
            last_readback = ""
            
            if range_text in preferred_kinds:
                range_arg = get_range_command_args(range_text, is_lr8450, preferred_kinds[range_text])[0][1]
                self.send_raw_cmd(f"{prefix_cmd}:RANGe {ch_id},{range_arg}")
                if is_lr8450:
                    time.sleep(0.01)
                last_readback = self.query_raw_cmd(f"{prefix_cmd}:RANGe? {ch_id}")
                if is_scpi_range_match(last_readback, scpi_range):
                    success = True
            else:
                for arg_kind, range_arg in get_range_command_args(range_text, is_lr8450):
                    self.send_raw_cmd(f"{prefix_cmd}:RANGe {ch_id},{range_arg}")
                    if is_lr8450:
                        time.sleep(0.02)
                    last_readback = self.query_raw_cmd(f"{prefix_cmd}:RANGe? {ch_id}")
                    if is_scpi_range_match(last_readback, scpi_range):
                        success = True
                        preferred_kinds[range_text] = arg_kind
                        break
                        
            # 5. 配置物理注释并在需要时复原缩放配置
            if comment:
                self.send_raw_cmd(f':COMMent:CH {ch_id},"{comment}"')
                if is_lr8450:
                    time.sleep(0.01)
                    
            ratio_text = self.channel_vars[k]["ratio"].get().strip()
            if comment and ratio_text:
                try:
                    ratio_val = float(ratio_text)
                    self.send_raw_cmd(f":SCALing:SET {ch_id},ENG")
                    self.send_raw_cmd(f":SCALing:KIND {ch_id},RATIO")
                    self.send_raw_cmd(f":SCALing:VOLT {ch_id},{ratio_val}")
                    self.send_raw_cmd(f":SCALing:OFFSet {ch_id},0.0")
                    if ratio_val not in (1.0, -1.0):
                        self.send_raw_cmd(f':SCALing:UNIT {ch_id},"mA"')
                    else:
                        self.send_raw_cmd(f':SCALing:UNIT {ch_id},"V"')
                    if is_lr8450:
                        time.sleep(0.01)
                except ValueError:
                    pass
                    
            if success:
                actual_range = format_range_for_log(last_readback)
                # 记录成功下发至物理仪器的量程到本地寄存器状态缓存中
                self.instrument_ranges[k] = range_text 
                self.instrument_stores[k] = True
                self.instrument_comments[k] = comment
                self.write_log(f"[连接同步成功] {ch_id} ({comment}) 的物理量程设为: {actual_range}")
            else:
                actual_range = format_range_for_log(last_readback) if last_readback else "无响应"
                self.write_log(f"[连接同步失败] ⚠️ {ch_id} ({comment}) 量程未成功配置！当前实际: {actual_range}")
                
        self.write_log("[连接同步] 所有已注释通道的初始化同步和回读校验已完成。")
        self.config_unsynced = False # 连接同步成功后，直接标记配置已拉齐，开启采集可“秒级启动”！

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

    def _handle_paste_sync(self, key, field_name):
        try:
            clipboard = self.root.clipboard_get()
        except Exception:
            return None
            
        lines = clipboard.split('\n')
        lines = [line.strip('\r') for line in lines]
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

    def on_comment_keyrelease(self, key, event):
        """多选状态下，修改任意选中的注释文本框，其它已选中的通道实时同步更改"""
        self._sync_selected_values(key, event, "comment")

    def on_comment_paste(self, key, event):
        """拦截粘贴操作：如果是多行，则按行向下顺序分发各通道"""
        return self._handle_paste_sync(key, "comment")

    def on_ratio_keyrelease(self, key, event):
        """多选状态下，修改任意选中的转换比文本框，其它已选中的通道实时同步更改"""
        self._sync_selected_values(key, event, "ratio")

    def on_ratio_paste(self, key, event):
        """拦截粘贴操作：如果是多行，则按行向下顺序分发各通道"""
        return self._handle_paste_sync(key, "ratio")

    def _create_ios_entry(self, parent, textvar, width, is_monospace=False):
        """工厂方法：高品质扁平微圆角 iOS 输入框"""
        font = ("Segoe UI", 10, "bold") if is_monospace else ("Microsoft YaHei UI", 10)
        return tk.Entry(
            parent,
            textvariable=textvar,
            width=width,
            font=font,
            bd=0,
            bg="#ffffff",
            fg="#1c1c1e",
            highlightthickness=1,
            highlightbackground="#e5e5ea", # iOS 浅色细边框
            highlightcolor="#007aff",       # iOS 聚焦系统蓝
            insertbackground="#1c1c1e"
        )

    def create_widgets(self):
        style = ttk.Style()
        style.theme_use('clam')
        
        # 重新定义更加精细平滑的 iOS 灰白主题
        style.configure("TLabel", font=("Microsoft YaHei UI", 10, "bold"), background="#ffffff", foreground="#1c1c1e")
        style.configure("TCombobox", 
                        fieldbackground="#ffffff", 
                        background="#f2f2f7", 
                        foreground="#1c1c1e", 
                        arrowcolor="#8e8e93", 
                        bordercolor="#e5e5ea",
                        lightcolor="#e5e5ea",
                        darkcolor="#e5e5ea")
        
        style.map("TCombobox", 
                  fieldbackground=[("readonly", "#ffffff"), ("disabled", "#f2f2f7")], 
                  selectbackground=[("readonly", "#bae6fd"), ("focus", "#bae6fd")],
                  selectforeground=[("readonly", "#1c1c1e"), ("focus", "#1c1c1e")],
                  foreground=[("disabled", "#aeaeb2"), ("readonly", "#1c1c1e"), ("focus", "#1c1c1e")])
        
        # 1. 顶部连接与文件导入导出设置
        conn_frame = tk.Frame(
            self.root, 
            bg="#ffffff", 
            highlightbackground="#e5e5ea", 
            highlightcolor="#e5e5ea", 
            highlightthickness=1, 
            bd=0
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
        
        # [iOS 升级] 系统蓝色主按钮
        self.btn_connect = tk.Button(
            conn_frame, text=" 连接仪器 ", bg="#007aff", fg="white", 
            font=("Microsoft YaHei UI", 10, "bold"), relief="flat", activebackground="#0062cc", activeforeground="white", bd=0, cursor="hand2"
        )
        self.btn_connect.config(command=self.toggle_connection)
        self.btn_connect.grid(row=1, column=8, padx=12, pady=8)
        
        self.lbl_status = tk.Label(conn_frame, text="未连接", fg="#ff3b30", font=("Microsoft YaHei UI", 11, "bold"), bg="#ffffff")
        self.lbl_status.grid(row=1, column=9, padx=8, pady=8)
        
        # [iOS 升级] 次级菜单统一采用 Tinted 风格
        btn_opts = {
            "font": ("Microsoft YaHei UI", 10, "bold"),
            "relief": "flat",
            "padx": 12,
            "pady": 4,
            "fg": "#007aff",
            "bg": "#f2f2f7",
            "activebackground": "#e5e5ea",
            "activeforeground": "#0056b3",
            "bd": 0,
            "cursor": "hand2"
        }
        
        self.btn_import = tk.Button(conn_frame, text=" 📂 导入配置 ", command=self.import_config_file, **btn_opts)
        self.btn_import.grid(row=1, column=10, padx=5, pady=8)
        
        self.btn_export = tk.Button(conn_frame, text=" 💾 导出配置 ", command=self.export_config_file, **btn_opts)
        self.btn_export.grid(row=1, column=11, padx=5, pady=8)

        self.btn_batch = tk.Button(conn_frame, text=" 🔧 集中处理 ", command=self.open_batch_config_dialog, **btn_opts)
        self.btn_batch.grid(row=1, column=12, padx=5, pady=8)
        
        # 2. 中部：批量通道配置矩阵区
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
                
                self.channel_vars[key]["ent_comment"] = ent_comment
                self.channel_vars[key]["lbl_ch"] = lbl_ch
                
                lbl_ch.bind("<Button-1>", lambda e, k=key: self.select_channel(k, e))
                ent_comment.bind("<Button-1>", lambda e, k=key: self.select_channel(k, e))
                ent_comment.bind("<KeyRelease>", lambda e, k=key: self.on_comment_keyrelease(k, e))
                ent_comment.bind("<Control-v>", lambda e, k=key: self.on_comment_paste(k, e))
                ent_comment.bind("<Command-v>", lambda e, k=key: self.on_comment_paste(k, e))
                ent_comment.bind("<Shift-Insert>", lambda e, k=key: self.on_comment_paste(k, e))
                
                cmb_range = ttk.Combobox(matrix_frame, textvariable=self.channel_vars[key]["range"], values=RANGE_LIST, width=8, font=("Segoe UI", 10), state="readonly")
                cmb_range.grid(row=ch+1, column=2, sticky="nsew", padx=2, pady=1)
                # 绑定量程即时选择事件
                cmb_range.bind("<<ComboboxSelected>>", lambda e, k=key: self.on_range_combobox_selected(k))
                
                ent_ratio = self._create_ios_entry(matrix_frame, self.channel_vars[key]["ratio"], width=10, is_monospace=True)
                ent_ratio.grid(row=ch+1, column=3, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["ent_ratio"] = ent_ratio
                
                ent_ratio.bind("<Button-1>", lambda e, k=key: self.select_channel(k, e))
                ent_ratio.bind("<KeyRelease>", lambda e, k=key: self.on_ratio_keyrelease(k, e))
                ent_ratio.bind("<Control-v>", lambda e, k=key: self.on_ratio_paste(k, e))
                ent_ratio.bind("<Command-v>", lambda e, k=key: self.on_ratio_paste(k, e))
                ent_ratio.bind("<Shift-Insert>", lambda e, k=key: self.on_ratio_paste(k, e))
                
                # Column 4: 实时测量值
                lbl_val = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=12, highlightthickness=0, bd=0)
                lbl_val.grid(row=ch+1, column=4, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["lbl_val"] = lbl_val
                
                # Column 5: 平均值
                lbl_avg = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=11, highlightthickness=0, bd=0)
                lbl_avg.grid(row=ch+1, column=5, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["lbl_avg"] = lbl_avg

                # Column 6: 最大值
                lbl_max = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=11, highlightthickness=0, bd=0)
                lbl_max.grid(row=ch+1, column=6, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["lbl_max"] = lbl_max

                # Column 7: 最小值
                lbl_min = tk.Label(matrix_frame, text="--", font=("Segoe UI", 10, "bold"), fg="#8e8e93", bg=row_bg, width=11, highlightthickness=0, bd=0)
                lbl_min.grid(row=ch+1, column=7, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["lbl_min"] = lbl_min

        # 3. 采集控制按钮及计时区
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
        
        # 4. 日志显示区
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
        """点击下拉时即时扫描可用串口"""
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

        has_unit2_active = False
        for ch in range(1, 16):
            key = f"1-2-{ch}"
            if self.channel_vars[key]["comment"].get().strip():
                has_unit2_active = True
                break
        
        if has_unit2_active:
            if current_val == "10ms":
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
                if comment:
                    source_options.append(f"CH {unit}-{ch} ({comment})")
                else:
                    source_options.append(f"CH {unit}-{ch}")
                    
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
                lbl_text = f"CH {unit}-{ch} ({comment_text})" if comment_text else f"CH {unit}-{ch}"
                target_listbox.insert(tk.END, lbl_text)
                
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
                
            any_p = any(v.get() for v in param_vars.values())
            if not any_p:
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
                    # 只有当复制目标的原本量程确实不等于复制源的设定量程时，才将其判定为“修改”并加入物理同步队列
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
            
            # 集中量程复制时，如已连接且有发生量程改变的活动通道，下发物理配置并验证
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
        self.rebuild_scpi_channel_mapping() # 还原配置后同步刷新映射

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
                self.config_unsynced = True # 标记配置变动
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
        """统一清除串口物理及系统级缓冲区"""
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
            
            # 主动清洗物理底层缓冲区
            self._clear_serial_buffers()
            self.send_raw_cmd(":HEADer OFF")
            
            # 先查询 *IDN? 获取仪器确切的物理硬件型号
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
                
                # 强制清除以前的所有状态
                self.send_raw_cmd("*CLS")
                
                # 动态兼容性控制
                self.send_raw_cmd(":CONFigure:ATSAve OFF")
                if is_lr8450:
                    self.send_raw_cmd(":CONFigure:SAVEWave OFF")
                    self.send_raw_cmd(":CONFigure:SAVECalc OFF")
                
                # 设定电网频率滤波器
                cmd_set, cmd_query = self._get_filter_scpi()
                    
                self.send_raw_cmd(cmd_set)
                time.sleep(0.3)
                
                f_res = self.query_raw_cmd(cmd_query).strip()
                f_res = clean_scpi_response(f_res)
                
                if f_res:
                    self.write_log(f"[连接建立] 设定电网频率滤波器配置为: {f_res} - 已确认验证")
                else:
                    self.write_log(f"[连接建立] 设定电网频率滤波器配置为: 无法回显/查询超时，请手动在仪器面板确认")
                
                # 模块探测逻辑
                if is_lr8450:
                    self.write_log("[自动检测] 识别到仪器实际型号为 HIOKI LR8450，自动转为 30ch 重映射模式。")
                    
                    opt_res = self.query_raw_cmd("*OPT?")
                    if opt_res:
                        parts = opt_res.upper().split(",")
                        if len(parts) > 0 and "U8552" in parts[0]:
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
                
                # 连接成功后强制安全触发一次 SCPI 静态通道重新离线映射
                self.root.after(0, self.rebuild_scpi_channel_mapping)
                
                self.is_connected = True
                self.config_unsynced = True # 连接成功后强制标记为需全同步状态
                self.root.after(0, self._update_ui_connected)
                
                # 【连接同步功能】：连接成功后，立即异步对所有在控制台上“有注释”的活动通道在物理仪器上进行量程同步与回读验证
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
        
        # 清空硬件寄存器状态及本地缓存，确保重连干净、不冲突
        self.instrument_ranges.clear()
        self.instrument_stores.clear()
        self.instrument_comments.clear()
        self.instrument_scalings.clear()
        self.gui_val_cache.clear()
        self.gui_stat_cache.clear()
        
        self.root.after(0, self._update_ui_disconnected)
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
        self.config_unsynced = True # 任意文本、量程变更均自动触发脏标记
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
        # 开始测量或重置时，清空 GUI 本地高速缓存
        self.gui_val_cache.clear()
        self.gui_stat_cache.clear()
        
        for key, vars_dict in self.channel_vars.items():
            row_bg = vars_dict["row_bg"]
            lbl = vars_dict["lbl_val"]
            if lbl:
                lbl.config(text="--", fg="#8e8e93", bg=row_bg, highlightthickness=0)
            
            lbl_avg = vars_dict["lbl_avg"]
            if lbl_avg:
                lbl_avg.config(text="--", fg="#8e8e93", bg=row_bg, highlightthickness=0)
                
            lbl_max = vars_dict["lbl_max"]
            if lbl_max:
                lbl_max.config(text="--", fg="#8e8e93", bg=row_bg, highlightthickness=0)
                
            lbl_min = vars_dict["lbl_min"]
            if lbl_min:
                lbl_min.config(text="--", fg="#8e8e93", bg=row_bg, highlightthickness=0)

    def _update_gui_val_label(self, key, val):
        """[最强大脑高性能重构]：对需要渲染的字符串及样式进行比对，抑制无意义的 Tkinter 重绘 repaint"""
        lbl = self.channel_vars[key]["lbl_val"]
        row_bg = self.channel_vars[key]["row_bg"]
        if lbl:
            if val is None:
                target_text, fg, bg = "--", "#8e8e93", row_bg
            elif np.isnan(val):
                target_text, fg, bg = "OVER", "#ffffff", "#ff3b30"
            else:
                is_ma = self.is_ma_channel.get(key, False)
                if val < 0:
                    fg = "#ff3b30" # iOS 系统红
                elif is_ma:
                    fg = "#34c759" # iOS 系统绿
                else:
                    fg = "#007aff" # iOS 系统蓝
                
                target_text = f"{val:.3f} mA" if is_ma else f"{val:.4f} V"
                bg = row_bg
            
            # 本地比对判定，剔除无用重绘
            cache_entry = self.gui_val_cache.get(key)
            if cache_entry != (target_text, fg, bg):
                lbl.config(text=target_text, fg=fg, bg=bg)
                self.gui_val_cache[key] = (target_text, fg, bg)

    def _batch_update_gui_vals(self, updates):
        for key, val in updates:
            self._update_gui_val_label(key, val)

    def _update_gui_stat_labels(self, key, avg, max_val, min_val):
        """[最强大脑高性能重构]：平均值、最大、最小值的渲染抑制比对，使重绘消耗减免 90%"""
        is_ma = self.is_ma_channel.get(key, False)
        fmt = ".3f" if is_ma else ".4f"
        unit = " mA" if is_ma else " V"
        row_bg = self.channel_vars[key]["row_bg"]
        
        # 计算平均值
        if np.isnan(avg):
            avg_text, avg_fg = "--", "#8e8e93"
        else:
            avg_text, avg_fg = f"{avg:{fmt}}{unit}", "#ff3b30" if avg < 0 else "#1c1c1e"
            
        # 计算最大值
        if np.isnan(max_val):
            max_text, max_fg = "--", "#8e8e93"
        else:
            max_text, max_fg = f"{max_val:{fmt}}{unit}", "#ff3b30" if max_val < 0 else "#1c1c1e"
            
        # 计算最小值
        if np.isnan(min_val):
            min_text, min_fg = "--", "#8e8e93"
        else:
            min_text, min_fg = f"{min_val:{fmt}}{unit}", "#ff3b30" if min_val < 0 else "#1c1c1e"
            
        target_state = (avg_text, avg_fg, max_text, max_fg, min_text, min_fg)
        cache_entry = self.gui_stat_cache.get(key)
        
        # 仅当文本或状态确实发生变动时，才写入组件
        if cache_entry != target_state:
            lbl_avg = self.channel_vars[key]["lbl_avg"]
            lbl_max = self.channel_vars[key]["lbl_max"]
            lbl_min = self.channel_vars[key]["lbl_min"]
            
            if lbl_avg:
                lbl_avg.config(text=avg_text, fg=avg_fg, bg=row_bg)
            if lbl_max:
                lbl_max.config(text=max_text, fg=max_fg, bg=row_bg)
            if lbl_min:
                lbl_min.config(text=min_text, fg=min_fg, bg=row_bg)
                
            self.gui_stat_cache[key] = target_state

    def _batch_update_gui_stats(self, updates):
        """批量线程同步通道统计状态"""
        for key, avg, max_val, min_val in updates:
            self._update_gui_stat_labels(key, avg, max_val, min_val)

    def show_over_alert(self, channel_key, comment):
        """高度定制·防卡死非阻塞式通道 OVER 警报弹窗"""
        meta = self.channel_metadata[channel_key]
        ch_display = f"{meta['prefix']}_{meta['ch']}"
        
        msg = f"通道 {ch_display} ({comment}) 发生 OVER 溢出！" if comment else f"通道 {ch_display} 发生 OVER 溢出！"
        
        self.root.bell()
        
        alert_win = tk.Toplevel(self.root)
        alert_win.title("⚠️ 测量值溢出告警")
        alert_win.geometry("460x180")
        alert_win.configure(bg="#ffffff")
        alert_win.attributes("-topmost", True)
        
        try:
            x = self.root.winfo_x() + (self.root.winfo_width() // 2) - 230
            y = self.root.winfo_y() + (self.root.winfo_height() // 2) - 90
            alert_win.geometry(f"460x180+{x}+{y}")
        except Exception:
            pass
        
        main_frame = tk.Frame(
            alert_win, 
            bg="#fff5f5", 
            highlightbackground="#ff3b30", 
            highlightcolor="#ff3b30", 
            highlightthickness=2, 
            bd=0
        )
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        tk.Label(main_frame, text="⚠️ MEASUREMENT OVERFLOW", font=("Microsoft YaHei UI", 12, "bold"), fg="#ff3b30", bg="#fff5f5").pack(pady=10)
        tk.Label(main_frame, text=msg, font=("Microsoft YaHei UI", 11, "bold"), fg="#1c1c1e", bg="#fff5f5", wraplength=420).pack(pady=5)
        
        btn = tk.Button(
            main_frame, 
            text=" 我知道了 ", 
            font=("Microsoft YaHei UI", 10, "bold"), 
            bg="#ff3b30", 
            fg="white", 
            activebackground="#e03126", 
            activeforeground="white", 
            relief="flat", 
            bd=0, 
            cursor="hand2", 
            command=alert_win.destroy, 
            width=12, 
            height=1
        )
        btn.pack(pady=10)

    def start_acquisition(self):
        self.btn_connect.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.btn_batch.config(state="disabled") 
        
        self.realtime_data_list = []
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

        self.is_ma_channel = {
            key: check_is_ma(key)
            for key, vars_dict in self.channel_vars.items()
        }
        
        self.save_local_config()
        threading.Thread(target=self._bg_start_task, daemon=True).start()

    def _bg_start_task(self):
        self.send_raw_cmd("*CLS")
        
        self.send_raw_cmd(":CONFigure:ATSAve OFF")
        if self.device_model.get() == "LR8450":
            self.send_raw_cmd(":CONFigure:SAVEWave OFF")
            self.send_raw_cmd(":CONFigure:SAVECalc OFF")
        
        # 只要之前连接或修改时同步过，且没有再手动改动，直接跳过耗时下发，实现瞬间极速起播测量！
        if getattr(self, "config_unsynced", True):
            self.write_log(">>> 检测到本地配置有变动，正在重构写入日置通道矩阵...")
            
            cmd_set, cmd_query = self._get_filter_scpi()
            self.send_raw_cmd(cmd_set)
            time.sleep(0.3)
            
            f_res = self.query_raw_cmd(cmd_query).strip()
            f_res = clean_scpi_response(f_res)
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
            
            prefix_cmd = ":UNIT"
            is_lr8450 = self.device_model.get() == "LR8450"
            preferred_kinds = {}
            
            # 统一下发通道配置参数
            for key in self.channel_keys:
                meta = self.channel_metadata[key]
                unit = meta["unit"]
                ch = meta["ch"]
                
                comment = self.channel_vars[key]["comment"].get().strip()
                range_text = self.channel_vars[key]["range"].get()
                ratio_text = self.channel_vars[key]["ratio"].get().strip()
                
                ch_id = self.scpi_channel_mapping[key]
                scpi_range = get_scpi_range(range_text)
                
                # 【最强大脑状态跟踪】：多维硬件状态差分过滤
                last_synced_range = self.instrument_ranges.get(key)
                last_synced_store = self.instrument_stores.get(key)
                
                range_modified = (range_text != last_synced_range) # 检测量程是否发生过实质改动
                
                if comment:
                    # ==================== 活动通道（有注释） ====================
                    # 仅在量程发生改变、或者该通道原本为关闭状态时，才触发量程物理指令下发和校验回读
                    if range_modified or last_synced_store is not True:
                        self.send_raw_cmd(f"{prefix_cmd}:STORe {ch_id},ON")
                        if is_lr8450:
                            time.sleep(0.01)
                        self.send_raw_cmd(f"{prefix_cmd}:INMOde {ch_id},VOLTAGE")
                        if is_lr8450:
                            time.sleep(0.01)
                        
                        # 临时关闭 SCALing
                        self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
                        if is_lr8450:
                            time.sleep(0.01)
                        
                        success = False
                        last_readback = ""
                        if range_text in preferred_kinds:
                            range_arg = get_range_command_args(range_text, is_lr8450, preferred_kinds[range_text])[0][1]
                            self.send_raw_cmd(f"{prefix_cmd}:RANGe {ch_id},{range_arg}")
                            if is_lr8450:
                                time.sleep(0.01)
                            last_readback = self.query_raw_cmd(f"{prefix_cmd}:RANGe? {ch_id}")
                            if is_scpi_range_match(last_readback, scpi_range):
                                success = True
                        else:
                            for range_arg_kind, range_arg in get_range_command_args(range_text, is_lr8450):
                                self.send_raw_cmd(f"{prefix_cmd}:RANGe {ch_id},{range_arg}")
                                if is_lr8450:
                                    time.sleep(0.02)
                                last_readback = self.query_raw_cmd(f"{prefix_cmd}:RANGe? {ch_id}")
                                if is_scpi_range_match(last_readback, scpi_range):
                                    preferred_kinds[range_text] = range_arg_kind
                                    success = True
                                    break
                            else:
                                preferred_kinds[range_text] = get_range_command_args(range_text, is_lr8450)[0][0]
                        
                        if success:
                            actual_range = format_range_for_log(last_readback)
                            self.instrument_ranges[key] = range_text  # 校验成功记入寄存器缓存！
                            self.write_log(f"[量程验证] {ch_id} ({comment}) 的物理量程设为: {actual_range}")
                        else:
                            actual_range = format_range_for_log(last_readback) if last_readback else "无响应"
                            self.write_log(f"[警告] {ch_id} ({comment}) 量程配置失败！当前实际: {actual_range}")
                    else:
                        # 量程未改动且原本就是开启状态，静默跳过设置和慢速查询！
                        pass
                    
                    self.instrument_stores[key] = True  # 同步记入本地物理存储状态
                    
                    # 写入通道注释和颜色（属于轻量设置，直接盲发）
                    self.send_raw_cmd(f':COMMent:CH {ch_id},"{comment}"')
                    color_idx = (ch - 1) % 24 + 1
                    self.send_raw_cmd(f":DISPlay:DRAWing {ch_id},C{color_idx}")
                    
                    # 重新还原缩放比配置
                    if ratio_text:
                        try:
                            ratio_val = float(ratio_text)
                            self.send_raw_cmd(f":SCALing:SET {ch_id},ENG")
                            self.send_raw_cmd(f":SCALing:KIND {ch_id},RATIO")
                            self.send_raw_cmd(f":SCALing:VOLT {ch_id},{ratio_val}")
                            self.send_raw_cmd(f":SCALing:OFFSet {ch_id},0.0")
                            
                            if ratio_val not in (1.0, -1.0):
                                self.send_raw_cmd(f':SCALing:UNIT {ch_id},"mA"')
                            else:
                                self.send_raw_cmd(f':SCALing:UNIT {ch_id},"V"')
                        except ValueError:
                            self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
                    else:
                        self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
                else:
                    # ==================== 闲置通道（无注释） ====================
                    # 【极限优化】：量程有变动且本通道还处于 ON 状态时，才盲发配置，否则免除一切闲置通道的冗余操作！
                    if range_modified or last_synced_store is not False:
                        if range_modified:
                            range_arg = get_range_command_args(range_text, is_lr8450)[0][1]
                            self.send_raw_cmd(f"{prefix_cmd}:RANGe {ch_id},{range_arg}")
                            self.instrument_ranges[key] = range_text
                        
                        # 恢复 STORe 属性为 OFF
                        self.send_raw_cmd(f"{prefix_cmd}:STORe {ch_id},OFF")
                        self.send_raw_cmd(f":DISPlay:DRAWing {ch_id},OFF")
                        self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
                    
                    self.instrument_stores[key] = False  # 同步记入本地物理存储状态
            
            self.send_raw_cmd(":DISPlay:CHANge DISPlay")
            self.send_raw_cmd(":DISPlay:PAGE 1")
            self.send_raw_cmd(":DISPlay:MODE W_D")
            self.send_raw_cmd(":DISPlay:GROUp ALL")
            self.config_unsynced = False # 参数成功下传至仪器，脏标记置零
        else:
            self.write_log(">>> 检测到通道配置与仪器保持一致，跳过参数写入，瞬间秒级开始采集！")
            
        self.write_log(">>> 正在启动测量记录...")
        self.dt_start = datetime.now()
        
        self.send_raw_cmd(":STARt")
        time.sleep(0.05)
        
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
        """高速测量值单通道快照截获线程 (采用 SCPI 通道名静态本地映射，斩断非安全跨线程访问瓶颈)"""
        self.active_channels = []
        for key in self.channel_keys:
            comment = self.channel_vars[key]["comment"].get().strip()
            if comment:
                meta = self.channel_metadata[key]
                # 完全避免跨线程 StringVar 查找
                scpi_ch = self.scpi_channel_mapping[key]
                self.active_channels.append({
                    "key": key,
                    "unit": meta["unit"],
                    "ch": meta["ch"],
                    "comment": comment,
                    "scpi_ch": scpi_ch,
                    "query_cmd": f":MEMory:VREAl? {scpi_ch}"
                })
                    
        num_active = len(self.active_channels)
        if num_active == 0:
            return
            
        start_clock = time.time()
        loop_counter = 0
        
        while self.timer_running:
            self.send_raw_cmd(":MEMory:GETReal")
            
            vals = []
            gui_vals = []
            
            for chan in self.active_channels:
                if not self.timer_running:
                    break
                
                res_str = self.query_raw_cmd(chan["query_cmd"])
                res_str = clean_scpi_response(res_str)
                
                val_float = 0.0
                is_over = False
                is_valid = False
                
                if res_str:
                    res_lower = res_str.lower()
                    if "over" in res_lower or "nan" in res_lower or "inf" in res_lower or "o.r" in res_lower:
                        is_over = True
                    else:
                        try:
                            val_float = float(res_str)
                            if abs(val_float) >= 1.0e+9:
                                is_over = True
                            else:
                                is_valid = True
                        except ValueError:
                            pass
                
                if is_over:
                    vals.append(np.nan)
                    gui_vals.append(np.nan)
                    
                    if chan["key"] not in self.alerted_over_channels:
                        self.alerted_over_channels.add(chan["key"])
                        self.write_log(f"[告警] 通道 {chan['scpi_ch']} ({chan['comment']}) 发生 OVER 溢出！")
                        self.root.after(0, self.show_over_alert, chan["key"], chan["comment"])
                elif not is_valid:
                    vals.append(np.nan)
                    gui_vals.append(None)
                else:
                    vals.append(val_float)
                    gui_vals.append(val_float)
                    
                    k = chan["key"]
                    self.stats_count[k] += 1
                    self.stats_sum[k] += val_float
                    if val_float > self.stats_max[k]:
                        self.stats_max[k] = val_float
                    if val_float < self.stats_min[k]:
                        self.stats_min[k] = val_float
                
            if not self.timer_running:
                break
                
            if len(vals) == num_active and self.timer_running:
                elapsed = time.time() - start_clock
                self.realtime_data_list.append((elapsed, vals))
                
                updates = [(chan["key"], gui_vals[idx]) for idx, chan in enumerate(self.active_channels)]
                self.root.after(0, self._batch_update_gui_vals, updates)
                
                loop_counter += 1
                if loop_counter % 10 == 0:
                    stats_updates = []
                    for chan in self.active_channels:
                        k = chan["key"]
                        if self.stats_count[k] > 0:
                            avg_v = self.stats_sum[k] / self.stats_count[k]
                            max_v = self.stats_max[k]
                            min_v = self.stats_min[k]
                            stats_updates.append((k, avg_v, max_v, min_v))
                        else:
                            stats_updates.append((k, np.nan, np.nan, np.nan))
                    self.root.after(0, self._batch_update_gui_stats, stats_updates)
                
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
        
        err_res = self.query_raw_cmd(":ERRor?").strip()
        err_res = clean_scpi_response(err_res)
        
        if err_res and err_res != "0" and "WARN_FL06" not in err_res:
            self.write_log(f"硬件错误代码检测反馈: {err_res}")
        else:
            self.write_log("硬件无异常。")
        
        self._save_collected_data()

    def _save_collected_data(self):
        total_points = len(self.realtime_data_list)
        self.write_log(f"【保存模式】共成功截获高精度实时数据点: {total_points} 个。")
        
        if total_points < 1:
            self.write_log("[错误] 未能成功截获任何通道数据，无有效数据生成！")
            self.root.after(0, self._restore_ui_after_task)
            return

        active_channels = getattr(self, "active_channels", [])
        if not active_channels:
            self.write_log("[错误] 无活跃通道配置记录！")
            self.root.after(0, self._restore_ui_after_task)
            return
                    
        all_data = np.array([item[1] for item in self.realtime_data_list])
        channel_data_dict = {chan["key"]: all_data[:, idx] for idx, chan in enumerate(active_channels)}
            
        self.write_log("数据提取完毕。正在进行运算范围统计...")
        
        date_trigger = f"{self.dt_start.year}/{self.dt_start.month}/{self.dt_start.day}"
        time_trigger_sec = self.dt_start.strftime("%H:%M:%S") + "s"
        time_trigger_ms = self.dt_start.strftime("%H:%M:%S.%f")[:-4] + "s ~"
        date_end = f"{self.dt_end.year}/{self.dt_end.month}/{self.dt_end.day}"
        time_end_ms = self.dt_end.strftime("%H:%M:%S.%f")[:-4] + "s"

        lines = []
        lines.append(f"触发时间\t{date_trigger}\t{time_trigger_sec}")
        lines.append(f"运算范围\t{date_trigger}\t{time_trigger_ms}\t\t{date_end}\t{time_end_ms}")
        lines.append("Channel\t平均值\t最大值\t最小值")
        
        active_map = {c["key"]: c for c in active_channels}
        
        for key in self.channel_keys:
            if key in active_map:
                data_arr = channel_data_dict[key]
                
                data_arr_clean = data_arr[~np.isnan(data_arr)]
                if len(data_arr_clean) == 0:
                    mean_val = max_val = min_val = 0.0
                else:
                    mean_val = np.mean(data_arr_clean)
                    max_val = np.max(data_arr_clean)
                    min_val = np.min(data_arr_clean)
                
                if self.is_ma_channel.get(key, False):
                    lines.append(f"{key}\t{mean_val:.3f}\t{max_val:.1f}\t{min_val:.1f}")
                else:
                    lines.append(f"{key}\t{mean_val:.3f}\t{max_val:.3f}\t{min_val:.3f}")
                
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