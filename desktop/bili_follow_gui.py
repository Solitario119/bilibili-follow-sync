# -*- coding: utf-8 -*-
"""bilibili-follow-sync 桌面版（PySide6 + Fluent Widgets，单窗口）。

业务逻辑与 CLI 共用 core/ 包；本文件只负责界面、线程与诊断。
源码运行：python desktop/bili_follow_gui.py（需 pip install -r desktop/requirements.txt）
无头自测：--selftest（渲染与接口）、--uitest（mock 网络层自动点按钮 + 截图）
诊断日志与应用配置存于 %APPDATA%/B站关注同步/，设置里可一键打开该文件夹。
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qrcode
import requests
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QVBoxLayout,
    QWidget)

from qfluentwidgets import (
    BodyLabel, CardWidget, CaptionLabel, CheckBox, Dialog, DoubleSpinBox,
    FluentIcon as FIF, InfoBar, InfoBarPosition, MessageBoxBase,
    PrimaryPushButton, PushButton, SpinBox, SubtitleLabel, SwitchButton,
    TextEdit, Theme, TitleLabel, TransparentToolButton, isDarkTheme, setTheme,
    setThemeColor)

from core import bilibili as bili
from core import tasks

APP_NAME = "B站关注同步"


class NoWheelSpinBox(SpinBox):
    """未聚焦时忽略滚轮，防止滚动弹窗时误改数值"""
    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()


class NoWheelDoubleSpinBox(DoubleSpinBox):
    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()
APP_VERSION = "0.1.0"
REPO_URL = "https://github.com/Solitario119/bilibili-follow-sync"
TODAY = lambda: time.strftime("%Y-%m-%d")


# ================= 诊断日志 =================

def _log_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def diag_log(*parts):
    try:
        os.makedirs(_log_dir(), exist_ok=True)
        with open(os.path.join(_log_dir(), "gui.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + " ".join(str(p) for p in parts) + "\n")
    except Exception:
        pass


def _excepthook(t, v, tb):
    diag_log("UNCAUGHT", t.__name__, v, "".join(traceback.format_exception(t, v, tb)))
    sys.__excepthook__(t, v, tb)


# ================= 配置（本机保存，见 README） =================

def config_path():
    return os.path.join(_log_dir(), "config.json")


class Config:
    DEFAULTS = {
        "main": {"cookie": "", "uid": "", "uname": "", "face": ""},
        "alt": {"cookie": "", "uid": "", "uname": "", "face": ""},
        "interval_s": 3.0,
        "daily_limit": tasks.DAILY_LIMIT_DEFAULT,
        "daily_date": "", "daily_count": 0,
        "theme": "light",
    }

    def __init__(self):
        self.path = config_path()
        self.data = dict(self.DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
            for k, v in self.DEFAULTS.items():
                self.data[k] = stored.get(k, v)
        except Exception:
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def account(self, role):
        return self.data[role]

    def daily_used(self):
        if self.data.get("daily_date") != TODAY():
            return 0
        return int(self.data.get("daily_count") or 0)

    def bump_daily(self):
        if self.data.get("daily_date") != TODAY():
            self.data["daily_date"], self.data["daily_count"] = TODAY(), 0
        self.data["daily_count"] = int(self.data.get("daily_count") or 0) + 1
        self.save()


# ================= 工作线程 =================

class Worker(QThread):
    """跑 core 流程；通过信号把日志/确认请求送回 UI 线程"""
    logSig = Signal(str)
    progSig = Signal(str)
    askSig = Signal(str)
    doneSig = Signal(str)
    failSig = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.stop_flag = False
        self._answer = None
        self._ask_event = threading.Event()

    def run(self):
        try:
            self.fn()
            self.doneSig.emit("完成")
        except tasks.FlowCancelled as e:
            self.logSig.emit(str(e))
            self.doneSig.emit("已取消")
        except bili.BiliError as e:
            diag_log("BiliError:", e)
            self.failSig.emit(str(e))
        except Exception as e:
            diag_log("WORKER CRASH:", "".join(traceback.format_exception(type(e), e, e.__traceback__)))
            self.failSig.emit(f"{type(e).__name__}: {e}")

    # ---- 传给 core 的回调 ----
    def report(self, kind, text):
        (self.logSig if kind == "log" else self.progSig).emit(text)

    def confirm(self, prompt, accept):
        self._ask_event.clear()
        self._answer = None
        pretty = prompt.replace("(y/N)", "").replace("输入 yes 执行：", "").strip() or "确认执行？"
        self.askSig.emit(pretty)
        self._ask_event.wait()
        return bool(self._answer)

    def should_stop(self):
        return self.stop_flag


class LoginWorker(QThread):
    """扫码登录：生成二维码 -> 轮询 -> 取 Cookie"""
    qrReadySig = Signal(str)
    statusSig = Signal(str)
    successSig = Signal(dict)
    failSig = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stop_flag = False

    def run(self):
        try:
            key_url = bili.qr_generate()
            diag_log("QR generate ok")
            self.qrReadySig.emit(key_url["url"])
        except Exception as e:
            diag_log("QR generate FAIL:", repr(e))
            self.failSig.emit(f"生成二维码失败：{e}")
            return
        while not self.stop_flag:
            try:
                r = bili.qr_poll(key_url["qrcode_key"])
            except Exception as e:
                diag_log("QR poll FAIL:", repr(e))
                self.failSig.emit(f"轮询扫码状态失败：{e}")
                return
            state = r["state"]
            if state == "success":
                cookie = r["cookie"]
                try:
                    nav = bili.get_nav(cookie)
                    uid = (nav or {}).get("mid") or bili.get_uid(cookie)
                    uname = (nav or {}).get("uname") or r.get("uname") or ""
                    face = (nav or {}).get("face") or ""
                except Exception as e:
                    diag_log("NAV after login FAIL:", repr(e))
                    self.failSig.emit(f"获取登录信息失败：{e}")
                    return
                diag_log("QR login ok:", uname)
                self.successSig.emit({"cookie": cookie, "uid": uid,
                                      "uname": uname, "face": face})
                return
            if state == "expired":
                try:
                    key_url = bili.qr_generate()
                except Exception as e:
                    self.failSig.emit(f"刷新二维码失败：{e}")
                    return
                self.qrReadySig.emit(key_url["url"])
                self.statusSig.emit("二维码已过期，已自动刷新，请重新扫码")
            elif state == "scanned":
                self.statusSig.emit("已扫码，请在手机上确认")
            else:
                self.statusSig.emit("请用B站手机客户端扫码")
            time.sleep(2)


class AvatarLoader(QThread):
    """下载并缓存头像；失败时 UI 用首字占位，不报错"""
    doneSig = Signal(str, QPixmap)  # uid, 已裁圆的位图

    def __init__(self, uid, url, parent=None):
        super().__init__(parent)
        self.uid = str(uid)
        self.url = url

    def _cached_or_download(self):
        cache = os.path.join(_log_dir(), "avatars", f"{self.uid}.jpg")
        if not os.path.exists(cache) and self.url:
            try:
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                r = requests.get(self.url, headers=bili.BASE_HEADERS, timeout=10)
                if r.ok and r.content:
                    with open(cache, "wb") as f:
                        f.write(r.content)
            except Exception as e:
                diag_log("avatar download fail:", repr(e))
        if os.path.exists(cache):
            pix = QPixmap(cache)
            if not pix.isNull():
                return pix
        return QPixmap()

    def run(self):
        pix = self._cached_or_download()
        if not pix.isNull():
            self.doneSig.emit(self.uid, circular_pixmap(pix, 56))


# ================= 二维码与头像渲染 =================

def make_qr_pixmap(text, max_side=300):
    """渲染二维码，边长不超过 max_side（位图超出标签会被裁掉定位角，导致扫不出来）"""
    qr = qrcode.QRCode(border=3)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    img = QImage(n, n, QImage.Format_RGB32)
    img.fill(0xFFFFFFFF)
    for y in range(n):
        row = matrix[y]
        for x in range(n):
            if row[x]:
                img.setPixel(x, y, 0xFF000000)
    pix = QPixmap.fromImage(img)
    # 整数倍放大，保证每个模块宽度一致、边缘清晰
    scale = max(1, max_side // n)
    side = n * scale
    if side != pix.width():
        pix = pix.scaled(side, side, Qt.IgnoreAspectRatio, Qt.FastTransformation)
    return pix


def circular_pixmap(src: QPixmap, size: int) -> QPixmap:
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, size, size)
    p.setClipPath(path)
    p.drawPixmap(0, 0, src.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    p.end()
    return out


def initial_avatar(text: str, size: int = 56) -> QPixmap:
    """无头像时的首字圆形占位"""
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#fb7299"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(0, 0, size, size)
    p.setPen(QColor("#ffffff"))
    f = QFont()
    f.setPixelSize(int(size * 0.45))
    f.setBold(True)
    p.setFont(f)
    ch = (text or "?").strip()[:1] or "?"
    p.drawText(out.rect(), Qt.AlignCenter, ch)
    p.end()
    return out


class AvatarLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(56, 56)

    def show_initial(self, text):
        self.setPixmap(initial_avatar(text))

    def show_face(self, pix):
        if pix and not pix.isNull():
            self.setPixmap(pix)


# ================= 弹窗 =================

class QrLoginBox(MessageBoxBase):
    def __init__(self, parent_window, role_label):
        super().__init__(parent_window)
        self.worker = None
        self.result_data = None

        self.titleLabel = SubtitleLabel(f"{role_label}扫码登录")
        self.qr_label = QLabel("正在生成二维码…")
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setStyleSheet(
            "background:#fff;border:1px solid rgba(0,0,0,.08);border-radius:8px;")
        self.status_label = BodyLabel("正在连接B站…")
        self.status_label.setAlignment(Qt.AlignCenter)
        tip = CaptionLabel("打开B站手机客户端扫一扫，二维码只在本地处理")
        tip.setAlignment(Qt.AlignCenter)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.qr_label, 0, Qt.AlignHCenter)
        self.viewLayout.addWidget(self.status_label)
        self.viewLayout.addWidget(tip)
        self.yesButton.setText("关闭")
        self.cancelButton.hide()
        self.widget.setMinimumWidth(420)
        self.rejected.connect(self._stop_worker)

    def exec(self):
        self.worker = LoginWorker(self)
        self.worker.qrReadySig.connect(self.show_qr)
        self.worker.statusSig.connect(self.status_label.setText)
        self.worker.successSig.connect(self.on_success)
        self.worker.failSig.connect(self.on_fail)
        self.worker.start()
        return super().exec()

    def show_qr(self, url):
        try:
            pix = make_qr_pixmap(url)
            if pix.isNull() or pix.width() < 50:
                raise RuntimeError("渲染结果为空图")
            self.qr_label.setFixedSize(pix.size())
            self.qr_label.setPixmap(pix)
            self.status_label.setText("请用B站手机客户端扫码")
        except Exception as e:
            diag_log("QR RENDER FAIL:", "".join(traceback.format_exception(type(e), e, e.__traceback__)))
            self.qr_label.setText("二维码渲染失败")
            self.status_label.setText(f"渲染失败：{e}（详见日志）")

    def on_success(self, data):
        self.result_data = data
        self.accept()

    def on_fail(self, msg):
        diag_log("QR dialog fail:", msg)
        self.status_label.setText(msg)
        self.status_label.setStyleSheet("color:#d44;")

    def _stop_worker(self):
        if self.worker:
            self.worker.stop_flag = True
            self.worker.wait(3000)


class PasteCookieBox(MessageBoxBase):
    def __init__(self, parent_window, title):
        super().__init__(parent_window)
        self.titleLabel = SubtitleLabel(title)
        self.cookie_edit = TextEdit()
        self.cookie_edit.setPlaceholderText(
            "F12 → 网络 → 任意 bilibili.com 请求 → 复制整串 Cookie 粘贴到这里")
        self.cookie_edit.setFixedHeight(120)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(CaptionLabel("凭据只在本地处理"))
        self.viewLayout.addWidget(self.cookie_edit)
        self.yesButton.setText("登录")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(480)

    def cookie_text(self):
        return self.cookie_edit.toPlainText()


class SettingsBox(MessageBoxBase):
    def __init__(self, parent_window, config, on_theme_changed):
        super().__init__(parent_window)
        self.config = config
        self._on_theme_changed = on_theme_changed

        self.titleLabel = SubtitleLabel("设置")
        form = QVBoxLayout()
        form.setSpacing(14)

        row1 = QHBoxLayout()
        row1.addWidget(BodyLabel("关注间隔"))
        row1.addStretch(1)
        self.interval_spin = NoWheelDoubleSpinBox()
        self.interval_spin.setRange(0.5, 10.0)
        self.interval_spin.setSingleStep(0.5)
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.setValue(float(config.data.get("interval_s", 3.0)))
        row1.addWidget(self.interval_spin)
        form.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(BodyLabel("每日尝试上限"))
        row2.addStretch(1)
        self.limit_spin = NoWheelSpinBox()
        self.limit_spin.setRange(10, 1000)
        self.limit_spin.setValue(int(config.data.get("daily_limit", 290)))
        row2.addWidget(self.limit_spin)
        form.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(BodyLabel("深色模式"))
        row3.addStretch(1)
        self.theme_switch = SwitchButton()
        self.theme_switch.setChecked(config.data.get("theme") == "dark")
        row3.addWidget(self.theme_switch)
        form.addLayout(row3)

        row4 = QHBoxLayout()
        row4.addWidget(BodyLabel("诊断日志"))
        row4.addStretch(1)
        open_log_btn = PushButton("打开日志文件夹")
        open_log_btn.clicked.connect(self._open_log_dir)
        row4.addWidget(open_log_btn)
        form.addLayout(row4)

        row5 = QHBoxLayout()
        row5.addWidget(CaptionLabel(f"版本 v{APP_VERSION}"))
        row5.addStretch(1)
        repo_link = CaptionLabel("GitHub：" + REPO_URL)
        repo_link.setStyleSheet("color:#0066c0;")
        row5.addWidget(repo_link)
        form.addLayout(row5)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addLayout(form)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(420)

        self.interval_spin.valueChanged.connect(self._save_interval)
        self.limit_spin.valueChanged.connect(self._save_limit)
        self.theme_switch.checkedChanged.connect(self._switch_theme)

    def _save_interval(self, v):
        self.config.data["interval_s"] = float(v)
        self.config.save()

    def _save_limit(self, v):
        self.config.data["daily_limit"] = int(v)
        self.config.save()

    def _switch_theme(self, dark):
        self.config.data["theme"] = "dark" if dark else "light"
        self.config.save()
        setTheme(Theme.DARK if dark else Theme.LIGHT)
        self._on_theme_changed()

    def _open_log_dir(self):
        try:
            os.makedirs(_log_dir(), exist_ok=True)
            os.startfile(_log_dir())
        except Exception as e:
            diag_log("open log dir fail:", repr(e))


# ================= 账号卡片 =================

class AccountCard(CardWidget):
    changed = Signal()

    def __init__(self, parent, role, role_label, config):
        super().__init__(parent)
        self.role = role
        self.role_label = role_label
        self.config = config
        self._avatar_loader = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        # 头像 + 昵称/UID 同排
        head = QHBoxLayout()
        head.setSpacing(12)
        self.avatar = AvatarLabel(self)
        head.addWidget(self.avatar)
        info = QVBoxLayout()
        info.setSpacing(2)
        self.name_label = BodyLabel(role_label)
        self.uid_label = CaptionLabel("未登录")
        info.addWidget(self.name_label)
        info.addWidget(self.uid_label)
        head.addLayout(info)
        head.addStretch(1)
        outer.addLayout(head)

        btns = QHBoxLayout()
        self.scan_btn = PrimaryPushButton(FIF.QR_CODE, "扫码登录") if hasattr(FIF, "QR_CODE") \
            else PrimaryPushButton("扫码登录")
        self.paste_btn = PushButton("粘贴Cookie")
        self.logout_btn = PushButton("退出登录")
        btns.addWidget(self.scan_btn)
        btns.addWidget(self.paste_btn)
        btns.addWidget(self.logout_btn)
        outer.addLayout(btns)

        self.scan_btn.clicked.connect(self.scan_login)
        self.paste_btn.clicked.connect(self.paste_login)
        self.logout_btn.clicked.connect(self.logout)
        self.refresh()

    def refresh(self):
        acc = self.config.account(self.role)
        logged = bool(acc.get("cookie"))
        if logged:
            self.name_label.setText(acc.get("uname") or "已登录")
            self.uid_label.setText(f"UID {acc.get('uid')}")
            self.logout_btn.setEnabled(True)
            self.scan_btn.setText("重新扫码")
        else:
            self.name_label.setText(self.role_label)
            self.uid_label.setText("未登录")
            self.logout_btn.setEnabled(False)
            self.scan_btn.setText("扫码登录")
        self.avatar.show_initial((acc.get("uname") if logged else self.role_label) or "?")
        if logged and acc.get("uid"):
            self._load_face(acc.get("uid"), acc.get("face"))
        self.changed.emit()

    def _load_face(self, uid, face_url):
        self._avatar_loader = AvatarLoader(uid, face_url, self)
        self._avatar_loader.doneSig.connect(
            lambda _uid, pix, card=self: card.avatar.show_face(pix))
        self._avatar_loader.start()

    def scan_login(self):
        dlg = QrLoginBox(self.window(), self.role_label)
        dlg.exec()
        if dlg.result_data:
            self.config.account(self.role).update(dlg.result_data)
            self.config.save()
            self.refresh()

    def paste_login(self):
        dlg = PasteCookieBox(self.window(), f"{self.role_label}粘贴 Cookie 登录")
        if not dlg.exec() or not dlg.cookie_text().strip():
            return
        try:
            cookie = bili.clean_cookie(dlg.cookie_text())
            uid = bili.get_uid(cookie, "账号")
            nav = bili.get_nav(cookie)
        except bili.BiliError as e:
            diag_log("paste cookie invalid:", e)
            InfoBar.error(title="Cookie 无效", content=str(e),
                          orient=Qt.Horizontal, isClosable=True,
                          position=InfoBarPosition.TOP, duration=5000, parent=self.window())
            return
        self.config.account(self.role).update({
            "cookie": cookie, "uid": uid,
            "uname": (nav or {}).get("uname") or "",
            "face": (nav or {}).get("face") or ""})
        self.config.save()
        self.refresh()

    def logout(self):
        self.config.account(self.role).update(
            {"cookie": "", "uid": "", "uname": "", "face": ""})
        self.config.save()
        self.refresh()


# ================= 主窗口（单页） =================

class _Emitter(QObject):
    """worker 线程安全地把“尝试计数变化”通知 UI"""
    changed = Signal()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = Config()
        self.worker = None
        self._emitter = _Emitter(self)
        self._emitter.changed.connect(self._refresh_status)
        setThemeColor("#fb7299")
        setTheme(Theme.DARK if self.config.data.get("theme") == "dark" else Theme.LIGHT)

        self._tasks_started = False
        self._build_ui()
        self._apply_bg()
        self.update_enabled()
        self._refresh_status()
        self._set_welcome()

    # ---- 界面 ----
    def _build_ui(self):
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(760, 660)
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 18, 24, 16)
        root.setSpacing(12)

        # 标题行
        title_row = QHBoxLayout()
        title_row.addWidget(TitleLabel(APP_NAME))
        title_row.addStretch(1)
        self.settings_btn = TransparentToolButton(FIF.SETTING)
        self.settings_btn.setToolTip("设置")
        self.settings_btn.clicked.connect(self.open_settings)
        title_row.addWidget(self.settings_btn)
        root.addLayout(title_row)

        # 账号卡片
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.card_main = AccountCard(self, "main", "主号", self.config)
        self.card_alt = AccountCard(self, "alt", "小号", self.config)
        cards.addWidget(self.card_main)
        cards.addWidget(self.card_alt)
        root.addLayout(cards)
        self.card_main.changed.connect(self.update_enabled)
        self.card_alt.changed.connect(self.update_enabled)

        # 主操作
        ops = QVBoxLayout()
        ops.setSpacing(8)
        main_row = QHBoxLayout()
        self.sync_btn = PrimaryPushButton(FIF.PLAY, "开始同步（主号 → 小号）")
        self.sync_btn.setFixedHeight(40)
        self.stop_btn = PushButton(FIF.PAUSE, "停止")
        self.stop_btn.setFixedHeight(40)
        main_row.addWidget(self.sync_btn, 4)
        main_row.addWidget(self.stop_btn, 1)
        ops.addLayout(main_row)

        second_row = QHBoxLayout()
        self.backup_btn = PushButton(FIF.SAVE, "备份关注列表")
        self.clean_btn = PushButton(FIF.BROOM, "反向清理（取关小号上多余的关注）")
        second_row.addWidget(self.backup_btn, 1)
        second_row.addWidget(self.clean_btn, 2)
        ops.addLayout(second_row)
        self.protect_chk = CheckBox("反向清理时保护小号的特别关注（推荐）")
        self.protect_chk.setChecked(True)
        ops.addWidget(self.protect_chk)
        root.addLayout(ops)

        # 日志
        root.addWidget(BodyLabel("运行日志"))
        self.log_view = TextEdit()
        self.log_view.setReadOnly(True)
        root.addWidget(self.log_view, 1)

        # 状态栏
        self.status_label = CaptionLabel("就绪")
        root.addWidget(self.status_label)

        self.sync_btn.clicked.connect(self.start_sync)
        self.clean_btn.clicked.connect(self.start_clean)
        self.backup_btn.clicked.connect(self.start_backup)
        self.stop_btn.clicked.connect(self.stop_worker)

    def _set_welcome(self):
        if self._tasks_started:
            return
        ready = all(self.config.account(r).get("cookie") for r in ("main", "alt"))
        text = ("两个账号已就绪，点「开始同步」开始。B站每天限约 300 次关注，"
                "大列表会分几天完成。" if ready
                else "先在上方登录两个账号，然后点「开始同步」。")
        self.log_view.setPlainText(text)

    def _apply_bg(self):
        bg = "rgb(32,32,32)" if isDarkTheme() else "rgb(249,249,249)"
        self.centralWidget().setStyleSheet(f"#central {{ background: {bg}; }}")

    def _on_theme_changed(self):
        self._apply_bg()

    # ---- 状态与日志 ----
    def log_line(self, text):
        self.log_view.append(text)

    def set_prog(self, text):
        self._last_prog = text
        self._refresh_status()

    def _refresh_status(self):
        used = self.config.daily_used()
        limit = int(self.config.data.get("daily_limit", 290))
        prog = getattr(self, "_last_prog", "")
        running = self.worker is not None and self.worker.isRunning()
        head = "运行中" if running else "就绪"
        self.status_label.setText(
            f"{head} · 今日已尝试 {used}/{limit}" + (f" · {prog}" if prog else ""))

    def update_enabled(self):
        running = self.worker is not None and self.worker.isRunning()
        main_ready = bool(self.config.account("main").get("cookie"))
        both_ready = main_ready and bool(self.config.account("alt").get("cookie"))
        self.sync_btn.setEnabled(both_ready and not running)
        self.clean_btn.setEnabled(both_ready and not running)
        self.backup_btn.setEnabled(main_ready and not running)
        self.stop_btn.setEnabled(running)
        for card in (self.card_main, self.card_alt):
            for b in (card.scan_btn, card.paste_btn, card.logout_btn):
                b.setEnabled(not running)
        self._refresh_status()
        self._set_welcome()

    def on_ask(self, prompt):
        d = Dialog("确认", prompt, self)
        self.worker._answer = bool(d.exec())
        self.worker._ask_event.set()

    def on_done(self, _msg):
        self.log_line("任务结束")
        InfoBar.success(title="完成", content="任务已结束",
                        orient=Qt.Horizontal, isClosable=True,
                        position=InfoBarPosition.TOP, duration=3000, parent=self)
        self.worker = None
        self.update_enabled()

    def on_fail(self, msg):
        self.log_line("❌ " + msg)
        InfoBar.error(title="出错", content=msg,
                      orient=Qt.Horizontal, isClosable=True,
                      position=InfoBarPosition.TOP, duration=5000, parent=self)
        self.worker = None
        self.update_enabled()

    def start_worker(self, job):
        if self.worker and self.worker.isRunning():
            return
        self._tasks_started = True
        self.worker = Worker(job, self)
        self.worker.logSig.connect(self.log_line)
        self.worker.progSig.connect(self.set_prog)
        self.worker.askSig.connect(self.on_ask)
        self.worker.doneSig.connect(self.on_done)
        self.worker.failSig.connect(self.on_fail)
        self.worker.start()
        self.update_enabled()

    def stop_worker(self):
        if self.worker:
            self.worker.stop_flag = True
            self.set_prog("正在停止（等待当前请求返回）")

    def _on_attempt(self):
        self.config.bump_daily()
        self._emitter.changed.emit()

    # ---- 设置 ----
    def open_settings(self):
        SettingsBox(self, self.config, self._on_theme_changed).exec()

    # ---- 三个动作 ----
    def start_sync(self):
        cfg = self.config

        def job():
            tasks.run_sync(
                cfg.account("main")["cookie"], cfg.account("alt")["cookie"],
                report=self.worker.report, confirm=self.worker.confirm,
                daily_limit=int(cfg.data.get("daily_limit", 290)),
                interval=float(cfg.data.get("interval_s", 3.0)),
                used_today=cfg.daily_used(), on_attempt=self._on_attempt,
                should_stop=self.worker.should_stop)

        self.log_line("开始同步")
        self.start_worker(job)

    def start_clean(self):
        cfg = self.config

        def job():
            tasks.run_clean(
                cfg.account("main")["cookie"], cfg.account("alt")["cookie"],
                report=self.worker.report, confirm=self.worker.confirm,
                daily_limit=int(cfg.data.get("daily_limit", 290)),
                protect_special=self.protect_chk.isChecked(),
                protect_hint="（可取消勾选下方保护选项）",
                rerun_cmd="「反向清理」",
                interval=float(cfg.data.get("interval_s", 3.0)),
                used_today=cfg.daily_used(), on_attempt=self._on_attempt,
                should_stop=self.worker.should_stop)

        self.log_line("开始反向清理")
        self.start_worker(job)

    def start_backup(self):
        out_dir = QFileDialog.getExistingDirectory(self, "选择备份保存目录")
        if not out_dir:
            return
        cfg = self.config

        def job():
            tasks.run_backup(cfg.account("main")["cookie"], out_dir,
                             report=self.worker.report, label="主号", include_tags=True)

        self.log_line("开始备份")
        self.start_worker(job)


# ================= 无头自测（CI / 打包验证，不需要人点） =================

def selftest():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication(sys.argv)  # widgets 需要先有 QApplication
    # 1) 二维码渲染与尺寸（超出弹窗会被裁掉定位角，导致扫不出来）
    pix = make_qr_pixmap("https://passport.bilibili.com/selftest")
    assert not pix.isNull() and 50 <= pix.width() <= 300, f"二维码尺寸异常: {pix.width()}"
    # 2) 头像渲染（圆形裁剪与首字占位）
    face = circular_pixmap(QPixmap(200, 200), 56)
    assert not face.isNull() and face.width() == 56
    ph = initial_avatar("测试", 56)
    assert not ph.isNull()
    # 3) 主题与组件可用
    setTheme(Theme.LIGHT)
    setTheme(Theme.DARK)
    setTheme(Theme.LIGHT)
    # 4) 完整构建主窗口（覆盖全部界面的导入与组装错误）
    win = MainWindow()
    win.close()
    # 5) 网络生成二维码（软性检查：CI 网络不通不算失败）
    try:
        r = bili.qr_generate()
        assert r.get("url") and r.get("qrcode_key")
        pix2 = make_qr_pixmap(r["url"])
        assert not pix2.isNull()
        print("selftest: qr_generate OK")
    except Exception as e:
        print("selftest: qr_generate soft-skip:", e)
    print(f"selftest OK ({APP_VERSION})")
    return 0


# ================= UI 自动化自测（--uitest，mock 网络层 + 截图） =================

def _wait_worker(win, app, timeout_s=20):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        app.processEvents()
        if win.worker is None:
            return True
        time.sleep(0.05)
    return False


def uitest():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["APPDATA"] = os.path.join(tempfile.gettempdir(), "bfs_uitest")
    shutil.rmtree(os.environ["APPDATA"], ignore_errors=True)
    app = QApplication.instance() or QApplication(sys.argv)
    shots_dir = os.path.join(os.environ["APPDATA"], "shots")
    os.makedirs(shots_dir, exist_ok=True)
    win = MainWindow()
    win.resize(760, 660)
    win.show()
    app.processEvents()

    # 1) 主窗口（未登录）截图；此时操作按钮应禁用
    assert win.grab().save(os.path.join(shots_dir, "main_loggedout.png"))
    assert not win.sync_btn.isEnabled()
    assert not win.clean_btn.isEnabled()
    assert not win.backup_btn.isEnabled()

    # 2) 模拟登录态后按钮解禁 + 截图
    win.config.account("main").update(
        {"cookie": "DedeUserID=111; bili_jct=t1", "uid": "111", "uname": "测试主号", "face": ""})
    win.config.account("alt").update(
        {"cookie": "DedeUserID=222; bili_jct=t2", "uid": "222", "uname": "测试小号", "face": ""})
    win.card_main.refresh()
    win.card_alt.refresh()
    app.processEvents()
    assert win.sync_btn.isEnabled() and win.backup_btn.isEnabled()
    assert win.grab().save(os.path.join(shots_dir, "main_loggedin.png"))

    # 确认框自动点“确认”
    win.on_ask = lambda prompt: (setattr(win.worker, "_answer", True),
                                 win.worker._ask_event.set())

    # 3) 同步接线：mock run_sync，验证账号透传与日志
    calls = {}
    tasks.run_sync = lambda mc, ac, *, report, confirm, **kw: (
        calls.__setitem__("sync", {"main": mc[:14], "alt": ac[:14]}),
        report("log", "（mock）同步流程已调用"),
        confirm("确认开始同步？", ("y",)),
        {"ok": 1, "fail": 0, "skip": 0, "attempts": 1})[-1]
    win.sync_btn.click()
    assert _wait_worker(win, app)
    assert calls["sync"] == {"main": "DedeUserID=111", "alt": "DedeUserID=222"}, calls
    assert "（mock）同步流程已调用" in win.log_view.toPlainText()

    # 4) 清理接线：保护开关取值透传
    tasks.run_clean = lambda mc, ac, *, report, confirm, protect_special=True, **kw: (
        calls.__setitem__("clean", protect_special),
        {"ok": 0, "fail": 0, "skip": 0, "attempts": 0})[-1]
    win.protect_chk.setChecked(False)
    win.clean_btn.click()
    assert _wait_worker(win, app) and calls["clean"] is False
    win.protect_chk.setChecked(True)
    win.clean_btn.click()
    assert _wait_worker(win, app) and calls["clean"] is True

    # 5) 备份接线（mock 掉文件对话框）
    tasks.run_backup = lambda cookie, out_dir, *, report, label="账号", include_tags=False: (
        calls.__setitem__("backup", {"dir": out_dir, "tags": include_tags}), out_dir)[-1]
    QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: "C:\\temp-uitest")
    win.backup_btn.click()
    assert _wait_worker(win, app)
    assert calls["backup"] == {"dir": "C:\\temp-uitest", "tags": True}

    # 6) 设置弹窗：控件改动立即落盘 + 截图
    box = SettingsBox(win, win.config, lambda: None)
    assert box.grab().save(os.path.join(shots_dir, "settings.png"))
    box.interval_spin.setValue(2.5)
    box.limit_spin.setValue(123)
    assert abs(win.config.data["interval_s"] - 2.5) < 1e-6
    assert win.config.data["daily_limit"] == 123

    # 7) 头像占位渲染
    assert not win.card_main.avatar.pixmap().isNull()

    # 8) 扫码弹窗：mock 网络，验证二维码尺寸完整
    bili.qr_generate = lambda: {"qrcode_key": "k" * 32,
                                "url": "https://passport.bilibili.com/x?test=1"}
    bili.qr_poll = lambda key: {"code": 86101, "state": "waiting",
                                "cookie": None, "uname": None}
    qrbox = QrLoginBox(win, "主号")
    qrbox.worker = LoginWorker(qrbox)
    qrbox.worker.qrReadySig.connect(qrbox.show_qr)
    qrbox.worker.statusSig.connect(qrbox.status_label.setText)
    qrbox.worker.successSig.connect(qrbox.on_success)
    qrbox.worker.failSig.connect(qrbox.on_fail)
    qrbox.worker.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        app.processEvents()
        pix = qrbox.qr_label.pixmap()
        if pix and not pix.isNull():
            break
        time.sleep(0.05)
    pix = qrbox.qr_label.pixmap()
    assert pix and not pix.isNull() and 50 <= pix.width() <= 300
    qrbox.grab().save(os.path.join(shots_dir, "qr_dialog.png"))
    qrbox.worker.stop_flag = True
    qrbox.worker.wait(6000)

    print(f"uitest OK ({APP_VERSION})，截图: {shots_dir}")
    return 0


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    sys.excepthook = _excepthook
    # 重定向到文件/管道时 Windows 默认用 charmap 编码，中文会炸，强制 UTF-8
    for _stream in (sys.stdout, sys.stderr):
        if _stream and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    _code = 0
    try:
        if "--selftest" in sys.argv[1:]:
            _code = selftest()
        elif "--uitest" in sys.argv[1:]:
            _code = uitest()
        else:
            main()
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        diag_log("FATAL", traceback.format_exc())
        _code = 1
    sys.exit(_code)
