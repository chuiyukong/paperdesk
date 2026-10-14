"""Isolated native picker; manuscript contents are never read or copied."""
import json
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
try:
    if sys.argv[1] == "folder":
        path = filedialog.askdirectory(title="选择论文文件夹", parent=root)
    else:
        path = filedialog.askopenfilename(title="选择稿件文件", parent=root, filetypes=[
            ("稿件文档", "*.doc *.docx *.pdf *.odt *.rtf *.txt *.md *.tex *.wps"),
        ])
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"path": path}, ensure_ascii=False))
finally:
    root.destroy()
