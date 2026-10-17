"""Double-click entry with explicit, validated recovery for damaged local data."""
import sys
import traceback
from pathlib import Path
from paperdesk import main, default_data_dir, InvalidDatabase, recover_database


def selected_directory():
    if "--data-dir" in sys.argv:
        index = sys.argv.index("--data-dir")
        if index + 1 < len(sys.argv):
            return Path(sys.argv[index + 1]).resolve()
    for value in sys.argv:
        if value.startswith("--data-dir="):
            return Path(value.split("=", 1)[1]).resolve()
    return default_data_dir()


def run(allow_recovery=True):
    try:
        main()
        return 0
    except Exception as error:
        directory = selected_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / "startup-error.log").open("a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
        except OSError:
            pass
        import tkinter as tk
        from tkinter import filedialog, messagebox
        root = tk.Tk()
        root.withdraw()
        recovered = False
        try:
            if allow_recovery and isinstance(error, InvalidDatabase) and messagebox.askyesno("数据文件异常，选择备份恢复？", f"{error}\n\n可选择 SQLite 或 JSON 备份。恢复前会校验并显示数量，原文件和现有备份另存保留。", parent=root):
                source = filedialog.askopenfilename(title="选择论文管理平台备份", initialdir=str(directory / "backups"), filetypes=[("PaperDesk 备份", "*.sqlite3 *.json"), ("所有文件", "*.*")], parent=root)
                if source:
                    try:
                        counts = recover_database(directory, source, lambda c: messagebox.askyesno("确认恢复已校验的备份", f"{c['papers']} 篇论文、{c['journals']} 个期刊、{c['submissions']} 轮投稿。\n确认后替换异常数据库，原文件和已有备份会保留。", parent=root))
                        recovered = counts is not None
                    except Exception as recovery_error:
                        messagebox.showerror("恢复未完成", f"{recovery_error}\n\n原文件保留在：{directory}", parent=root)
            else:
                messagebox.showerror("论文管理平台启动失败", f"{error}\n\n数据保留在：{directory}\n详细信息见 startup-error.log。", parent=root)
        finally:
            root.destroy()
        return run(False) if recovered else 1


if __name__ == "__main__":
    sys.exit(run())
