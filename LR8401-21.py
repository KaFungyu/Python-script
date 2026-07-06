# -*- coding: utf-8 -*-
"""
HIOKI LR8401-21 / LR8450 自动化测试控制台 v12.0 (iOS 高端低疲劳扁平化美学版)
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
    "100V": "100.0",
    "1-5V": "1_5V"
}
RANGE_LIST = list(RANGE_MAP.keys())

def get_scpi_range(range_text):
    return RANGE_MAP.get(range_text, "0.01")

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
        self.root.title("HIOKI LR8401-21 / LR8450 程控控制台 v12.0")
        # 扩宽窗体至 1580px，优雅容纳平均、最大、最小三列看板
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
        
        # 存储 30 个通道控件变量的字典
        self.channel_vars = {}
        for key in self.channel_keys:
            unit = int(key.split("-")[1])
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
                "row_bg": "#ffffff" # [新增] 用于保存本行的交替条纹背景色，保证状态刷新时不失真
            }
            # 初始化各通道统计变量
            self.stats_count[key] = 0
            self.stats_sum[key] = 0.0
            self.stats_max[key] = -float('inf')
            self.stats_min[key] = float('inf')
                
        self.create_widgets()
        self.load_local_config()
        
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
        model = self.device_model.get()
        if model == "LR8450":
            self.ch30_var.set(True)
        else:
            self.ch30_var.set(False)
        self.auto_set_shortest_interval(verbose=False)

    def _get_filter_scpi(self):
        """设定电网滤波器 SCPI 指令及查询语法"""
        return ":UNIT:FILTer 50HZ", ":UNIT:FILTer?"

    def get_scpi_ch(self, unit, ch):
        """核心映射：根据当前型号与 Slot 1 模块类型自动重定向物理通道"""
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

    def on_comment_keyrelease(self, key, event):
        """多选状态下，修改任意选中的注释文本框，其它已选中的通道实时同步更改 (实现批量修改通道注释)"""
        if event.keysym in ["Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Caps_Lock", "Tab", "Escape"]:
            return
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            val = self.channel_vars[key]["comment"].get()
            self.init_completed = False
            for k in self.selected_keys:
                if k != key:
                    self.channel_vars[k]["comment"].set(val)
            self.init_completed = True
            self._schedule_save()

    def on_comment_paste(self, key, event):
        """拦截粘贴操作：如果是多行（直接从 Excel 复制一列），则按行向下顺序分发各通道，无需复制30次！"""
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
                self.channel_vars[k]["comment"].set(lines[0])
            self.init_completed = True
            self.save_local_config()
            return "break"
            
        if len(lines) > 1:
            start_idx = keys_list.index(key)
            self.init_completed = False
            for i, line in enumerate(lines):
                if start_idx + i < len(keys_list):
                    target_key = keys_list[start_idx + i]
                    self.channel_vars[target_key]["comment"].set(line)
            self.init_completed = True
            self.save_local_config()
            return "break"
            
        self.root.after(50, self.save_local_config)
        return None

    def on_ratio_keyrelease(self, key, event):
        """多选状态下，修改任意选中的转换比文本框，其它已选中的通道实时同步更改 (实现批量修改通道转换比)"""
        if event.keysym in ["Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Caps_Lock", "Tab", "Escape"]:
            return
        if len(self.selected_keys) > 1 and key in self.selected_keys:
            val = self.channel_vars[key]["ratio"].get()
            self.init_completed = False
            for k in self.selected_keys:
                if k != key:
                    self.channel_vars[k]["ratio"].set(val)
            self.init_completed = True
            self._schedule_save()

    def on_ratio_paste(self, key, event):
        """拦截粘贴操作：如果是多行（直接从 Excel 复制一列），则按行向下顺序分发各通道，无需复制30次！"""
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
                self.channel_vars[k]["ratio"].set(lines[0])
            self.init_completed = True
            self.save_local_config()
            return "break"
            
        if len(lines) > 1:
            start_idx = keys_list.index(key)
            self.init_completed = False
            for i, line in enumerate(lines):
                if start_idx + i < len(keys_list):
                    target_key = keys_list[start_idx + i]
                    self.channel_vars[target_key]["ratio"].set(line)
            self.init_completed = True
            self.save_local_config()
            return "break"
            
        self.root.after(50, self.save_local_config)
        return None

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
        
        # 1. 顶部连接与文件导入导出设置 (纯白面板)
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
        
        # [iOS 升级] 次级菜单统一采用 Tinted 风格（浅灰蓝色卡片底 + 纯 iOS 蓝色字体），视觉极为平滑舒服
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
            # [iOS 升级] 弃用生硬的黑色/灰黑色实底表头，改用透明背景配合系统灰色（#8e8e93），形成极佳的通透呼吸感
            tk.Label(parent, text=title, font=("Microsoft YaHei UI", 11, "bold"), fg="#007aff", bg="#ffffff").grid(row=0, column=0, columnspan=8, pady=6)
            tk.Label(parent, text="通道", font=("Microsoft YaHei UI", 10, "bold"), fg="#8e8e93", bg="#ffffff", width=6).grid(row=1, column=0, sticky="w", padx=2)
            tk.Label(parent, text="首注释 (多选及Excel粘)", font=("Microsoft YaHei UI", 10, "bold"), fg="#8e8e93", bg="#ffffff", width=18).grid(row=1, column=1, sticky="w", padx=2)
            tk.Label(parent, text="量程选择", font=("Microsoft YaHei UI", 10, "bold"), fg="#8e8e93", bg="#ffffff", width=10).grid(row=1, column=2, sticky="w", padx=2)
            tk.Label(parent, text="转换比", font=("Microsoft YaHei UI", 10, "bold"), fg="#8e8e93", bg="#ffffff", width=10).grid(row=1, column=3, sticky="w", padx=2)
            
            # 测量列和统计列采用轻量扁平色块区分
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
                
                # [iOS 升级] 引入备忘录级别的「白 + 凉灰（#f6f6f9）交替条纹行背景」，消除杂乱的框线，阅读舒适度跨越式提升
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
                
                ent_ratio = self._create_ios_entry(matrix_frame, self.channel_vars[key]["ratio"], width=10, is_monospace=True)
                ent_ratio.grid(row=ch+1, column=3, sticky="nsew", padx=2, pady=1)
                self.channel_vars[key]["ent_ratio"] = ent_ratio
                
                ent_ratio.bind("<Button-1>", lambda e, k=key: self.select_channel(k, e))
                ent_ratio.bind("<KeyRelease>", lambda e, k=key: self.on_ratio_keyrelease(k, e))
                ent_ratio.bind("<Control-v>", lambda e, k=key: self.on_ratio_paste(k, e))
                ent_ratio.bind("<Command-v>", lambda e, k=key: self.on_ratio_paste(k, e))
                ent_ratio.bind("<Shift-Insert>", lambda e, k=key: self.on_ratio_paste(k, e))
                
                # [iOS 升级] 移除四周生硬的灰色黑框，采用 highlightthickness=0，使数值与行卡片背景彻底融合
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
        
        # [iOS 升级] 计时器采用 Apple Watch 经典圆角凉灰底卡片（#f2f2f7），大号炭黑色数显（#1c1c1e），极致现代且防刺眼
        self.lbl_timer = tk.Label(
            control_frame, text="00:00.0", font=("Segoe UI", 32, "bold"), 
            bg="#f2f2f7", fg="#1c1c1e", width=12, highlightthickness=0, bd=0
        )
        self.lbl_timer.pack(side="left", padx=15, pady=8)
        
        tk.Label(control_frame, text="SOC:", bg="#ffffff", fg="#1c1c1e", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left", padx=5)
        self.entry_case = self._create_ios_entry(control_frame, self.case_name, width=15)
        self.entry_case.pack(side="left", padx=5)
        
        # [iOS 升级] 绿、靛蓝、红三主色调全部升级为 iOS 16 标准柔和护眼系统色值
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
        """点击下拉时即时扫描可用串口，消除后台循环扫描带来的驱动卡顿与界面未响应故障"""
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
            for idx in selected_indices:
                unit = 1 if idx < 15 else 2
                ch = (idx % 15) + 1
                t_key = f"1-{unit}-{ch}"
                
                if t_key == src_key:
                    continue  
                
                if param_vars["comment"].get():
                    self.channel_vars[t_key]["comment"].set(self.channel_vars[src_key]["comment"].get())
                if param_vars["range"].get():
                    self.channel_vars[t_key]["range"].set(self.channel_vars[src_key]["range"].get())
                if param_vars["ratio"].get():
                    self.channel_vars[t_key]["ratio"].set(self.channel_vars[src_key]["ratio"].get())
                
            self.init_completed = True
            self.save_local_config()
            self._reset_gui_val_labels()
            
            self.write_log(f"[集中复制] 成功将源 [{src_display.split(' ')[1]}] 的设定参数应用至目标通道。")
            dialog.destroy()
            
        tk.Button(bottom_bar, text=" 📋 复制 ", bg="#007aff", fg="white", font=("Microsoft YaHei UI", 10, "bold"), relief="flat", width=12, command=commit_batch_copy, cursor="hand2").pack(side="right", padx=15)
        tk.Button(bottom_bar, text=" 取消 ", bg="#f2f2f7", fg="#007aff", font=("Microsoft YaHei UI", 10, "bold"), relief="flat", width=12, command=dialog.destroy, cursor="hand2").pack(side="right", padx=5)

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
                    
                    if "soc" in config:
                        self.case_name.set(config.get("soc", ""))
                    if "device_model" in config:
                        self.device_model.set(config.get("device_model", "LR8401-21"))
                    if "ch_30" in config:
                        self.ch30_var.set(config.get("ch_30", False))
                    if "interval" in config:
                        self.interval_var.set(config.get("interval", "10ms"))
                        
                    if "channels" in config:
                        channels_data = config["channels"]
                    else:
                        channels_data = config
                        
                    for key, val in channels_data.items():
                        if key in self.channel_vars:
                            unit = int(key.split("-")[1])
                            default_range = "10mV" if unit == 1 else "1V"
                            default_ratio = "-50000" if unit == 1 else "-1"
                            self.channel_vars[key]["comment"].set(val.get("comment", ""))
                            self.channel_vars[key]["range"].set(val.get("range", default_range))
                            self.channel_vars[key]["ratio"].set(val.get("ratio", default_ratio))
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
                config = {
                    "soc": self.case_name.get().strip(),
                    "device_model": self.device_model.get(),
                    "ch_30": self.ch30_var.get(),
                    "interval": self.interval_var.get(),
                    "channels": {}
                }
                for key, vars_dict in self.channel_vars.items():
                    config["channels"][key] = {
                        "comment": vars_dict["comment"].get().strip(),
                        "range": vars_dict["range"].get(),
                        "ratio": vars_dict["ratio"].get().strip()
                    }
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
        """统一清除串口物理及系统级缓冲区，防御性排除中途断线异常与指令队列帧错位"""
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
            # 连接与初始配置阶段保持 1.0 秒宽松超时，给予仪器硬件切换（如滤波器重构）充足的物理响应时间
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
                
                # 使用局部变量确定型号，安全触发 GUI 更新，彻底避开 StringVar 跨线程延迟
                if is_lr8450:
                    self.root.after(0, lambda: self.device_model.set("LR8450"))
                    self.root.after(0, lambda: self.ch30_var.set(True))
                else:
                    self.root.after(0, lambda: self.device_model.set("LR8401-21"))
                    self.root.after(0, lambda: self.ch30_var.set(False))
                
                # 强制清除以前的所有状态
                self.send_raw_cmd("*CLS")
                
                # 动态兼容性控制：两代仪器均关闭 ATSAve，但仅 LR8450 需要关闭专门的 SAVEWave 和 SAVECalc 功能
                self.send_raw_cmd(":CONFigure:ATSAve OFF")
                if is_lr8450:
                    self.send_raw_cmd(":CONFigure:SAVEWave OFF")
                    self.send_raw_cmd(":CONFigure:SAVECalc OFF")
                
                # 设定电网频率滤波器
                cmd_set, cmd_query = self._get_filter_scpi()
                    
                self.send_raw_cmd(cmd_set)
                time.sleep(0.3) # 给予 300ms 充足的硬件重组等待时间
                
                # 回查滤波器配置
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
                        ans = self.query_raw_cmd(":MODule:STORe? CH1_16") 
                        if ans and ("ON" in ans.upper() or "OFF" in ans.upper()):
                            ch30_detected = True
                else:
                    ans = self.query_raw_cmd(":UNIT:STORe? CH1_16")
                    if ans and ("ON" in ans.upper() or "OFF" in ans.upper()):
                        self.root.after(0, lambda: self.ch30_var.set(True))
                    else:
                        self.root.after(0, lambda: self.ch30_var.set(False))
                
                self.auto_set_shortest_interval(verbose=True)
                
                self.is_connected = True
                self.config_unsynced = True # 连接成功后强制标记为需全同步状态
                self.root.after(0, self._update_ui_connected)
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
        self.root.after(0, self._update_ui_disconnected)
        self.write_log("已释放串口资源。")

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
                        
                    if "channels" in config:
                        channels_data = config["channels"]
                    else:
                        channels_data = config
                        
                    for key, val in channels_data.items():
                        if key in self.channel_vars:
                            unit = int(key.split("-")[1])
                            default_range = "10mV" if unit == 1 else "1V"
                            default_ratio = "-50000" if unit == 1 else "-1"
                            self.channel_vars[key]["comment"].set(val.get("comment", ""))
                            self.channel_vars[key]["range"].set(val.get("range", default_range))
                            self.channel_vars[key]["ratio"].set(val.get("ratio", default_ratio))
                self.write_log("[配置读取] 成功恢复上次运行的配置信息。")
            except Exception:
                pass

    def on_close_save(self):
        self.save_local_config()
        self._disconnect_device()
        self.root.destroy()

    def _reset_gui_val_labels(self):
        for key, vars_dict in self.channel_vars.items():
            row_bg = vars_dict["row_bg"]
            lbl = vars_dict["lbl_val"]
            if lbl:
                lbl.config(text="--", fg="#8e8e93", bg=row_bg, highlightthickness=0)
            
            # 重置平均、最大、最小标签，保持条纹背景
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
        lbl = self.channel_vars[key]["lbl_val"]
        row_bg = self.channel_vars[key]["row_bg"]
        if lbl:
            if np.isnan(val):
                # 出现 OVER 溢出时，实时测量标签框立即变为红底白字超强对比度（扁平设计）
                lbl.config(text="OVER", fg="#ffffff", bg="#ff3b30", highlightthickness=0)
            else:
                is_ma = self.is_ma_channel.get(key, False)
                
                # [iOS 升级] 采用更精细、高雅的 iOS 语义配色
                if val < 0:
                    text_color = "#ff3b30" # iOS 系统红
                elif is_ma:
                    text_color = "#34c759" # iOS 系统绿
                else:
                    text_color = "#007aff" # iOS 系统蓝
                
                # 正常数值状态，恢复本行条纹底色，移除外边框
                if is_ma:
                    lbl.config(text=f"{val:.3f} mA", fg=text_color, bg=row_bg) 
                else:
                    lbl.config(text=f"{val:.4f} V", fg=text_color, bg=row_bg)  

    def _batch_update_gui_vals(self, updates):
        for key, val in updates:
            self._update_gui_val_label(key, val)

    def _update_gui_stat_labels(self, key, avg, max_val, min_val):
        """[iOS 升级] 刷新平均值、最大值、最小值指标显示，完美融入 Row交替底色"""
        is_ma = self.is_ma_channel.get(key, False)
        fmt = ".3f" if is_ma else ".4f"
        unit = " mA" if is_ma else " V"
        row_bg = self.channel_vars[key]["row_bg"]
        
        lbl_avg = self.channel_vars[key]["lbl_avg"]
        lbl_max = self.channel_vars[key]["lbl_max"]
        lbl_min = self.channel_vars[key]["lbl_min"]
        
        # 负数统一标红呈现，正数保持标准 iOS 深色字体 (#1c1c1e)
        if lbl_avg:
            if np.isnan(avg):
                lbl_avg.config(text="--", fg="#8e8e93", bg=row_bg)
            else:
                lbl_avg.config(text=f"{avg:{fmt}}{unit}", fg="#ff3b30" if avg < 0 else "#1c1c1e", bg=row_bg)
                
        if lbl_max:
            if np.isnan(max_val):
                lbl_max.config(text="--", fg="#8e8e93", bg=row_bg)
            else:
                lbl_max.config(text=f"{max_val:{fmt}}{unit}", fg="#ff3b30" if max_val < 0 else "#1c1c1e", bg=row_bg)
                
        if lbl_min:
            if np.isnan(min_val):
                lbl_min.config(text="--", fg="#8e8e93", bg=row_bg)
            else:
                lbl_min.config(text=f"{min_val:{fmt}}{unit}", fg="#ff3b30" if min_val < 0 else "#1c1c1e", bg=row_bg)

    def _batch_update_gui_stats(self, updates):
        """批量线程同步通道统计状态"""
        for key, avg, max_val, min_val in updates:
            self._update_gui_stat_labels(key, avg, max_val, min_val)

    def show_over_alert(self, channel_key, comment):
        """高度定制·防卡死非阻塞式通道 OVER 警报弹窗"""
        parts = channel_key.split("-")
        unit = int(parts[1])
        ch = int(parts[2])
        prefix = "CH1" if unit == 1 else "CH2"
        ch_display = f"{prefix}_{ch}"
        
        msg = f"通道 {ch_display} ({comment}) 发生 OVER 溢出！" if comment else f"通道 {ch_display} 发生 OVER 溢出！"
        
        # 触发物理蜂鸣器提示
        self.root.bell()
        
        # 构建非阻塞 Toplevel
        alert_win = tk.Toplevel(self.root)
        alert_win.title("⚠️ 测量值溢出告警")
        alert_win.geometry("460x180")
        alert_win.configure(bg="#ffffff")
        alert_win.attributes("-topmost", True)
        
        # 精确计算坐标，居中于控制台中央
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
        # 启动时重设已弹窗溢出的通道名单
        self.alerted_over_channels = set()
        self._reset_gui_val_labels()
        
        # 启动时彻底清空累加式极速统计量
        for key in self.channel_keys:
            self.stats_count[key] = 0
            self.stats_sum[key] = 0.0
            self.stats_max[key] = -float('inf')
            self.stats_min[key] = float('inf')
        
        # 通过「转换比」的数值特征扫描并缓存电流类型，避免兼容性差的正则字符匹配
        def check_is_ma(k):
            ratio_text = self.channel_vars[k]["ratio"].get().strip()
            if not ratio_text:
                return False
            try:
                val = float(ratio_text)
                return val not in (1.0, -1.0) # 只有当转换比非 1 且非 -1 时，才判定为电流
            except ValueError:
                return False

        self.is_ma_channel = {
            key: check_is_ma(key)
            for key, vars_dict in self.channel_vars.items()
        }
        
        self.save_local_config()
        threading.Thread(target=self._bg_start_task, daemon=True).start()

    def _bg_start_task(self):
        # 强制清空旧的状态、报错和警告队列
        self.send_raw_cmd("*CLS")
        
        # 动态兼容性物理拦截：强制切断仪器的自动保存与本地存储功能。仅在 LR8450 上下发专用保存关闭指令
        self.send_raw_cmd(":CONFigure:ATSAve OFF")
        if self.device_model.get() == "LR8450":
            self.send_raw_cmd(":CONFigure:SAVEWave OFF")
            self.send_raw_cmd(":CONFigure:SAVECalc OFF")
        
        # 智能差分同步：若配置未发生任何改变，则直接启动采集，降低物理起播等待
        if getattr(self, "config_unsynced", True):
            self.write_log(">>> 检测到本地配置有变动，正在重构写入日置通道矩阵...")
            
            # 多重滤波器下发（LR8450 / LR8401-21 统一使用 :UNIT 级别命令树）
            cmd_set, cmd_query = self._get_filter_scpi()
            self.send_raw_cmd(cmd_set)
            time.sleep(0.3) # 给予 300ms 充足的硬件切换时间
            
            f_res = self.query_raw_cmd(cmd_query).strip()
            f_res = clean_scpi_response(f_res)
            if f_res:
                self.write_log(f">>> 设定滤波器配置为: 50Hz (设备当前读取状态: {f_res} - 已确认验证)")
            else:
                self.write_log(f">>> 设定滤波器配置为: 50Hz (设备当前读取状态: 无法回显/查询超时，请手动在仪器面板确认)")
            
            # 基于 HIOKI 标准命令集重构记录间隔下发机制
            interval_val = self.interval_var.get()
            try:
                if interval_val.endswith("ms"):
                    interval_sec = float(interval_val.replace("ms", "")) / 1000.0
                elif interval_val.endswith("s"):
                    interval_sec = float(interval_val.replace("s", ""))
                else:
                    interval_sec = float(interval_val)
            except ValueError:
                interval_sec = 0.01  # 默认 10ms
                
            self.send_raw_cmd(f":CONFigure:SAMPle {interval_sec}")
            self.write_log(f">>> 设定日置物理硬件记录间隔为: {interval_val} ({interval_sec}s)")
            
            # 针对 LR8450 采用标准的 :MODule 协议，针对 LR8401-21 采用原配的 :UNIT 协议，从根本上杜绝语法错误和命令阻断
            prefix_cmd = ":MODule" if self.device_model.get() == "LR8450" else ":UNIT"
            
            # 统一下发通道配置参数
            for unit in [1, 2]:
                for ch in range(1, 16):
                    key = f"1-{unit}-{ch}"
                    comment = self.channel_vars[key]["comment"].get().strip()
                    range_text = self.channel_vars[key]["range"].get()
                    ratio_text = self.channel_vars[key]["ratio"].get().strip()
                    
                    ch_id = self.get_scpi_ch(unit, ch)
                    
                    if comment:
                        self.send_raw_cmd(f"{prefix_cmd}:INMOde {ch_id},VOLTAGE")
                        scpi_range = get_scpi_range(range_text)
                        self.send_raw_cmd(f"{prefix_cmd}:RANGe {ch_id},{scpi_range}")
                        self.send_raw_cmd(f':COMMent:CH {ch_id},"{comment}"')
                        self.send_raw_cmd(f"{prefix_cmd}:STORe {ch_id},ON")
                        
                        color_idx = (ch - 1) % 24 + 1
                        self.send_raw_cmd(f":DISPlay:DRAWing {ch_id},C{color_idx}")
                        
                        if ratio_text:
                            try:
                                ratio_val = float(ratio_text)
                                self.send_raw_cmd(f":SCALing:SET {ch_id},ENG")
                                self.send_raw_cmd(f":SCALing:KIND {ch_id},RATIO")
                                self.send_raw_cmd(f":SCALing:VOLT {ch_id},{ratio_val}")
                                self.send_raw_cmd(f":SCALing:OFFSet {ch_id},0.0")
                                
                                # 基于数值特征判定，若非 1/-1 则是电流，设置 SCPI 缩放单位为 mA，否则设置 V
                                if ratio_val not in (1.0, -1.0):
                                    self.send_raw_cmd(f':SCALing:UNIT {ch_id},"mA"')
                                else:
                                    self.send_raw_cmd(f':SCALing:UNIT {ch_id},"V"')
                            except ValueError:
                                self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
                        else:
                            self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
                    else:
                        self.send_raw_cmd(f"{prefix_cmd}:STORe {ch_id},OFF")
                        self.send_raw_cmd(f":DISPlay:DRAWing {ch_id},OFF")
                        self.send_raw_cmd(f":SCALing:SET {ch_id},OFF")
            
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
        
        # 在高速测量开始前，临时将串口超时缩短至 0.3s，保障实时流的高速采集
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
        """高速测量值单通道快照截获线程 (容错并行更新版)"""
        self.active_channels = []
        for unit in [1, 2]:
            for ch in range(1, 16):
                key = f"1-{unit}-{ch}"
                comment = self.channel_vars[key]["comment"].get().strip()
                if comment:
                    scpi_ch = self.get_scpi_ch(unit, ch)
                    self.active_channels.append({
                        "key": key,
                        "unit": unit,
                        "ch": ch,
                        "comment": comment,
                        "scpi_ch": scpi_ch,
                        "query_cmd": f":MEMory:VREAl? {scpi_ch}"
                    })
                    
        num_active = len(self.active_channels)
        if num_active == 0:
            return
            
        start_clock = time.time()
        loop_counter = 0 # 统计专用刷新节流计数器
        
        while self.timer_running:
            self.send_raw_cmd(":MEMory:GETReal")
            
            vals = []
            
            for chan in self.active_channels:
                if not self.timer_running:
                    break
                
                res_str = self.query_raw_cmd(chan["query_cmd"])
                res_str = clean_scpi_response(res_str)
                
                val_float = 0.0
                is_over = False
                if res_str:
                    res_lower = res_str.lower()
                    if "over" in res_lower or "nan" in res_lower or "inf" in res_lower or "o.r" in res_lower:
                        is_over = True
                    else:
                        try:
                            val_float = float(res_str)
                            # [阈值优化] 降低判定门槛至 1.0e+9 (10亿)
                            if abs(val_float) >= 1.0e+9:
                                is_over = True
                        except ValueError:
                            pass
                
                if is_over:
                    vals.append(np.nan)
                    
                    # 仅在单次运行中对特定通道进行首次非阻塞强提醒
                    if chan["key"] not in self.alerted_over_channels:
                        self.alerted_over_channels.add(chan["key"])
                        self.write_log(f"[告警] 通道 {chan['scpi_ch']} ({chan['comment']}) 发生 OVER 溢出！")
                        self.root.after(0, self.show_over_alert, chan["key"], chan["comment"])
                else:
                    vals.append(val_float)
                    
                    # 只有在正常接收到有效物理值时，才通过无损增量公式对平均值、最大、最小指标进行极速累加运算
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
                
                # 1. 第一步：以最大极限速率更新当前“实时测量值” (第4列)
                updates = [(chan["key"], vals[idx]) for idx, chan in enumerate(self.active_channels)]
                self.root.after(0, self._batch_update_gui_vals, updates)
                
                # 2. 第二步：采用 [节流防抖机制]，每累计 10 轮测量（约 200~300ms）在主界面集中重绘一次平均值、最大值和最小值标签
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
        
        # 智能静默过滤：抑制掉无报错(0)或因没有插盘产生的 WARN_FL06 消息
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

        # 优化性能：直接读取采集前生成的活跃通道缓存，避免二次遍历 30 次 GUI 变量
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
        
        for unit in [1, 2]:
            for ch in range(1, 16):
                key = f"1-{unit}-{ch}"
                chan_str = f"1-{unit}-{ch}"
                if key in active_map:
                    # 性能恢复：data_arr 接收已切片的 1D 数据
                    data_arr = channel_data_dict[key]
                    
                    data_arr_clean = data_arr[~np.isnan(data_arr)]
                    if len(data_arr_clean) == 0:
                        mean_val = max_val = min_val = 0.0
                    else:
                        mean_val = np.mean(data_arr_clean)
                        max_val = np.max(data_arr_clean)
                        min_val = np.min(data_arr_clean)
                    
                    # 根据转换比扫描后的 is_ma_channel，正确处理统计文本的保留小数位数（避免采用硬字符检测导致的错误精度）
                    if self.is_ma_channel.get(key, False):
                        lines.append(f"{chan_str}\t{mean_val:.3f}\t{max_val:.1f}\t{min_val:.1f}")
                    else:
                        lines.append(f"{chan_str}\t{mean_val:.3f}\t{max_val:.3f}\t{min_val:.3f}")
                
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
        # 采集停止后，恢复串口超时至 1.0s，方便后续的常规配置查询
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