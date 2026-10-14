"""Friendly double-click launcher, including startup errors when run without a console."""
import sys
import traceback
from paperdesk import main, default_data_dir

try:
    main()
except Exception as error:
    directory = default_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "startup-error.log").open("a", encoding="utf-8") as log:
        traceback.print_exc(file=log)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("论文管理平台启动失败", f"{error}\n\n数据保留在：{directory}\n详细信息已写入 startup-error.log。")
        root.destroy()
    finally:
        sys.exit(1)
