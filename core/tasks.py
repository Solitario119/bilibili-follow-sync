# -*- coding: utf-8 -*-
"""同步 / 反向清理 / 备份 流程（CLI 与桌面版共用）。

所有流程通过回调与调用方交互，不直接 print / input：
- report(kind, text)：kind="log" 日志行、"progress" 阶段进度
- confirm(prompt, accept)：确认对话框，accept 为可接受的答复集合（小写）
- should_stop()：返回 True 则在下一个用户前停止（断点续跑语义不变）
- used_today / on_attempt：跨运行的每日尝试计数（桌面版持久化，CLI 恒为 0）

CLI 和桌面版共用同一套风控策略与输出文案。
"""

import json
import os
import random
import time

from . import bilibili as bili

DAILY_LIMIT_DEFAULT = 290


class FlowCancelled(Exception):
    """用户取消流程"""


def _report_all(report, text):
    report("log", text)


def _sleep(interval):
    """interval=None 走 CLI 默认 2.5~3.5s；数值则按基准 ±15% 抖动"""
    if interval is None:
        time.sleep(random.uniform(2.5, 3.5))
    else:
        time.sleep(max(0.3, interval * random.uniform(0.85, 1.15)))


def _fetch_group_map(cookie, uid, report, context="同步"):
    """拉主号分组成员映射 mid -> [分组名]；失败降级为空映射"""
    mid_tags = {}
    try:
        for t in bili.get_relation_tags(cookie, uid):
            if t.get("tagid") in (bili.TAG_DEFAULT, bili.TAG_SECRET) or not t.get("count"):
                continue
            for mid in bili.get_tag_members(cookie, uid, t["tagid"]):
                mid_tags.setdefault(mid, []).append(t["name"])
        grouped = sum(1 for v in mid_tags.values() if v)
        custom_cnt = len({n for v in mid_tags.values() for n in v} - {"特别关注"})
        report("log", f"主号分组：{grouped} 人有特别关注/自定义分组，自定义分组 {custom_cnt} 个。")
    except bili.BiliError:
        raise
    except Exception as e:
        report("log", f"⚠️ 分组信息拉取失败（{e}），本次{context}不处理分组。")
        mid_tags = {}
    return mid_tags


def _load_alt_tags(cookie, uid, report):
    """读小号分组名 -> tagid；失败降级为空（归组时按需创建）"""
    report("log", "正在读取小号的关注分组 ...")
    alt_tags_by_name = {}
    try:
        alt_tags_by_name = {t["name"]: t["tagid"] for t in bili.get_relation_tags(cookie, uid)}
    except bili.BiliError:
        raise
    except Exception as e:
        report("log", f"⚠️ 读取小号分组列表失败（{e}），归组时将按需创建同名分组。")
    return alt_tags_by_name


def apply_groups(alt_cookie, alt_csrf, alt_uid, mid, names, alt_tags_by_name, report):
    """把用户归入指定分组（按名匹配小号分组，缺则创建同名分组），失败只告警不中断"""
    tagids = []
    for gname in names:
        tagid = alt_tags_by_name.get(gname)
        if tagid is None and gname == "特别关注":
            tagid = bili.TAG_SPECIAL
        if tagid is None:
            r = bili.create_tag(alt_cookie, alt_csrf, alt_uid, gname)
            if r.get("code") == 0 and (r.get("data") or {}).get("tagid") is not None:
                tagid = r["data"]["tagid"]
                alt_tags_by_name[gname] = tagid
                report("log", f"    🆕 已创建分组「{gname}」")
            elif r.get("code") == 22106:  # 分组已存在但未返回 id：刷新后按名重取
                try:
                    alt_tags_by_name.clear()
                    alt_tags_by_name.update(
                        {t["name"]: t["tagid"] for t in bili.get_relation_tags(alt_cookie, alt_uid)}
                    )
                    tagid = alt_tags_by_name.get(gname)
                except Exception:
                    tagid = None
                if tagid is None:
                    report("log", f"    ⚠️ 分组「{gname}」已存在但未获取到 id，跳过该分组")
                    continue
            else:
                report("log", f"    ⚠️ 创建分组「{gname}」失败 code={r.get('code')}，跳过该分组")
                continue
        tagids.append(tagid)
    if not tagids:
        return
    time.sleep(0.4)
    g = bili.add_users_to_tags(alt_cookie, alt_csrf, alt_uid, mid, tagids)
    if g.get("code") == 0:
        report("log", f"    📁 已归入：{'、'.join(names)}")
    else:
        report("log", f"    ⚠️ 归组失败 code={g.get('code')} msg={g.get('message')}")


