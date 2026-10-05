# -*- coding: utf-8 -*-
"""
bilibili-follow-sync CLI 版：把主号的关注列表同步到小号（只新增，不取关）

同步时会把主号的关注分组（特别关注/自定义分组）一并保留到小号。
业务逻辑在 core/ 包里（与桌面版共用），本文件只是命令行入口，因此需要
下载整个仓库运行（或 Releases 里的桌面版，扫码即用更省事）。

使用步骤：
1. pip install requests
2. 浏览器登录主号 -> F12 打开开发者工具 -> 网络(Network) -> 随便刷新一下页面 ->
   点任意一个 bilibili.com 的请求 -> 请求标头里复制整串 Cookie，
   粘贴到下面 MAIN_COOKIE（引号里面）
3. 同样方法登录小号，复制整串 Cookie 粘贴到 ALT_COOKIE
4. 运行：python cli/bili_sync_follows.py
5. 双向同步（两边互相补齐）：把 MAIN_COOKIE / ALT_COOKIE 内容互换再跑一次即可
6. 反向清理（可选，危险）：python cli/bili_sync_follows.py --clean
   取关小号上主号没有的关注；默认保护小号的特别关注（加 --no-protect 关闭保护）
7. 定时备份：python cli/bili_sync_follows.py --backup [--account alt]
   导出关注列表 JSON 到 cli/backups/；配合 Windows 任务计划程序可每天自动备份
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import bilibili as bili
from core import tasks

MAIN_COOKIE = "把主号的整串Cookie粘到这里"
ALT_COOKIE = "把小号的整串Cookie粘到这里"

# B站软配额约300次关注/天，超限会触发风控拦截(22015)，次日恢复
DAILY_LIMIT = tasks.DAILY_LIMIT_DEFAULT


def _report(kind, text):
    print(text)


def _confirm(prompt, accept):
    return input(prompt).strip().lower() in accept


def main():
    args = sys.argv[1:]
    try:
        if "--clean" in args:
            tasks.run_clean(
                MAIN_COOKIE, ALT_COOKIE,
                report=_report, confirm=_confirm, daily_limit=DAILY_LIMIT,
                protect_special="--no-protect" not in args,
                protect_hint="（加 --no-protect 可关闭保护）",
            )
        elif "--backup" in args:
            use_alt = "--account" in args and "alt" in args
            tasks.run_backup(ALT_COOKIE if use_alt else MAIN_COOKIE,
                             os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups"),
                             report=_report, label="小号" if use_alt else "主号")
        else:
            tasks.run_sync(MAIN_COOKIE, ALT_COOKIE,
                           report=_report, confirm=_confirm, daily_limit=DAILY_LIMIT)
    except tasks.FlowCancelled as e:
        print(e)
    except bili.BiliError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
