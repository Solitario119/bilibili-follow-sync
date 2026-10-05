# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：单文件 Windows exe（在仓库根目录运行 pyinstaller desktop/BiliFollowSync.spec）
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# qfluentwidgets 携带字体/图标等资源文件，需整体收集
qw_datas, qw_binaries, qw_hidden = collect_all("qfluentwidgets")

a = Analysis(
    # 注意：spec 内脚本路径相对 SPECPATH（本 spec 所在目录）解析，必须用绝对路径
    [os.path.join(ROOT, "desktop", "bili_follow_gui.py")],
    pathex=[ROOT],
    binaries=qw_binaries,
    datas=qw_datas,
    hiddenimports=["core", "core.bilibili", "core.tasks", "qrcode"] + qw_hidden,
    hookspath=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BiliFollowSync",
    console=False,
    upx=False,
)
