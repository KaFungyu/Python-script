import tkinter as tk
from tkinter import ttk
import serial
import serial.tools.list_ports
import threading
import time
from collections import deque

class TH1941_Smart_Sync_GUI:
    UI_REFRESH_MS = 40
    SAMPLE_TIMEOUT_SEC = 1.2
    MODE_CHECK_INTERVAL_SEC = 2.0
    TARGET_NPLC = "1"
    NPLC_RATE_HZ = {
        0.5: 25.0,
        1.0: 10.0,
        2.0: 5.0,
    }

    def __init__(self, root):
        self.root = root
        self.root.title("同惠 TH1941 智能同步监控面板")
        self.root.geometry("540x460")
        self.root.resizable(False, False)
        
        # 串口及通信控制变量
        self.port = ""
        self.serial_active = False  # 控制后台轮询是否活跃
        
        # 统计数据与线程安全变量：每次仪器返回的有效读数都对应一次同步采样。
        self.readings = []
        self.current_val = None
        self.current_raw = None
        self.current_seq = 0
        self.displayed_seq = 0
        self.last_sample_at = None
        self.sample_intervals = deque(maxlen=25)
        self.measured_rate_hz = None
        self.configured_nplc = None
        self.target_rate_hz = None
        self.sample_method = None
        self.last_sample_elapsed_sec = None
        self.link_limit_hz = None
        self.measurement_key = None
        self.measurement_name = "测量值"
        self.measurement_unit = ""
        self.measurement_func = ""
        
        # 用于监测测量模式变化的本地缓存
        self.last_ui_measurement_key = None
        
        self.lock = threading.Lock()
        self.running = True
        
        # 1. 绘制界面
        self.create_widgets()
        
        # 2. 扫描并初始化串口状态
        self.refresh_ports(startup=True)
        
        # 3. 启动后台智能去重轮询线程（常驻线程，通过信号控制连接/断开）
        print("[DEBUG] 主线程：正在准备启动后台串口轮询线程...")
        self.serial_thread = threading.Thread(target=self.serial_polling_loop, daemon=True)
        self.serial_thread.start()
        print("[DEBUG] 主线程：后台串口轮询线程已启动。")
        
        # 4. 启动 UI 高频更新循环
        self.root.after(self.UI_REFRESH_MS, self.update_ui_loop)
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def create_widgets(self):
        """绘制界面组件"""
        main_frame = ttk.Frame(self.root, padding="15")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # ================= 区域 0: 串口与单位设置栏 =================
        top_bar = ttk.Frame(main_frame)
        top_bar.pack(fill=tk.X, pady=(0, 15))
        
        # 串口选择
        lbl_port = ttk.Label(top_bar, text="串口选择:", font=("Microsoft YaHei", 9))
        lbl_port.pack(side=tk.LEFT, padx=(0, 5))
        
        self.cb_port = ttk.Combobox(top_bar, width=10, state="readonly", font=("Microsoft YaHei", 9))
        self.cb_port.pack(side=tk.LEFT, padx=(0, 5))
        self.cb_port.bind("<<ComboboxSelected>>", self.on_port_selected)
        
        self.btn_refresh = ttk.Button(top_bar, text="刷新", width=6, command=self.refresh_ports)
        self.btn_refresh.pack(side=tk.LEFT, padx=(0, 10))
        
        self.btn_connect = tk.Button(
            top_bar, 
            text="连接", 
            font=("Microsoft YaHei", 9, "bold"), 
            bg="#1a73e8", 
            fg="white", 
            activebackground="#1557b0", 
            activeforeground="white",
            relief="flat", 
            cursor="hand2",
            width=6,
            command=self.toggle_connection
        )
        self.btn_connect.pack(side=tk.LEFT, padx=(0, 20))
        
        # 分割线
        sep = ttk.Separator(top_bar, orient="vertical")
        sep.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 20))
        
        # 单位选择
        lbl_unit = ttk.Label(top_bar, text="显示单位:", font=("Microsoft YaHei", 9))
        lbl_unit.pack(side=tk.LEFT, padx=(0, 5))
        
        self.cb_unit = ttk.Combobox(top_bar, width=8, state="readonly", font=("Microsoft YaHei", 9))
        self.cb_unit.pack(side=tk.LEFT, padx=(0, 5))
        self.cb_unit.bind("<<ComboboxSelected>>", self.on_unit_changed)
        self.cb_unit['values'] = ["自动"]
        self.cb_unit.current(0)

        # ================= 区域 A: 实时值显示 =================
        self.card_frame = tk.LabelFrame(main_frame, text=" 实时测量值 (智能同步) ", font=("Microsoft YaHei", 10, "bold"), fg="#1a73e8", padx=10, pady=10)
        self.card_frame.pack(fill=tk.X, pady=(0, 15))
        
        # 实时值水平布局容器
        realtime_container = ttk.Frame(self.card_frame)
        realtime_container.pack()
        
        self.lbl_realtime = tk.Label(
            realtime_container, 
            text="连接万用表...", 
            font=("Consolas", 32, "bold"), 
            fg="#5f6368",
            cursor="hand2"  # 悬停手形指针提示可复制
        )
        self.lbl_realtime.pack(side=tk.LEFT)
        self.lbl_realtime.bind("<Button-1>", lambda e: self.copy_realtime())

        # 实时值旁边的独立复制按钮
        self.btn_copy_realtime = tk.Button(
            realtime_container,
            text="📋 复制",
            font=("Microsoft YaHei", 9),
            bg="#f1f3f4",
            fg="#1a73e8",
            activebackground="#e8f0fe",
            activeforeground="#1a73e8",
            relief="flat",
            cursor="hand2",
            command=self.copy_realtime,
            padx=6,
            pady=2
        )
        self.btn_copy_realtime.pack(side=tk.LEFT, padx=(15, 0))

        self.lbl_realtime_meta = tk.Label(self.card_frame, text="等待第一条采样...", font=("Microsoft YaHei", 9), fg="#5f6368")
        self.lbl_realtime_meta.pack(pady=(4, 0))

        # ================= 区域 B: 统计分析网格 =================
        stats_frame = tk.LabelFrame(main_frame, text=" 统计分析面板 (Statistics) ", font=("Microsoft YaHei", 10, "bold"), fg="#1a73e8", padx=15, pady=10)
        stats_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 15))

        for i in range(4):
            stats_frame.columnconfigure(i, weight=1)

        headers = ["最大值 (Max)", "最小值 (Min)", "平均值 (Avg)", "物理采样次数"]
        for i, h in enumerate(headers):
            lbl = tk.Label(stats_frame, text=h, font=("Microsoft YaHei", 9), fg="#5f6368")
            lbl.grid(row=0, column=i, pady=(0, 5), sticky="ew")

        # 数据标签（绑定点击事件和手形指针）
        self.lbl_max = tk.Label(stats_frame, text="--", font=("Consolas", 14, "bold"), fg="#d93025", cursor="hand2")
        self.lbl_max.grid(row=1, column=0, sticky="ew")
        self.lbl_max.bind("<Button-1>", lambda e: self.copy_max())

        self.lbl_min = tk.Label(stats_frame, text="--", font=("Consolas", 14, "bold"), fg="#1a73e8", cursor="hand2")
        self.lbl_min.grid(row=1, column=1, sticky="ew")
        self.lbl_min.bind("<Button-1>", lambda e: self.copy_min())

        self.lbl_avg = tk.Label(stats_frame, text="--", font=("Consolas", 14, "bold"), fg="#202124", cursor="hand2")
        self.lbl_avg.grid(row=1, column=2, sticky="ew")
        self.lbl_avg.bind("<Button-1>", lambda e: self.copy_avg())

        self.lbl_count = tk.Label(stats_frame, text="0 次", font=("Consolas", 14, "bold"), fg="#5f6368")
        self.lbl_count.grid(row=1, column=3, sticky="ew")

        # 新增行：独立复制按钮
        self.btn_copy_max = tk.Button(
            stats_frame, 
            text="📋 复制", 
            font=("Microsoft YaHei", 8), 
            bg="#f1f3f4", 
            fg="#d93025", 
            activebackground="#fce8e6", 
            activeforeground="#d93025",
            relief="flat", 
            cursor="hand2", 
            command=self.copy_max,
            pady=1
        )
        self.btn_copy_max.grid(row=2, column=0, pady=(4, 0), padx=10, sticky="ew")

        self.btn_copy_min = tk.Button(
            stats_frame, 
            text="📋 复制", 
            font=("Microsoft YaHei", 8), 
            bg="#f1f3f4", 
            fg="#1a73e8", 
            activebackground="#e8f0fe", 
            activeforeground="#1a73e8",
            relief="flat", 
            cursor="hand2", 
            command=self.copy_min,
            pady=1
        )
        self.btn_copy_min.grid(row=2, column=1, pady=(4, 0), padx=10, sticky="ew")

        self.btn_copy_avg = tk.Button(
            stats_frame, 
            text="📋 复制", 
            font=("Microsoft YaHei", 8), 
            bg="#f1f3f4", 
            fg="#202124", 
            activebackground="#f1f3f4", 
            activeforeground="#202124",
            relief="flat", 
            cursor="hand2", 
            command=self.copy_avg,
            pady=1
        )
        self.btn_copy_avg.grid(row=2, column=2, pady=(4, 0), padx=10, sticky="ew")
        
        # 物理采样次数不需要复制按钮，放置一个占位空标签
        lbl_count_placeholder = tk.Label(stats_frame, text="", font=("Microsoft YaHei", 8))
        lbl_count_placeholder.grid(row=2, column=3, pady=(4, 0))

        # ================= 区域 C: 控制区 =================
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X)

        self.btn_clear = tk.Button(
            btn_frame, 
            text="清除数据 (重新统计)", 
            font=("Microsoft YaHei", 10, "bold"), 
            bg="#d93025", 
            fg="white", 
            activebackground="#b02218", 
            activeforeground="white",
            relief="flat", 
            cursor="hand2", 
            command=self.clear_data, 
            padx=12, 
            pady=6
        )
        self.btn_clear.pack(side=tk.RIGHT)

        # 新增一键复制所有按钮
        self.btn_copy_all = tk.Button(
            btn_frame, 
            text="一键复制所有统计", 
            font=("Microsoft YaHei", 10, "bold"), 
            bg="#1a73e8", 
            fg="white", 
            activebackground="#1557b0", 
            activeforeground="white",
            relief="flat", 
            cursor="hand2", 
            command=self.copy_all_values, 
            padx=12, 
            pady=6
        )
        self.btn_copy_all.pack(side=tk.RIGHT, padx=(0, 10))

        # ================= 区域 D: 底层操作提示状态栏 =================
        self.lbl_status = tk.Label(
            self.root, 
            text="提示：点击任意测量值或 📋 按钮可单独复制，支持一键复制所有数据", 
            font=("Microsoft YaHei", 8), 
            fg="#5f6368", 
            anchor="w", 
            padx=10, 
            pady=3,
            bg="#f1f3f4"
        )
        self.lbl_status.pack(side=tk.BOTTOM, fill=tk.X)

    # ================= 串口与连接控制逻辑 =================
    def refresh_ports(self, startup=False):
        """扫描可用串口并更新下拉菜单列表"""
        ports = [p.device for p in serial.tools.list_ports.comports()]
        print(f"[DEBUG] 扫描串口结果: {ports}")
        
        if ports:
            self.cb_port['values'] = ports
            # 启动或当前选择为空时，默认选中首个串口
            current_selection = self.cb_port.get()
            if not current_selection or current_selection not in ports:
                self.cb_port.current(0)
                self.port = self.cb_port.get()
            else:
                self.port = current_selection
                
            if startup:
                self.serial_active = True
                self.update_connect_button_style(connected=True)
                self.show_status(f"已自动选择并启动串口: {self.port}")
        else:
            self.cb_port['values'] = ["无可用串口"]
            self.cb_port.current(0)
            self.port = ""
            self.serial_active = False
            self.update_connect_button_style(connected=False)
            self.show_status("未检测到可用串口，请插入设备并点击'刷新'")

    def on_port_selected(self, event=None):
        """用户手动从下拉菜单切换串口"""
        new_port = self.cb_port.get()
        if new_port == "无可用串口" or new_port == self.port:
            return
            
        print(f"[DEBUG] 用户切换串口为: {new_port}")
        with self.lock:
            self.port = new_port
            self.reset_samples_locked()
            
        # 如果当前是开启状态，则触发后台线程自动断开旧串口、连接新串口
        if self.serial_active:
            self.show_status(f"正在切换到串口: {new_port} ...")
        else:
            self.show_status(f"已选择串口: {new_port}，请点击'连接'开启")

    def toggle_connection(self):
        """连接 / 断开 按钮点击逻辑"""
        current_port = self.cb_port.get()
        if not current_port or current_port == "无可用串口":
            self.show_status("无有效串口，无法连接！")
            return

        if self.serial_active:
            # 执行断开
            self.serial_active = False
            self.update_connect_button_style(connected=False)
            self.show_status("串口通信已断开")
            with self.lock:
                self.current_val = "通信已暂停"
                self.current_seq += 1
        else:
            # 执行连接
            with self.lock:
                self.port = current_port
                self.reset_samples_locked()
            self.serial_active = True
            self.update_connect_button_style(connected=True)
            self.show_status(f"正在连接串口 {current_port} ...")

    def update_connect_button_style(self, connected):
        """动态更新连接按钮外观"""
        if connected:
            self.btn_connect.config(
                text="断开", 
                bg="#d93025", 
                activebackground="#b02218"
            )
        else:
            self.btn_connect.config(
                text="连接", 
                bg="#1a73e8", 
                activebackground="#1557b0"
            )

    # ================= 复制功能逻辑 =================
    def show_status(self, message, is_error=False):
        """更新底部状态栏信息并自动在3秒后重置为默认提示"""
        color = "#d93025" if is_error else "#1a73e8"
        self.lbl_status.config(text=message, fg=color)
        self.root.after(3500, lambda: self.reset_status_hint(message))

    def reset_status_hint(self, old_msg):
        """如果状态栏信息未被其他操作覆盖，恢复默认提示"""
        if self.lbl_status.cget("text") == old_msg:
            self.lbl_status.config(text="提示：点击任意测量值或 📋 按钮可单独复制，支持一键复制所有数据", fg="#5f6368")

    def copy_to_clipboard(self, text, label_name):
        """统一写入剪贴板函数"""
        if not text or "连接" in text or "错误" in text or "暂不支持" in text or "暂停" in text:
            self.show_status(f"复制失败：{label_name}当前无有效数据", is_error=True)
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.show_status(f"已成功复制{label_name}: {text.replace(chr(9), '  ')}")
        except Exception as e:
            self.show_status(f"写入剪贴板失败: {e}", is_error=True)

    def copy_realtime(self):
        """复制实时值（不含单位）"""
        with self.lock:
            curr = self.current_val
            base_unit = self.measurement_unit
        if curr is not None and isinstance(curr, float):
            formatted = self.format_measurement_value(curr, base_unit, include_unit=False)
            self.copy_to_clipboard(formatted, "实时测量值")
        else:
            self.show_status("复制失败：当前无有效测量值", is_error=True)

    def copy_max(self):
        """复制最大值（不含单位）"""
        with self.lock:
            if self.readings:
                val = max(self.readings)
                base_unit = self.measurement_unit
            else:
                val = None
        if val is not None:
            formatted = self.format_measurement_value(val, base_unit, include_unit=False)
            self.copy_to_clipboard(formatted, "最大值")
        else:
            self.show_status("复制失败：暂无统计数据", is_error=True)

    def copy_min(self):
        """复制最小值（不含单位）"""
        with self.lock:
            if self.readings:
                val = min(self.readings)
                base_unit = self.measurement_unit
            else:
                val = None
        if val is not None:
            formatted = self.format_measurement_value(val, base_unit, include_unit=False)
            self.copy_to_clipboard(formatted, "最小值")
        else:
            self.show_status("复制失败：暂无统计数据", is_error=True)

    def copy_avg(self):
        """复制平均值（不含单位）"""
        with self.lock:
            if self.readings:
                val = sum(self.readings) / len(self.readings)
                base_unit = self.measurement_unit
            else:
                val = None
        if val is not None:
            formatted = self.format_measurement_value(val, base_unit, include_unit=False)
            self.copy_to_clipboard(formatted, "平均值")
        else:
            self.show_status("复制失败：暂无统计数据", is_error=True)

    def copy_all_values(self):
        """一键复制所有统计数据（仅包含无单位的 MAX、MIN、AVG，制表符横向分隔）"""
        with self.lock:
            count = len(self.readings)
            curr = self.current_val
            base_unit = self.measurement_unit
            if count > 0:
                max_v = max(self.readings)
                min_v = min(self.readings)
                avg_v = sum(self.readings) / count
            else:
                max_v = min_v = avg_v = None

        if count == 0 or curr is None or not isinstance(curr, float):
            self.show_status("复制失败：当前无有效测量或统计数据", is_error=True)
            return

        max_str = self.format_measurement_value(max_v, base_unit, include_unit=False)
        min_str = self.format_measurement_value(min_v, base_unit, include_unit=False)
        avg_str = self.format_measurement_value(avg_v, base_unit, include_unit=False)

        # 仅生成横向排列（制表符分隔）的三个数值，粘贴到 Excel 时会自动分在三个相邻列中
        text = f"{max_str}\t{min_str}\t{avg_str}"
        self.copy_to_clipboard(text, "MAX/MIN/AVG")

    # ================= 单位智能选择与格式化逻辑 =================
    def on_unit_changed(self, event=None):
        """显示单位下拉菜单切换回调"""
        print(f"[DEBUG] 主线程：用户切换显示单位为: {self.cb_unit.get()}")
        # 强制触发一次 UI 刷新，使用户立即看到单位换算效果
        self.update_ui_loop(force_redraw=True)

    def format_measurement_value(self, value, base_unit, include_unit=True):
        """核心换算逻辑：强制保证所有换算单位下显示小数点后4位 (可配置是否包含单位后缀)"""
        if value is None:
            return "--"
        
        # 获取用户在下拉菜单中主动选择的单位，如果界面未完全初始化默认"自动"
        target_unit = "自动"
        if hasattr(self, 'cb_unit') and self.cb_unit:
            target_unit = self.cb_unit.get() or "自动"

        if target_unit == "自动":
            # 原始智能自适应单位量程逻辑 (依然保持4位小数)
            abs_val = abs(value)
            if base_unit == "V":
                if abs_val >= 1:
                    return f"{value:.4f}" if not include_unit else f"{value:.4f} V"
                else:
                    return f"{value * 1000:.4f}" if not include_unit else f"{value * 1000:.4f} mV"
            elif base_unit == "A":
                if abs_val >= 1:
                    return f"{value:.4f}" if not include_unit else f"{value:.4f} A"
                else:
                    return f"{value * 1000:.4f}" if not include_unit else f"{value * 1000:.4f} mA"
            elif base_unit == "Ω":
                if abs_val >= 1_000_000:
                    return f"{value / 1_000_000:.4f}" if not include_unit else f"{value / 1_000_000:.4f} MΩ"
                elif abs_val >= 1000:
                    return f"{value / 1000:.4f}" if not include_unit else f"{value / 1000:.4f} kΩ"
                elif abs_val >= 1:
                    return f"{value:.4f}" if not include_unit else f"{value:.4f} Ω"
                else:
                    return f"{value * 1000:.4f}" if not include_unit else f"{value * 1000:.4f} mΩ"
            else:
                return f"{value:.4f}" if not include_unit else f"{value:.4f} {base_unit}".rstrip()
        else:
            # 用户指定了特定换算单位，强制在指定单位下显示 4 位小数
            if base_unit == "V":
                if target_unit == "V":
                    return f"{value:.4f}" if not include_unit else f"{value:.4f} V"
                elif target_unit == "mV":
                    return f"{value * 1000.0:.4f}" if not include_unit else f"{value * 1000.0:.4f} mV"
                elif target_unit == "uV":
                    return f"{value * 1000000.0:.4f}" if not include_unit else f"{value * 1000000.0:.4f} uV"
            elif base_unit == "A":
                if target_unit == "A":
                    return f"{value:.4f}" if not include_unit else f"{value:.4f} A"
                elif target_unit == "mA":
                    return f"{value * 1000.0:.4f}" if not include_unit else f"{value * 1000.0:.4f} mA"
                elif target_unit == "uA":
                    return f"{value * 1000000.0:.4f}" if not include_unit else f"{value * 1000000.0:.4f} uA"
            elif base_unit == "Ω":
                if target_unit == "MΩ":
                    return f"{value / 1000000.0:.4f}" if not include_unit else f"{value / 1000000.0:.4f} MΩ"
                elif target_unit == "kΩ":
                    return f"{value / 1000.0:.4f}" if not include_unit else f"{value / 1000.0:.4f} kΩ"
                elif target_unit == "Ω":
                    return f"{value:.4f}" if not include_unit else f"{value:.4f} Ω"
                elif target_unit == "mΩ":
                    return f"{value * 1000.0:.4f}" if not include_unit else f"{value * 1000.0:.4f} mΩ"
            
            # 后备容错
            return f"{value:.4f}" if not include_unit else f"{value:.4f} {target_unit}"

    # ================= 后台通信线程及命令交互 =================
    def write_command(self, ser, command):
        ser.write((command + "\n").encode("ascii"))
        ser.flush()

    def query_command(self, ser, command, timeout=None):
        old_timeout = ser.timeout
        if timeout is not None:
            ser.timeout = timeout

        try:
            ser.reset_input_buffer()
            self.write_command(ser, command)
            normalized_command = command.strip().upper()
            while self.running and self.serial_active:
                line = ser.readline().decode('ascii', errors='ignore').strip()
                if not line:
                    return ""
                if line.upper() == normalized_command:
                    continue
                return line
            return ""
        finally:
            ser.timeout = old_timeout

    def query_numeric(self, ser, command, timeout=None):
        t_start = time.perf_counter()
        res = self.query_command(ser, command, timeout=timeout)
        elapsed = time.perf_counter() - t_start
        if not res:
            return None, "", elapsed

        try:
            return float(res), res, elapsed
        except ValueError:
            print(f"[DEBUG] 警告：{command} 返回非数字: {repr(res)}")
            return None, res, elapsed

    def normalize_function_name(self, func_res):
        return func_res.replace('"', '').replace("'", "").replace(" ", "").upper()

    def get_measurement_config(self, func_res):
        func = self.normalize_function_name(func_res)
        if "VOLT" in func and "DC" in func and "AC" not in func:
            return {
                "key": "DCV",
                "name": "DC电压",
                "unit": "V",
                "nplc_command": ":VOLTage:DC:NPLCycles",
                "func": func,
            }
        if "CURR" in func and "DC" in func and "AC" not in func:
            return {
                "key": "DCI",
                "name": "DC电流",
                "unit": "A",
                "nplc_command": ":CURRent:DC:NPLCycles",
                "func": func,
            }
        if "FRES" in func:
            return {
                "key": "FRES",
                "name": "四线电阻",
                "unit": "Ω",
                "nplc_command": ":RESistance:NPLCycles",
                "func": func,
            }
        if "RES" in func:
            return {
                "key": "RES",
                "name": "电阻",
                "unit": "Ω",
                "nplc_command": ":RESistance:NPLCycles",
                "func": func,
            }
        return None

    def reset_samples_locked(self):
        self.readings.clear()
        self.current_val = None
        self.current_raw = None
        self.current_seq = 0
        self.last_sample_at = None
        self.sample_intervals.clear()
        self.measured_rate_hz = None
        self.sample_method = None
        self.last_sample_elapsed_sec = None
        self.link_limit_hz = None

    def configure_current_function(self, ser, force=False):
        func_res = self.query_command(ser, ":FUNC?", timeout=1.0)
        print(f"[DEBUG] 后台线程：当前测量模式: {repr(func_res)}")

        config = self.get_measurement_config(func_res)
        if not config:
            with self.lock:
                self.measurement_key = None
                self.measurement_name = "不支持档位"
                self.measurement_unit = ""
                self.measurement_func = self.normalize_function_name(func_res)
                self.reset_samples_locked()
                self.current_val = f"当前档位暂不支持: {func_res or '未知'}"
                self.current_seq += 1
            return None

        with self.lock:
            mode_changed = config["key"] != self.measurement_key
            self.measurement_key = config["key"]
            self.measurement_name = config["name"]
            self.measurement_unit = config["unit"]
            self.measurement_func = config["func"]
            if mode_changed:
                self.reset_samples_locked()
            current_target_rate_hz = self.target_rate_hz

        if mode_changed:
            print(f"[DEBUG] 后台线程：识别到 {config['name']} 档，单位 {config['unit']}，统计已重置")
        elif not force:
            return current_target_rate_hz or self.NPLC_RATE_HZ[1.0]

        self.write_command(ser, ":TRIGger:SOURce IMMediate")
        self.write_command(ser, ":HOLD:STATe OFF")
        self.write_command(ser, f"{config['nplc_command']} {self.TARGET_NPLC}")
        time.sleep(0.2)
        nplc_res = self.query_command(ser, f"{config['nplc_command']}?", timeout=1.0)
        target_rate_hz = self.update_target_rate(nplc_res)
        print(
            f"[DEBUG] 后台线程：{config['name']} NPLC 配置为 "
            f"{repr(nplc_res or self.TARGET_NPLC)}，目标同步速率 {target_rate_hz:.1f} 次/秒"
        )
        return target_rate_hz

    def parse_nplc(self, raw):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def rate_for_nplc(self, nplc):
        if nplc is None:
            return None

        closest = min(self.NPLC_RATE_HZ, key=lambda key: abs(key - nplc))
        if abs(closest - nplc) <= 0.05:
            return self.NPLC_RATE_HZ[closest]
        return None

    def update_target_rate(self, nplc_raw):
        nplc = self.parse_nplc(nplc_raw)
        rate_hz = self.rate_for_nplc(nplc) or self.NPLC_RATE_HZ[0.5]
        with self.lock:
            self.configured_nplc = nplc_raw or self.TARGET_NPLC
            self.target_rate_hz = rate_hz
        return rate_hz

    def read_measurement_once(self, ser):
        val, raw, elapsed = self.query_numeric(ser, ":FETCh?", timeout=self.SAMPLE_TIMEOUT_SEC)
        if val is not None:
            if self.sample_method != "FETCH":
                self.sample_method = "FETCH"
            return val, raw, elapsed, "FETCH"
        return None, "", 0.0, None

    def record_measurement_sample(self, val, raw, elapsed_sec):
        now = time.perf_counter()
        with self.lock:
            if self.last_sample_at is not None:
                interval = now - self.last_sample_at
                if interval > 0:
                    self.sample_intervals.append(interval)
            self.last_sample_at = now

            if self.sample_intervals:
                avg_interval = sum(self.sample_intervals) / len(self.sample_intervals)
                self.measured_rate_hz = 1.0 / avg_interval if avg_interval > 0 else None
            else:
                self.measured_rate_hz = None

            self.current_seq += 1
            self.current_val = val
            self.current_raw = raw
            self.last_sample_elapsed_sec = elapsed_sec
            self.link_limit_hz = 1.0 / elapsed_sec if elapsed_sec > 0 else None
            self.readings.append(val)

    def serial_polling_loop(self):
        """后台智能去重及自适应重连轮询逻辑"""
        print("[DEBUG] 后台线程：进入 serial_polling_loop 内部")
        ser = None
        while self.running:
            # 如果通信被用户断开，后台挂起并降低 CPU 占用率
            if not self.serial_active:
                time.sleep(0.1)
                continue

            with self.lock:
                port_to_open = self.port

            if not port_to_open or port_to_open == "无可用串口":
                time.sleep(0.5)
                continue

            try:
                print(f"[DEBUG] 后台线程：准备尝试开启串口 {port_to_open} ...")
                ser = serial.Serial(
                    port=port_to_open,
                    baudrate=9600,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=self.SAMPLE_TIMEOUT_SEC
                )
                print(f"[DEBUG] 后台线程：串口 {port_to_open} 打开成功！")
                
                # 查询当前测量模式
                ser.reset_input_buffer()
                target_rate_hz = self.configure_current_function(ser, force=True)
                if target_rate_hz is None:
                    print("[DEBUG] 后台线程：当前档位不支持同步采样，等待用户切换到 DCV/Ω/DCI")
                ser.reset_input_buffer()
                
                print("[DEBUG] 后台线程：完全就绪！进入同步采样循环...")
                next_sample_at = time.perf_counter()
                next_mode_check_at = time.perf_counter() + self.MODE_CHECK_INTERVAL_SEC
                
                while self.running and self.serial_active:
                    # 动态监视：如果用户在主界面上切了串口，则跳出内层循环重新打开新串口
                    with self.lock:
                        if self.port != port_to_open:
                            print("[DEBUG] 后台线程：检测到切换了新串口目标，正在重置通信管道...")
                            break

                    if time.perf_counter() >= next_mode_check_at:
                        target_rate_hz = self.configure_current_function(ser)
                        next_mode_check_at = time.perf_counter() + self.MODE_CHECK_INTERVAL_SEC
                        if target_rate_hz is None:
                            time.sleep(0.5)
                            continue

                    with self.lock:
                        mode_supported = self.measurement_key is not None
                    if not mode_supported:
                        time.sleep(0.5)
                        continue

                    wait_sec = next_sample_at - time.perf_counter()
                    if wait_sec > 0:
                        time.sleep(wait_sec)

                    val, raw, elapsed, method = self.read_measurement_once(ser)
                    if val is not None:
                        self.record_measurement_sample(val, raw, elapsed)
                        print(f"[DEBUG] 采样：方式 {method} | 耗时: {elapsed:.3f}s | 收到原始数据: {repr(raw)}")
                    else:
                        print("[DEBUG] 警告：所有采样方式本轮都未返回有效读数")

                    with self.lock:
                        target_rate_hz = self.target_rate_hz or self.NPLC_RATE_HZ[0.5]
                    next_sample_at = max(next_sample_at + 1.0 / target_rate_hz, time.perf_counter())
                    
            except Exception as e:
                print(f"[DEBUG] 严重错误：串口后台发生异常: {e}")
                with self.lock:
                    self.current_val = f"连接错误: {e}"
                    self.current_seq += 1
                time.sleep(1.5)  # 发生错误后等待 1.5 秒尝试自动恢复连接
            finally:
                if ser and ser.is_open:
                    try:
                        self.write_command(ser, ":TRIGger:SOURce IMMediate")
                    except Exception:
                        pass
                    ser.close()
                    print(f"[DEBUG] 后台线程：串口 {port_to_open} 已安全释放")
                ser = None

    # ================= UI 刷新主循环逻辑 =================
    def update_ui_loop(self, force_redraw=False):
        """主线程 UI 刷新循环（带渲染容错保护）"""
        if not self.running:
            return

        try:
            with self.lock:
                count = len(self.readings)
                curr = self.current_val
                curr_seq = self.current_seq
                measured_rate_hz = self.measured_rate_hz
                configured_nplc = self.configured_nplc
                target_rate_hz = self.target_rate_hz
                sample_method = self.sample_method
                last_sample_elapsed_sec = self.last_sample_elapsed_sec
                link_limit_hz = self.link_limit_hz
                measurement_name = self.measurement_name
                measurement_unit = self.measurement_unit
                measurement_func = self.measurement_func
                measurement_key = self.measurement_key
                
                if count > 0:
                    max_v = max(self.readings)
                    min_v = min(self.readings)
                    avg_v = sum(self.readings) / count
                else:
                    max_v = min_v = avg_v = None

            # 1. 动态自适应单位下拉选择列表：如果检测到档位发生切换，自动更换单位选项
            if measurement_key != self.last_ui_measurement_key:
                self.last_ui_measurement_key = measurement_key
                if measurement_key == "DCV":
                    opts = ["自动", "V", "mV", "uV"]
                elif measurement_key == "DCI":
                    opts = ["自动", "A", "mA", "uA"]
                elif measurement_key in ["RES", "FRES"]:
                    opts = ["自动", "MΩ", "kΩ", "Ω", "mΩ"]
                else:
                    opts = ["自动"]
                self.cb_unit['values'] = opts
                self.cb_unit.current(0)  # 默认回滚到智能"自动"状态

            self.card_frame.config(text=f" 实时{measurement_name} (智能同步) ")

            # 2. 实时值秒显与渲染
            if curr is not None:
                if isinstance(curr, float):
                    if curr_seq != self.displayed_seq or force_redraw:
                        self.lbl_realtime.config(
                            text=self.format_measurement_value(curr, measurement_unit),
                            fg="#0f9d58",
                            font=("Consolas", 32, "bold")
                        )
                        rate_text = f"{measured_rate_hz:.1f} 次/秒" if measured_rate_hz else "计算中"
                        target_text = f"{target_rate_hz:.1f} 次/秒" if target_rate_hz else "--"
                        link_text = f"{link_limit_hz:.1f} 次/秒" if link_limit_hz else "--"
                        elapsed_text = f"{last_sample_elapsed_sec:.3f}s" if last_sample_elapsed_sec is not None else "--"
                        nplc_text = configured_nplc or "--"
                        method_text = sample_method or "--"
                        self.lbl_realtime_meta.config(
                            text=f"{measurement_func} | 采样 #{curr_seq} | 实测 {rate_text} | 链路 {link_text} | {method_text} {elapsed_text} | NPLC {nplc_text}",
                            fg="#5f6368"
                        )
                        self.displayed_seq = curr_seq
                else:
                    # 报错信息显示
                    self.lbl_realtime.config(text=str(curr), fg="#d93025", font=("Microsoft YaHei", 12, "bold"))
                    self.lbl_realtime_meta.config(text="采样已停止", fg="#d93025")
            else:
                if self.serial_active:
                    self.lbl_realtime.config(text="等待数据...", fg="#5f6368")
                else:
                    self.lbl_realtime.config(text="未连接串口", fg="#5f6368")
                self.lbl_realtime_meta.config(text="等待第一条采样...", fg="#5f6368")

            # 3. 统计数据刷新
            if count > 0:
                self.lbl_max.config(text=self.format_measurement_value(max_v, measurement_unit))
                self.lbl_min.config(text=self.format_measurement_value(min_v, measurement_unit))
                self.lbl_avg.config(text=self.format_measurement_value(avg_v, measurement_unit))
                self.lbl_count.config(text=f"{count} 次")
            else:
                # 即使没有数据，也要使用用户选择的单位或者自动基本单位显示占位
                empty_text = f"-- {self.cb_unit.get() if self.cb_unit.get() != '自动' else measurement_unit}".rstrip()
                self.lbl_max.config(text=empty_text)
                self.lbl_min.config(text=empty_text)
                self.lbl_avg.config(text=empty_text)
                self.lbl_count.config(text="0 次")
                
        except Exception as ui_err:
            print(f"[DEBUG] 严重错误：UI刷新主线程发生异常: {ui_err}")
            
        finally:
            # 无论发生何种错误，强制继续调度下一次更新，确保程序永不挂死
            self.root.after(self.UI_REFRESH_MS, self.update_ui_loop)

    def clear_data(self):
        """一键清空"""
        print("[DEBUG] 主线程：用户点击了‘清除数据’按钮")
        with self.lock:
            self.reset_samples_locked()
        self.displayed_seq = 0
        self.lbl_realtime.config(text="数据已清空...", fg="#5f6368")
        self.lbl_realtime_meta.config(text="等待第一条采样...", fg="#5f6368")
        self.show_status("统计数据已成功清零")

    def on_closing(self):
        print("[DEBUG] 主线程：正在关闭窗口，释放资源...")
        self.running = False
        self.root.destroy()


# ================= 启动程序 =================
if __name__ == "__main__":
    root = tk.Tk()
    
    # 强制使 Vista / Clam 扁平风格提升整体 UI 档次
    style = ttk.Style()
    style.theme_use('vista' if 'vista' in style.theme_names() else 'clam')
    
    app = TH1941_Smart_Sync_GUI(root)
    root.mainloop()