# -*- coding: utf-8 -*-
"""B站 HTTP API 封装：登录态 / 关注列表 / 关注分组 / 关注与取关 / 扫码登录。

错误码约定（详见 docs/risk-control-notes.md）：
- 22013 账号已注销、40061 用户不存在 → 永久跳过
- 22015 / -412 / -352 → 风控拦截（疑似当日配额用尽）
- -101 未登录 / -111 csrf 校验失败

端点依据 bilibili-API-collect 原文档（上游已关停，核对自社区 fork 存档），
端点与参数变更记录同样见 docs/risk-control-notes.md。
"""

import re
import time

import requests

API = "https://api.bilibili.com"
PASSPORT_API = "https://passport.bilibili.com"

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

# 关注分组 tagid 约定：0=默认分组，-10=特别关注；-1=悄悄关注（不在分组体系内，防御性跳过）
TAG_DEFAULT, TAG_SECRET, TAG_SPECIAL = 0, -1, -10

RISK_CODES = frozenset({22015, -412, -352})
PERMANENT_CODES = frozenset({22013, 40061})
NOT_LOGIN = -101


class BiliError(Exception):
    """需要中止流程的错误（Cookie 失效、参数缺失、接口不可用等）"""


# ================= Cookie 工具 =================

def clean_cookie(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:]
    return raw.replace("\n", "").replace("\r", "").strip()


def get_uid(cookie: str, label: str = "账号") -> str:
    m = re.search(r"DedeUserID=(\d+)", cookie)
    if not m:
        raise BiliError(f"{label}的Cookie里没找到 DedeUserID（用户ID），请确认复制的是完整Cookie。")
    return m.group(1)


def get_csrf(cookie: str, label: str = "账号") -> str:
    m = re.search(r"bili_jct=([^;]+)", cookie)
    if not m:
        raise BiliError(f"{label}的Cookie里没找到 bili_jct（csrf令牌），请确认复制的是完整Cookie。")
    return m.group(1)


def to_json(resp: requests.Response, what: str) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {"code": -1, "message": f"{what}返回非JSON（HTTP {resp.status_code}，可能被风控拦截）"}


def _space_headers(cookie: str, uid: str = "") -> dict:
    referer = f"https://space.bilibili.com/{uid}/fans/follow" if uid else "https://space.bilibili.com/"
    return dict(BASE_HEADERS, Cookie=cookie, Referer=referer)


# ================= 登录态 / 关注列表 =================

def get_nav(cookie: str) -> dict:
    """返回 {'mid': str, 'uname': str, 'face': str}；未登录返回 None"""
    resp = requests.get(f"{API}/x/web-interface/nav",
                        headers=dict(BASE_HEADERS, Cookie=cookie), timeout=15)
    data = to_json(resp, "获取登录信息")
    if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
        return {"mid": str(data["data"]["mid"]), "uname": data["data"]["uname"],
                "face": data["data"].get("face") or ""}
    return None


def get_followings(cookie: str, uid: str, on_page=None) -> dict:
    """拉取某账号的全部关注，返回 {mid(str): 用户名}；on_page(已拉取数, 总数) 用于进度"""
    headers = _space_headers(cookie, uid)
    result = {}
    pn = 1
    while True:
        resp = requests.get(
            f"{API}/x/relation/followings",
            params={"vmid": uid, "pn": pn, "ps": 50},
            headers=headers,
            timeout=15,
        )
        data = to_json(resp, "获取关注列表")
        if data.get("code") == NOT_LOGIN:
            raise BiliError("Cookie已失效（未登录），请重新登录或复制该账号的Cookie。")
        if data.get("code") != 0:
            raise BiliError(f"获取关注列表失败: code={data.get('code')} msg={data.get('message')}")
        users = data["data"]["list"]
        if not users:
            break
        for u in users:
            result[str(u["mid"])] = u["uname"]
        if on_page:
            on_page(len(result), data["data"].get("total") or 0)
        if len(result) >= (data["data"].get("total") or 0):
            break
        pn += 1
        time.sleep(0.4)
    return result


def get_followings_total(cookie: str, uid: str) -> int:
    """只取关注总数（第一页即含 total，用于界面展示，不翻页）"""
    resp = requests.get(
        f"{API}/x/relation/followings",
        params={"vmid": uid, "pn": 1, "ps": 1},
        headers=_space_headers(cookie, uid),
        timeout=15,
    )
    data = to_json(resp, "获取关注数")
    if data.get("code") == NOT_LOGIN:
        raise BiliError("Cookie已失效（未登录），请重新登录或复制该账号的Cookie。")
    if data.get("code") != 0:
        raise BiliError(f"获取关注数失败: code={data.get('code')} msg={data.get('message')}")
    return data.get("data", {}).get("total") or 0


# ================= 关注分组 =================