def _run_write_loop(todo, write_fn, report, *, used_today, on_attempt,
                    daily_limit, interval, should_stop, unit_ok, skip_codes_msg,
                    risk_msgs, stop_msg):
    """关注/取关共用的写循环：风控码自动停止、每日上限、断点续跑"""
    ok = fail = skip = 0
    attempts = used_today
    for i, item in enumerate(todo, 1):
        mid = item["mid"]
        name = item.get("uname", mid)
        if should_stop and should_stop():
            report("log", stop_msg)
            break
        if attempts >= daily_limit:
            report("log", f"🛑 已达今日尝试上限({daily_limit})，明天继续。")
            break
        attempts += 1
        if on_attempt:
            on_attempt()
        res = write_fn(mid)
        if res.get("code") == 0:
            ok += 1
            report("log", f"[{i}/{len(todo)}] {unit_ok} {name}({mid})")
        elif res.get("code") in bili.RISK_CODES:
            report("log", f"[{i}/{len(todo)}] {risk_msgs[0]}")
            report("log", risk_msgs[1])
            break
        elif res.get("code") in bili.PERMANENT_CODES:
            skip += 1
            report("log", f"[{i}/{len(todo)}] {skip_codes_msg}：{name}({mid})")
        else:
            fail += 1
            report("log", f"[{i}/{len(todo)}] 失败 {name}({mid}): "
                          f"code={res.get('code')} msg={res.get('message')}")
        _sleep(interval)
    return {"ok": ok, "fail": fail, "skip": skip, "attempts": attempts}


