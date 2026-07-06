import os
import re
import io
import tkinter as tk
from collections import Counter, defaultdict
from tkinter import ttk, messagebox
from datetime import datetime
from queue import Queue
import concurrent.futures

MAX_WORKERS = min(8, os.cpu_count() or 4)
UI_UPDATE_INTERVAL = 100

directories = [
    r"D:\SecureCRT_log\Serial-COM3",
    # r"\\10.28.37.197\share\DDR_TEST\BZ409-26\.Amlogic_DDR_Test",
    # r"\\10.28.37.197\share\DDR_TEST\BZ409-04\.Amlogic_DDR_Test",
    # r"\\10.28.37.197\share\DDR_TEST\BZ409-02\.Amlogic_DDR_Test",
]

keywords = ["u-boot","linux","ID:"]

timestamp_pattern = re.compile(r'(\d{4}[-/]\d{2}[-/]\d{2}[_ ]\d{2}:\d{2}:\d{2})')

stats_data = []
log_contents = []
results_dict = {}
progress_var = None
progress_label = None
root = None
progress_window = None
main_window = None
total_files = 0
processed_files = 0

def parse_timestamp(timestamp_str):
    try:
        return datetime.strptime(timestamp_str, "%Y/%m/%d_%H:%M:%S" if '/' in timestamp_str else "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

def process_log_file(file_path, result_queue):
    keyword_counter = Counter()
    matching_lines = []
    first_timestamp = None
    last_timestamp = None

    run_time = "N/A"
    file_name = os.path.basename(file_path)
    
    keywords_lower = [k.lower() for k in keywords]
    ts_search = timestamp_pattern.search
    
    try:
        with open(file_path, "r", encoding='utf-8', errors='ignore') as f:
            for line in f:
                # 1. 检索关键字
                line_lower = line.lower()
                for keyword in keywords_lower:
                    if keyword in line_lower:
                        keyword_counter[keyword] += 1
                        matching_lines.append((line.rstrip(), keyword))
                        break
                
                # 2. 提取时间戳
                if ':' in line:
                    match = ts_search(line)
                    if match:
                        ts = match.group(1)
                        if not first_timestamp:
                            first_timestamp = ts
                        last_timestamp = ts
    except Exception as e:
        print(f"处理文件出错 {file_path}: {str(e)}")
        result_queue.put((file_path, None))
        return
    
    if first_timestamp and last_timestamp:
        try:
            start_time = parse_timestamp(first_timestamp)
            end_time = parse_timestamp(last_timestamp)
            if start_time and end_time:
                run_time = f"{(end_time - start_time).total_seconds() / 3600:.2f}h"
        except Exception:
            run_time = "Error"
    
    result = (keyword_counter, matching_lines, run_time, file_name)
    result_queue.put((file_path, result))

def update_progress():
    global processed_files, progress_var, progress_label, progress_window
    
    try:
        if progress_var and progress_label and progress_window and hasattr(progress_window, 'winfo_exists') and progress_window.winfo_exists():
            progress_var.set(processed_files)
            percentage = int((processed_files / max(total_files, 1)) * 100)
            progress_label.config(text=f"⏳ 处理进度: {processed_files}/{total_files} ({percentage}%)")
            progress_window.update_idletasks()
    except (tk.TclError, Exception):
        pass

def show_progress_window():
    global progress_var, progress_label, progress_window, total_files, main_window
    
    progress_window = None
    progress_var = None
    progress_label = None
    
    if not main_window or not hasattr(main_window, 'winfo_exists') or not main_window.winfo_exists():
        main_window = tk.Tk()
        main_window.withdraw()
    
    try:
        progress_window = tk.Toplevel(main_window)
        progress_window.title("⏳ 处理进度")
        progress_window.geometry("400x150")
        progress_window.resizable(False, False)
        
        x = (progress_window.winfo_screenwidth() - 400) // 2
        y = (progress_window.winfo_screenheight() - 150) // 2
        progress_window.geometry(f"400x150+{x}+{y}")
        
        frame = ttk.Frame(progress_window, padding="20 20 20 20")
        frame.pack(fill='both', expand=True)
        
        progress_label = ttk.Label(frame, text=f"⏳ 处理进度: 0/{total_files} (0%)")
        progress_label.pack(pady=(0, 10))
        
        progress_var = tk.IntVar(value=0)
        progress_bar = ttk.Progressbar(frame, orient="horizontal", length=360, mode="determinate", 
                                     maximum=total_files, variable=progress_var)
        progress_bar.pack(fill='x', pady=10)
        
        progress_window.attributes('-topmost', True)
        
        def on_closing():
            global processed_files, progress_window
            processed_files = total_files
            try:
                if progress_window and hasattr(progress_window, 'winfo_exists') and progress_window.winfo_exists():
                    progress_window.destroy()
                    progress_window = None
            except tk.TclError:
                pass
        
        progress_window.protocol("WM_DELETE_WINDOW", on_closing)
        
        try:
            if progress_window and hasattr(progress_window, 'winfo_exists') and progress_window.winfo_exists():
                progress_window.update()
        except tk.TclError:
            pass
    except tk.TclError as e:
        print(f"创建进度窗口时出错: {str(e)}")
        progress_window = None

def scan_directory(directory):
    try:
        log_files = [f for f in os.scandir(directory) if f.name.lower().endswith('.log')]
        if log_files:
            return (directory, max(log_files, key=lambda x: x.stat().st_mtime))
    except Exception as e:
        print(f"扫描目录出错 {directory}: {str(e)}")
    return None

def process_single_directory(directory, index, latest_file, result_queue):
    try:
        process_log_file(latest_file.path, result_queue)
    except Exception as e:
        print(f"处理目录出错 {directory}: {str(e)}")
        result_queue.put((latest_file.path, None))

def process_results(directory, index, result):
    global processed_files, results_dict, log_contents
    
    if result is None:
        return
    
    keyword_counter, matching_lines, run_time, file_name = result
    
    total = sum(keyword_counter[keyword] for keyword in keywords)
    status_emoji = "⚠️ " if total > 0 else "✅ "
    
    row = [status_emoji + os.path.basename(directory)]
    row.extend([keyword_counter[keyword] for keyword in keywords])
    row.append(total)
    row.append(run_time)
    row.append(file_name)
    
    if matching_lines:
        log_contents.append((directory, matching_lines))
    
    results_dict[index] = row

def process_all_directories():
    global stats_data, results_dict, total_files, processed_files, main_window, progress_window, progress_var, progress_label, log_contents
    stats_data = []
    results_dict = {}
    log_contents = []
    processed_files = 0
    
    valid_directories = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as scan_executor:
        scan_futures = [scan_executor.submit(scan_directory, d) for d in directories if os.path.exists(d)]
        
        for future in concurrent.futures.as_completed(scan_futures):
            result = future.result()
            if result:
                valid_directories.append(result)
    
    total_files = len(valid_directories)
    
    if total_files == 0:
        messagebox.showinfo("ℹ️ 提示", "没有找到有效的日志文件")
        return
    
    show_progress_window()
    result_queue = Queue()
    
    file_path_map = {}
    dir_to_original_idx = {dir_path: idx for idx, dir_path in enumerate(directories) if os.path.exists(dir_path)}
    for dir_path, latest_file in valid_directories:
        original_idx = dir_to_original_idx.get(dir_path)
        if original_idx is not None:
            file_path_map[latest_file.path] = (dir_path, original_idx)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for directory, latest_file in valid_directories:
            original_idx = dir_to_original_idx.get(directory)
            if original_idx is not None:
                executor.submit(process_single_directory, directory, original_idx, latest_file, result_queue)
        
        def update_ui():
            try:
                if progress_window and progress_window.winfo_exists() and processed_files < total_files:
                    update_progress()
                    try:
                        progress_window.after(UI_UPDATE_INTERVAL, update_ui)
                    except tk.TclError:
                        pass
            except tk.TclError:
                pass
        
        try:
            if progress_window and hasattr(progress_window, 'winfo_exists') and progress_window.winfo_exists():
                progress_window.after(UI_UPDATE_INTERVAL, update_ui)
        except tk.TclError:
            pass
        except Exception as e:
            print(f"设置UI更新定时器时出错: {str(e)}")
        
        while processed_files < total_files:
            try:
                file_path, result = result_queue.get(timeout=0.1)
                if file_path in file_path_map:
                    directory, index = file_path_map[file_path]
                    if result is not None:
                        process_results(directory, index, result)
                processed_files += 1
            except Exception:
                continue
    
    try:
        if progress_window and hasattr(progress_window, 'winfo_exists') and progress_window.winfo_exists():
            progress_window.destroy()
    except (tk.TclError, Exception):
        pass
    
    progress_window = None
    progress_var = None
    progress_label = None
    
    for i in range(len(directories)):
        if i in results_dict:
            stats_data.append(results_dict[i])
    
    dir_to_matches = dict(log_contents)
    
    for directory in directories:
        if os.path.exists(directory) and directory in dir_to_matches:
            matching_lines = dir_to_matches[directory]
            if matching_lines:
                display_path = directory.replace('\\', '/')
                print("\n" + "═" * 100)
                print(f"📂 路径: {display_path}")
                print("═" * 100)
                
                keyword_to_lines = defaultdict(list)
                for line, keyword in matching_lines:
                    keyword_to_lines[keyword].append(line)
                
                for keyword, lines in keyword_to_lines.items():
                    if lines:
                        print(f"\n🔍 [{keyword}] ({len(lines)}个)")
                        for idx, line in enumerate(lines, 1):
                            # 对关键字进行不区分大小写的高亮替换，并保持原始日志的大小写格式
                            pattern = re.compile(re.escape(keyword), re.IGNORECASE)
                            highlighted_line = pattern.sub(lambda m: f"\033[1;31m{m.group(0)}\033[0m", line)
                            
                            # 带有 👉 [递增编号] 格式输出
                            print(f"  👉 [{idx}] {highlighted_line}")
                
                print("─" * 100)
    
    if stats_data:
        create_table_window()
    else:
        messagebox.showinfo("ℹ️ 提示", "没有找到有效数据")

def create_table_window():
    global root, main_window
    root = main_window
    root.deiconify()
    root.title("📊 关键字统计")
    
    main_frame = ttk.Frame(root, padding="0")
    main_frame.pack(expand=True, fill='both')
    
    base_headers = ["📂 路径"] + [f"🔍 {keyword}" for keyword in keywords] + ["📊 总计", "⏱️ 运行时间"]
    file_header = ["📄 文件名"]
    headers = base_headers + file_header
    
    style = ttk.Style()
    if not style.theme_names() or 'optimized_theme' not in style.theme_names():
        style.configure("Treeview", 
                      font=('Microsoft YaHei', 10),
                      rowheight=25,
                      borderwidth=1,
                      relief="solid",
                      fieldbackground="white",
                      background="white")
        style.configure("Treeview.Heading", font=('Microsoft YaHei', 10))
        style.map("Treeview",
                background=[("selected", "#CCE8FF")],
                foreground=[("selected", "black")])
    
    table_frame = ttk.Frame(main_frame)
    table_frame.pack(expand=True, fill='both')
    
    vsb = ttk.Scrollbar(table_frame, orient="vertical")
    hsb = ttk.Scrollbar(table_frame, orient="horizontal")
    
    tree = ttk.Treeview(table_frame, columns=headers, show='headings', 
                       height=min(len(stats_data), 20),
                       selectmode='extended',
                       yscrollcommand=vsb.set,
                       xscrollcommand=hsb.set)
    
    vsb.config(command=tree.yview)
    hsb.config(command=tree.xview)
    
    vsb.pack(side='right', fill='y')
    hsb.pack(side='bottom', fill='x')
    tree.pack(expand=True, fill='both')
    
    tree.tag_configure('red', foreground='red')
    
    char_width = 8
    max_widths = {}
    for i, header in enumerate(headers):
        column_data = [str(row[i]) for row in stats_data]
        longest_content = max([header] + column_data, key=len, default='')
        max_widths[header] = max(len(longest_content) * char_width + 30, 60)
    
    for header in headers:
        tree.column(header, width=max_widths[header], anchor='center', stretch=False)
        tree.heading(header, text=header, command=lambda h=header: sort_treeview(tree, h, False))
    
    batch_size = 100
    for i in range(0, len(stats_data), batch_size):
        batch = stats_data[i:i+batch_size]
        for row in batch:
            tags = []
            keyword_values = row[1:len(keywords)+1]
            if any(str(val) != '0' for val in keyword_values):
                tags.append('red')
            tree.insert('', 'end', values=row, tags=tags if tags else ())
        
        if i + batch_size < len(stats_data):
            root.update_idletasks()
    
    button_frame = ttk.Frame(main_frame)
    button_frame.pack(pady=5)
    
    def copy_all():
        buffer = io.StringIO()
        buffer.write('\t'.join(headers) + '\n')
        
        items = tree.get_children()
        batch_size = 500
        
        for i in range(0, len(items), batch_size):
            batch_items = items[i:i+batch_size]
            for item in batch_items:
                values = tree.item(item)['values']
                row_text = '\t'.join(str(x) if x is not None else "" for x in values)
                buffer.write(row_text + '\n')
        
        text = buffer.getvalue()
        buffer.close()
        
        root.clipboard_clear()
        root.clipboard_append(text)
        messagebox.showinfo("ℹ️ 提示", "已复制到剪贴板")
    
    copy_button = ttk.Button(button_frame, text="📋 一键复制", command=copy_all)
    copy_button.pack(side=tk.LEFT, padx=5)
    
    def refresh_data():
        root.destroy()
        process_all_directories()
    
    refresh_button = ttk.Button(button_frame, text="🔄 刷新数据", command=refresh_data)
    refresh_button.pack(side=tk.LEFT, padx=5)
    
    window_width = sum(max_widths.values()) + 30
    displayed_rows = min(len(stats_data), 20)
    window_height = (displayed_rows + 1) * 25 + 80
    
    x = (root.winfo_screenwidth() - window_width) // 2
    y = (root.winfo_screenheight() - window_height) // 2
    root.geometry(f"{window_width}x{window_height}+{x}+{y}")
    
    def close_window(event=None):
        root.destroy()
    
    root.bind('<space>', close_window)
    root.bind('<Escape>', close_window)
    
    root.mainloop()

def sort_treeview(tree, col, reverse):
    items = tree.get_children('')
    data = []
    
    is_numeric = False
    if items:
        first_val = tree.set(items[0], col)
        try:
            float(first_val)
            is_numeric = True
        except (ValueError, TypeError):
            is_numeric = False
    
    for item in items:
        value = tree.set(item, col)
        if is_numeric:
            try:
                value = float(value)
            except (ValueError, TypeError):
                value = 0.0
        data.append((value, item))
    
    data.sort(reverse=reverse)
    
    for idx, (_, item) in enumerate(data):
        tree.move(item, '', idx)
    
    tree.heading(col, command=lambda: sort_treeview(tree, col, not reverse))

def main():
    global main_window
    
    # 如果在 Windows 平台运行，执行空系统调用来开启命令行对 ANSI Escape 序列的支持
    if os.name == 'nt':
        os.system('')
        
    while True:
        main_window = tk.Tk()
        main_window.withdraw()
        main_window.title("日志搜索工具")
        
        def on_main_window_close():
            main_window.quit()
            main_window.destroy()
            raise SystemExit
            
        main_window.protocol("WM_DELETE_WINDOW", on_main_window_close)
        main_window.update_idletasks()
        
        try:
            process_all_directories()
            main_window.mainloop()
            
            try:
                if main_window and main_window.winfo_exists():
                    main_window.destroy()
            except tk.TclError:
                pass
                
        except Exception as e:
            import traceback
            error_msg = f"程序运行出错：\n{str(e)}\n\n{traceback.format_exc()}"
            print(error_msg)
            messagebox.showerror("❌ 错误", error_msg)
            break
        except KeyboardInterrupt:
            print("程序被用户中断")
            break
        except SystemExit:
            break
    
    try:
        if main_window and main_window.winfo_exists():
            main_window.destroy()
    except tk.TclError:
        pass

if __name__ == "__main__":
    main()