def get_relation_tags(cookie: str, uid: str) -> list:
    """拉取账号的关注分组列表，返回 [{tagid, name, count}, ...]"""
    resp = requests.get(
        f"{API}/x/relation/tags",
        headers=_space_headers(cookie, uid),
        timeout=15,
    )
    data = to_json(resp, "获取关注分组列表")
    if data.get("code") == NOT_LOGIN:
        raise BiliError("Cookie已失效（未登录），请重新登录或复制该账号的Cookie。")
    if data.get("code") != 0:
        raise BiliError(f"获取关注分组列表失败: code={data.get('code')} msg={data.get('message')}")
    return data.get("data") or []


def get_tag_members(cookie: str, uid: str, tagid: int) -> set:
    """拉取某分组内全部用户 mid（接口无 total，返回空页即到底）"""
    headers = _space_headers(cookie, uid)
    mids = set()
    pn = 1
    while True:
        resp = requests.get(
            f"{API}/x/relation/tag",
            params={"tagid": tagid, "pn": pn, "ps": 50},
            headers=headers,
            timeout=15,
        )
        data = to_json(resp, "获取分组成员")
        if data.get("code") != 0:
            raise RuntimeError(
                f"获取分组({tagid})成员失败: code={data.get('code')} msg={data.get('message')}"
            )
        users = data.get("data") or []
        for u in users:
            mids.add(str(u["mid"]))
        if not users or pn > 200:
            break
        pn += 1
        time.sleep(0.4)
    return mids


def create_tag(cookie: str, csrf: str, uid: str, name: str) -> dict:
    resp = requests.post(
        f"{API}/x/relation/tag/create",
        data={"tag": name, "csrf": csrf},
        headers=_space_headers(cookie, uid),
        timeout=15,
    )
    return to_json(resp, "创建分组")


def add_users_to_tags(cookie: str, csrf: str, uid: str, fid: str, tagids) -> dict:
    resp = requests.post(
        f"{API}/x/relation/tags/addUsers",
        data={"fids": str(fid), "tagids": ",".join(str(t) for t in tagids), "csrf": csrf},
        headers=_space_headers(cookie, uid),
        timeout=15,
    )
    return to_json(resp, "归组请求")


# ================= 关注 / 取关 =================

def modify_relation(cookie: str, csrf: str, mid: str, act: int) -> dict:
    """操作用户关系：act=1 关注，act=2 取关（POST /x/relation/modify）"""
    headers = dict(BASE_HEADERS, Cookie=cookie,
                   Referer="https://space.bilibili.com/",
                   Origin="https://space.bilibili.com")
    resp = requests.post(
        f"{API}/x/relation/modify",
        data={"fid": mid, "act": act, "re_src": 11, "csrf": csrf},
        headers=headers,
        timeout=15,
    )
    return to_json(resp, "关注/取关请求")


def follow_one(cookie: str, csrf: str, mid: str) -> dict:
    return modify_relation(cookie, csrf, mid, 1)


def unfollow_one(cookie: str, csrf: str, mid: str) -> dict:
    return modify_relation(cookie, csrf, mid, 2)


# ================= 扫码登录 =================

def qr_generate() -> dict:
    """生成扫码登录二维码，返回 {'qrcode_key': str, 'url': str}（url 用于渲染二维码）"""
    # 必须带浏览器 UA，裸 requests UA 会被 passport 接口以 HTTP 412 拦截
    resp = requests.get(f"{PASSPORT_API}/x/passport-login/web/qrcode/generate",
                        headers=BASE_HEADERS, timeout=15)
    data = to_json(resp, "生成登录二维码")
    if data.get("code") != 0:
        raise BiliError(f"生成登录二维码失败: code={data.get('code')} msg={data.get('message')}")
    return {"qrcode_key": data["data"]["qrcode_key"], "url": data["data"]["url"]}


def qr_poll(qrcode_key: str) -> dict:
    """轮询扫码状态。

    返回 {'code': int, 'state': str, 'cookie': str|None, 'uname': str|None}：
    - 0     成功，cookie 为可直接使用的整串 Cookie
    - 86038 二维码已失效（调用方应重新 generate）
    - 86090 已扫码未确认
    - 86101 未扫码
    """
    resp = requests.get(
        f"{PASSPORT_API}/x/passport-login/web/qrcode/poll",
        params={"qrcode_key": qrcode_key},
        headers=BASE_HEADERS,
        timeout=15,
    )
    data = to_json(resp, "轮询扫码状态")
    inner = data.get("data") or {}
    code = inner.get("code")
    state = {0: "success", 86038: "expired", 86090: "scanned", 86101: "waiting"}.get(code, "unknown")
    cookie = None
    if code == 0:
        cookie = "; ".join(f"{k}={v}" for k, v in resp.cookies.items())
        if "SESSDATA" not in cookie:
            raise BiliError("扫码成功但未取到 SESSDATA，请重试。")
    return {"code": code, "state": state, "cookie": cookie, "uname": inner.get("uname")}