def run_sync(main_cookie, alt_cookie, *, report, confirm,
             daily_limit=DAILY_LIMIT_DEFAULT, interval=None,
             used_today=0, on_attempt=None, should_stop=None):
    """同步：主号关注 → 小号（只新增，自动归组）"""
    main_cookie = bili.clean_cookie(main_cookie)
    alt_cookie = bili.clean_cookie(alt_cookie)
    if main_cookie == alt_cookie:
        raise bili.BiliError("主号和小号的Cookie完全相同，请检查是否分别粘贴了两个账号的Cookie。")
    main_uid = bili.get_uid(main_cookie, "主号")
    alt_uid = bili.get_uid(alt_cookie, "小号")
    alt_csrf = bili.get_csrf(alt_cookie, "小号")

    report("log", f"正在拉取主号({main_uid})的关注列表 ...")
    main_follows = bili.get_followings(main_cookie, main_uid)
    report("log", f"主号共关注 {len(main_follows)} 人。")

    report("log", "正在拉取主号的关注分组 ...")
    mid_tags = _fetch_group_map(main_cookie, main_uid, report, context="同步")

    report("log", f"正在拉取小号({alt_uid})的关注列表 ...")
    alt_follows = bili.get_followings(alt_cookie, alt_uid)
    report("log", f"小号已关注 {len(alt_follows)} 人。")
    alt_tags_by_name = _load_alt_tags(alt_cookie, alt_uid, report)

    todo = {mid: name for mid, name in main_follows.items() if mid not in alt_follows}
    report("log", f"需要在小号新关注的用户共 {len(todo)} 人。")
    need_group = sum(1 for m in todo if mid_tags.get(m))
    if need_group:
        report("log", f"其中 {need_group} 人需归入特别关注/自定义分组。")
    if not todo:
        report("log", "小号已经和主号一致，无需操作。")
        return {"ok": 0, "fail": 0, "skip": 0, "attempts": used_today}

    limit_now = max(0, daily_limit - used_today)
    if len(todo) > limit_now:
        if limit_now == 0:
            report("log", f"🛑 今日尝试额度已用完({daily_limit})，明天再来。")
            return {"ok": 0, "fail": 0, "skip": 0, "attempts": used_today}
        report("log", f"注意：待关注人数({len(todo)})超过每日安全上限({daily_limit})，"
                      f"本次只处理前 {limit_now} 人，明天重跑本脚本可自动续跑。")
        todo = dict(list(todo.items())[:limit_now])

    if not confirm(f"确认开始在小号上关注这 {len(todo)} 人吗？(y/N) ", ("y",)):
        raise FlowCancelled("已取消。")

    todo_items = [{"mid": mid, "uname": name, "names": mid_tags.get(mid) or []}
                  for mid, name in todo.items()]
    alt_uid_ref = alt_uid

    ok = fail = skip = 0
    attempts = used_today
    for i, item in enumerate(todo_items, 1):
        mid = item["mid"]
        if should_stop and should_stop():
            report("log", "⏹ 已手动停止，明天重新同步即可断点续跑（已关注的会自动跳过）。")
            break
        if attempts >= daily_limit:
            report("log", f"🛑 已达今日尝试上限({daily_limit})，明天继续。")
            break
        attempts += 1
        if on_attempt:
            on_attempt()
        res = bili.follow_one(alt_cookie, alt_csrf, mid)
        if res.get("code") == 0:
            ok += 1
            report("log", f"[{i}/{len(todo)}] 已关注 {item['uname']}({mid})")
            if item["names"]:
                apply_groups(alt_cookie, alt_csrf, alt_uid_ref, mid,
                             item["names"], alt_tags_by_name, report)
        elif res.get("code") in bili.RISK_CODES:
            report("log", f"[{i}/{len(todo)}] 触发风控拦截，疑似今日配额已用完。")
            report("log", "已自动停止；明天重跑本脚本即可断点续跑（已关注的会自动跳过）。")
            break
        elif res.get("code") == 22013:
            skip += 1
            fail += 1
            report("log", f"[{i}/{len(todo)}] 跳过（账号已注销）：{item['uname']}({mid})")
        else:
            fail += 1
            report("log", f"[{i}/{len(todo)}] 失败 {item['uname']}({mid}): "
                          f"code={res.get('code')} msg={res.get('message')}")
        _sleep(interval)

    report("log", f"\n完成：成功 {ok}，失败/跳过 {fail}。")
    return {"ok": ok, "fail": fail, "skip": skip, "attempts": attempts}


