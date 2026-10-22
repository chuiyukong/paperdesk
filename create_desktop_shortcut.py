"""Create a Windows desktop shortcut with the platform's existing P logo."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    if sys.platform != "win32":
        raise SystemExit("桌面快捷方式仅适用于 Windows。")
    root = Path(__file__).resolve().parent
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise SystemExit("未找到 Python 的无窗口启动程序，请使用已安装的 Python 3.12 运行此文件。")
    for path in (root / "launch.py", root / "assets" / "paperdesk.ico"):
        if not path.is_file():
            raise SystemExit(f"程序文件缺失：{path}")
    environment = os.environ.copy()
    environment["PAPERDESK_SHORTCUT_ROOT"] = str(root)
    environment["PAPERDESK_SHORTCUT_PYTHONW"] = str(pythonw)
    # Paths are passed as environment values rather than interpolated shell code.
    command = r'''
$ErrorActionPreference = 'Stop'
$platformRoot = $env:PAPERDESK_SHORTCUT_ROOT
$desktopDirectory = [Environment]::GetFolderPath('Desktop')
if (-not $desktopDirectory) { throw 'Windows did not return a desktop folder.' }
$shortcutPath = Join-Path $desktopDirectory '论文管理台.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $env:PAPERDESK_SHORTCUT_PYTHONW
$shortcut.Arguments = '"' + (Join-Path $platformRoot 'launch.py') + '"'
$shortcut.WorkingDirectory = $platformRoot
$shortcut.IconLocation = (Join-Path $platformRoot 'assets\paperdesk.ico') + ',0'
$shortcut.Description = 'PaperDesk - start the local service and open the workspace'
$shortcut.WindowStyle = 1
$shortcut.Save()
$shortcutPath
'''
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env=environment, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise SystemExit("桌面快捷方式创建失败：" + result.stderr.decode("utf-8", errors="replace"))
    print(json.dumps({"created": True, "icon": "assets/paperdesk.ico"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