def run_clean(main_cookie, alt_cookie, *, report, confirm,
              daily_limit=DAILY_LIMIT_DEFAULT, protect_special=True,
              protect_hint="", rerun_cmd=" --clean",
              interval=None, used_today=0, on_attempt=None, should_stop=None):
    """反向清理：取关小号上主号没有的关注（预览 + 确认 + 默认保护特别关注）

    protect_hint/rerun_cmd 用于让 CLI/GUI 各自输出贴切的提示文案。
    """
    main_cookie = bili.clean_cookie(main_cookie)
    alt_cookie = bili.clean_cookie(alt_cookie)
    if main_cookie == alt_cookie:
        raise bili.BiliError("主号和小号的Cookie完全相同，请检查是否分别粘贴了两个账号的Cookie。")
    main_uid = bili.get_uid(main_cookie, "主号")
    alt_uid = bili.get_uid(alt_cookie, "小号")
    alt_csrf = bili.get_csrf(alt_cookie, "小号")

    report("log", "正在拉取主号关注列表 ...")
    main_follows = bili.get_followings(main_cookie, main_uid)
    report("log", f"主号共关注 {len(main_follows)} 人。")
    report("log", f"正在拉取小号({alt_uid})最新关注列表 ...")
    alt_follows = bili.get_followings(alt_cookie, alt_uid)
    report("log", f"小号共关注 {len(alt_follows)} 人。")

    protect = set()
    if protect_special:
        report("log", "正在拉取小号的特别关注名单（默认保护，不参与取关）...")
        try:
            for t in bili.get_relation_tags(alt_cookie, alt_uid):
                if t.get("tagid") == bili.TAG_SPECIAL:
                    protect = bili.get_tag_members(alt_cookie, alt_uid, bili.TAG_SPECIAL)
                    break
            report("log", f"特别关注 {len(protect)} 人将被保护{protect_hint}。")
        except bili.BiliError:
            raise
        except Exception as e:
            report("log", f"⚠️ 特别关注名单拉取失败（{e}），保护未生效。")

    extras = [m for m in alt_follows if m not in main_follows and m not in protect]
    if not extras:
        report("log", "没有多余关注，无需清理。")
        return {"ok": 0, "fail": 0, "skip": 0, "attempts": used_today}

    limit_now = max(0, daily_limit - used_today)
    if len(extras) > limit_now:
        if limit_now == 0:
            report("log", f"🛑 今日尝试额度已用完({daily_limit})，明天再来。")
            return {"ok": 0, "fail": 0, "skip": 0, "attempts": used_today}
        report("log", f"注意：待取关人数({len(extras)})超过每日安全上限({daily_limit})，"
                      f"本次只处理前 {limit_now} 人，明天重跑可自动续跑。")
        extras = extras[:limit_now]

    report("log", f"\n待取关 {len(extras)} 人：")
    for mid in extras:
        report("log", f"  - {alt_follows[mid]}({mid})")
    report("log", "\n⚠️ 取关后需手动重新关注才能恢复，特别关注标志与分组可能丢失。")

    if not confirm(f"确认取关这 {len(extras)} 人吗？输入 yes 执行：", ("yes",)):
        raise FlowCancelled("已取消。")

    todo_items = [{"mid": mid, "uname": alt_follows[mid]} for mid in extras]
    summary = _run_write_loop(
        todo_items,
        lambda mid: bili.unfollow_one(alt_cookie, alt_csrf, mid),
        report,
        used_today=used_today, on_attempt=on_attempt, daily_limit=daily_limit,
        interval=interval, should_stop=should_stop,
        unit_ok="已取关",
        skip_codes_msg="跳过（账号已注销/不存在，关系将保留）",
        risk_msgs=("触发风控拦截，疑似今日配额已用完，已自动停止。",
                   f"明天重新运行{rerun_cmd}即可断点续跑（已取关的会自动跳过）。"),
        stop_msg=f"⏹ 已手动停止，剩余用户未处理；明天重新运行{rerun_cmd}即可断点续跑。",
    )
    report("log", f"\n完成：取关 {summary['ok']}，跳过 {summary['skip']}，失败 {summary['fail']}。")
    return summary


def run_backup(cookie, out_dir, *, report, label="账号", include_tags=False):
    """备份账号关注列表为 JSON（include_tags=True 导出 v2 含分组）"""
    cookie = bili.clean_cookie(cookie)
    uid = bili.get_uid(cookie, label)

    report("log", f"正在拉取{label}({uid})的关注列表 ...")
    follows = bili.get_followings(cookie, uid)

    version, tags_note = 1, ""
    mid_tags = {}
    if include_tags:
        report("log", "正在拉取分组信息 ...")
        mid_tags = _fetch_group_map(cookie, uid, report, context="备份")
        version, tags_note = 2, "（含分组）"

    payload = {
        "type": "bilibili-follow-sync",
        "version": version,
        "exportedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "account": {"mid": int(uid), "uname": ""},
        "total": len(follows),
        "users": [{"mid": int(mid), "uname": name,
                   **({"tags": mid_tags.get(mid, [])} if include_tags else {})}
                  for mid, name in follows.items()],
    }
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"bilibili-follows-{uid}-{time.strftime('%Y-%m-%d')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    report("log", f"✅ 已备份 {len(follows)} 人{tags_note} -> {path}")
    return path
