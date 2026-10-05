// ==UserScript==
// @name         B站关注同步 (Bilibili Follow Sync)
// @namespace    https://github.com/Solitario119/bilibili-follow-sync
// @version      0.1.0
// @description  在B站"我的关注"页导出当前账号的关注列表为JSON备份（含特别关注/自定义分组）；在另一个账号导入该文件并自动关注缺失的用户、按名归入分组（主号→小号同步）；可选「反向清理」取关小号上多余的关注；每天首次打开页面自动备份到浏览器本地。内置限速、风控自适应退避、断点续跑与每日配额保护。
// @author       Solitario119
// @match        https://space.bilibili.com/*/relation/follow*
// @match        https://space.bilibili.com/*/fans/follow*
// @updateURL    https://raw.githubusercontent.com/Solitario119/bilibili-follow-sync/master/userscript/bili-follow-sync.user.js
// @downloadURL  https://raw.githubusercontent.com/Solitario119/bilibili-follow-sync/master/userscript/bili-follow-sync.user.js
// @grant        none
// @license      MIT
// ==/UserScript==

(function () {
  "use strict";

  // ================= 配置 =================
  const API = "https://api.bilibili.com";
  // 风控类错误码（实测）：22015 风控拦截；-412/-352 同类
  const RISK_CODES = new Set([22015, -412, -352]);
  // 永久失败错误码：22013 账号已注销
  const PERMANENT_CODES = new Set([22013]);
  const DAILY_LIMIT_DEFAULT = 290; // B站软配额约300次/天，默认留一点余量
  // 关注分组 tagid 约定：0=默认分组，-10=特别关注；-1=悄悄关注（不在分组体系内，防御性跳过）
  const TAG_DEFAULT = 0, TAG_SECRET = -1, TAG_SPECIAL = -10;

  // ================= 小工具 =================
  const SLEEP = (ms) => new Promise((r) => setTimeout(r, ms));
  const rand = (a, b) => a + Math.random() * (b - a);

  function getCookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : null;
  }

  function todayKey() {
    return "bfs_daily_" + new Date().toISOString().slice(0, 10);
  }
  function getDailyCount() {
    return parseInt(localStorage.getItem(todayKey()) || "0", 10);
  }
  function bumpDailyCount() {
    localStorage.setItem(todayKey(), String(getDailyCount() + 1));
  }

  // ================= API =================
  async function apiNav() {
    const r = await fetch(API + "/x/web-interface/nav", { credentials: "include" });
    const j = await r.json();
    if (j.code !== 0 || !j.data || !j.data.isLogin) return null;
    return { mid: String(j.data.mid), uname: j.data.uname };
  }

  async function fetchFollowings(mid, onPage) {
    const out = new Map(); // mid -> {uname, special}
    let pn = 1;
    for (;;) {
      const r = await fetch(
        API + "/x/relation/followings?vmid=" + mid + "&pn=" + pn + "&ps=50",
        { credentials: "include" }
      );
      const j = await r.json();
      if (j.code !== 0) throw new Error("拉取关注列表失败 code=" + j.code + " " + j.message);
      const list = (j.data && j.data.list) || [];
      for (const u of list) out.set(String(u.mid), { uname: u.uname, special: !!u.special });
      if (onPage) onPage(out.size, (j.data && j.data.total) || 0);
      if (list.length === 0 || out.size >= ((j.data && j.data.total) || 0)) break;
      pn += 1;
      await SLEEP(400);
    }
    return out;
  }

  // ================= 关注分组 API =================
  // 当前登录账号的分组列表 [{tagid, name, count, ...}]
  async function fetchTagList() {
    const r = await fetch(API + "/x/relation/tags", { credentials: "include" });
    const j = await r.json();
    if (j.code !== 0) throw new Error("拉取分组列表失败 code=" + j.code + " " + j.message);
    return j.data || [];
  }

  // 拉取某分组下全部用户 mid（分页；接口无 total，返回空页即到底）
  async function fetchTagMembers(tagid, name) {
    const mids = [];
    let pn = 1;
    for (;;) {
      const r = await fetch(
        API + "/x/relation/tag?tagid=" + tagid + "&pn=" + pn + "&ps=50",
        { credentials: "include" }
      );
      const j = await r.json();
      if (j.code !== 0) throw new Error("拉取分组「" + name + "」成员失败 code=" + j.code + " " + j.message);
      const list = Array.isArray(j.data) ? j.data : [];
      for (const u of list) mids.push(String(u.mid));
      if (list.length === 0 || pn > 200) break;
      pn += 1;
      await SLEEP(400);
    }
    return mids;
  }

  // 当前登录账号全部关注的分组归属：mid -> [分组名]（仅特别关注与自定义分组）
  // 任一步失败即抛错，由调用方降级处理
  async function fetchFollowTagMap() {
    const map = new Map();
    const customNames = new Set();
    for (const t of await fetchTagList()) {
      if (t.tagid === TAG_DEFAULT || t.tagid === TAG_SECRET || !t.count) continue;
      setProgress("拉取分组「" + t.name + "」…");
      for (const mid of await fetchTagMembers(t.tagid, t.name)) {
        if (!map.has(mid)) map.set(mid, []);
        map.get(mid).push(t.name);
      }
      if (t.tagid !== TAG_SPECIAL) customNames.add(t.name);
      await SLEEP(400);
    }
    return { map, customNames };
  }

  // 当前登录账号的 分组名 -> tagid
  async function loadTagMapByName() {
    const m = new Map();
    for (const t of await fetchTagList()) m.set(t.name, t.tagid);
    return m;
  }

  async function createTag(name, csrf) {
    const r = await fetch(API + "/x/relation/tag/create", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "tag=" + encodeURIComponent(name) + "&csrf=" + encodeURIComponent(csrf),
    });
    try {
      return await r.json();
    } catch (e) {
      return { code: -1, message: "HTTP " + r.status };
    }
  }

  async function addUsersToTags(mid, tagids, csrf) {
    const r = await fetch(API + "/x/relation/tags/addUsers", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body:
        "fids=" + encodeURIComponent(String(mid)) +
        "&tagids=" + encodeURIComponent(tagids.join(",")) +
        "&csrf=" + encodeURIComponent(csrf),
    });
    try {
      return await r.json();
    } catch (e) {
      return { code: -1, message: "HTTP " + r.status };
    }
  }

  // 把刚关注的人按名归入小号分组（缺则创建）；返回日志后缀，失败只告警不中断
  async function applyGroups(item, csrf, altTags) {
    const names = item.tags || [];
    if (!names.length) return "";
    bumpDailyCount(); // 保守起见，归组写入同样计入每日尝试
    const tagids = [];
    for (const name of names) {
      let tagid = altTags.get(name);
      if (tagid === undefined && name === "特别关注") tagid = TAG_SPECIAL;
      if (tagid === undefined) {
        const r = await createTag(name, csrf);
        if (r.code === 0 && r.data && r.data.tagid != null) {
          tagid = r.data.tagid;
          altTags.set(name, tagid);
          log("🆕 已创建分组「" + name + "」");
        } else if (r.code === 22106) {
          // 分组已存在但未返回 id：刷新小号分组列表后按名重取
          try {
            altTags.clear();
            (await loadTagMapByName()).forEach((v, k) => altTags.set(k, v));
            tagid = altTags.get(name);
          } catch (e) {
            tagid = undefined;
          }
          if (tagid === undefined) {
            log("⚠️ 分组「" + name + "」已存在但未获取到 id，跳过该分组");
            continue;
          }
        } else {
          log("⚠️ 创建分组「" + name + "」失败 code=" + r.code + " " + (r.message || ""));
          continue;
        }
      }
      tagids.push(tagid);
    }
    if (!tagids.length) return "";
    const g = await addUsersToTags(item.mid, tagids, csrf);
    await SLEEP(300);
    if (g.code === 0) return "（已归入：" + names.join("、") + "）";
    if (RISK_CODES.has(g.code)) {
      log("⚠️ 归组被风控拦截 code=" + g.code + "，" + item.uname + " 分组未保留");
    } else {
      log("⚠️ 归组失败 " + item.uname + " code=" + g.code + " " + g.message);
    }
    return "";
  }

  async function modifyRelation(mid, act, csrf) {
    const r = await fetch(API + "/x/relation/modify", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body:
        "fid=" + encodeURIComponent(String(mid)) +
        "&act=" + act + "&re_src=11&csrf=" + encodeURIComponent(csrf),
    });
    let j;
    try {
      j = await r.json();
    } catch (e) {
      j = { code: -1, message: "HTTP " + r.status };
    }
    return j;
  }

  async function followOne(mid, csrf) {
    return modifyRelation(mid, 1, csrf);
  }

  // ================= UI =================
  const els = {};
  // job = { todo, index, ok, permFail, fail, riskStreak, riskTotal, running, stop, from }
  let job = null;

  function log(msg) {
    const div = document.createElement("div");
    div.textContent = msg;
    els.log.prepend(div);
    while (els.log.childElementCount > 200) els.log.removeChild(els.log.lastChild);
  }

  function setProgress(text) {
    els.progress.textContent = text;
  }

  function renderSummary() {
    if (!job) {
      els.summary.textContent = "";
      return;
    }
    const done = job.ok + job.permFail + job.fail;
    els.summary.textContent =
      "来源账号：" + (job.from ? job.from.uname + " (" + job.from.mid + ")" : "未知") +
      " · 待关注 " + job.todo.length + " 人 · 已处理 " + done + " 人";
  }

  function renderProgress(interval) {
    if (!job) return;
    setProgress(
      "进度 " + job.index + "/" + job.todo.length +
      " · 成功 " + job.ok +
      " · 注销跳过 " + job.permFail +
      " · 其他失败 " + job.fail +
      " · 间隔 " + interval + "ms" +
      " · 今日已尝试 " + getDailyCount()
    );
  }

  function buildPanel() {
    const style = document.createElement("style");
    style.textContent = [
      "#bfs-panel{position:fixed;right:16px;bottom:16px;width:340px;background:#fff;color:#222;",
      "border:1px solid #ddd;border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.15);",
      "font:12px/1.6 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;z-index:999999;}",
      "#bfs-head{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid #eee;}",
      "#bfs-head b{font-size:13px;}",
      "#bfs-user{flex:1;color:#666;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}",
      "#bfs-body{padding:10px 12px;}",
      ".bfs-row{display:flex;align-items:center;gap:8px;margin:6px 0;flex-wrap:wrap;}",
      "#bfs-panel button{cursor:pointer;border:1px solid #fb7299;background:#fb7299;color:#fff;",
      "border-radius:6px;padding:4px 10px;font-size:12px;}",
      "#bfs-panel button:disabled{opacity:.45;cursor:not-allowed;}",
      "#bfs-panel button.bfs-grey{background:#888;border-color:#888;}",
      "#bfs-panel button.bfs-danger{background:#e05252;border-color:#e05252;}",
      "#bfs-panel select,#bfs-panel input{font-size:12px;padding:2px 4px;}",
      "#bfs-progress{margin:6px 0;color:#555;min-height:18px;}",
      "#bfs-log{max-height:170px;overflow-y:auto;background:#f7f7f7;border-radius:6px;padding:6px 8px;}",
      "#bfs-log div{padding:1px 0;border-bottom:1px dashed #eee;word-break:break-all;}",
      ".bfs-tip{margin-top:8px;color:#999;line-height:1.5;}",
    ].join("");
    document.head.appendChild(style);

    const panel = document.createElement("div");
    panel.id = "bfs-panel";
    panel.innerHTML = [
      '<div id="bfs-head"><b>B站关注同步</b><span id="bfs-user">读取登录中…</span>',
      '<button id="bfs-toggle" title="收起/展开">−</button></div>',
      '<div id="bfs-body">',
      '<div class="bfs-row"><button id="bfs-export">① 导出本账号关注列表</button>',
      '<button id="bfs-bkhist" class="bfs-grey">📦 历史备份</button></div>',
      '<div class="bfs-row"><input type="file" id="bfs-file" accept=".json,application/json" style="display:none" />',
      '<button id="bfs-import">② 选择导出文件并比对</button></div>',
      '<div class="bfs-row">间隔 <select id="bfs-speed">',
      '<option value="3000">3秒（稳妥，推荐）</option>',
      '<option value="2000">2秒（较快）</option>',
      '<option value="1000">1秒（激进，易触发风控）</option>',
      '</select> 今日上限 <input type="number" id="bfs-limit" value="' + DAILY_LIMIT_DEFAULT + '" min="10" max="1000" style="width:64px" /></div>',
      '<div class="bfs-row" id="bfs-summary"></div>',
      '<div class="bfs-row"><button id="bfs-start" disabled>③ 开始同步</button>',
      '<button id="bfs-stop" class="bfs-grey" disabled>停止</button></div>',
      '<div class="bfs-row"><button id="bfs-clean" class="bfs-danger" disabled>④ 反向清理（取关多余）</button>',
      '<label style="color:#666"><input type="checkbox" id="bfs-protect" checked /> 保护特别关注</label></div>',
      '<div id="bfs-progress">待命</div>',
      '<div id="bfs-log"></div>',
      '<div class="bfs-tip">B站限制每天约300次关注操作，触发拦截后需次日恢复；',
      '同步可随时停止，重新导入同一文件即可断点续跑（已关注的会自动跳过）；',
      '每天首次打开本页面会自动备份关注列表到浏览器本地，点「📦 历史备份」可下载。</div>',
      "</div>",
    ].join("");
    document.body.appendChild(panel);

    els.user = panel.querySelector("#bfs-user");
    els.summary = panel.querySelector("#bfs-summary");
    els.progress = panel.querySelector("#bfs-progress");
    els.log = panel.querySelector("#bfs-log");
    els.export = panel.querySelector("#bfs-export");
    els.bkhist = panel.querySelector("#bfs-bkhist");
    els.import = panel.querySelector("#bfs-import");
    els.file = panel.querySelector("#bfs-file");
    els.speed = panel.querySelector("#bfs-speed");
    els.limit = panel.querySelector("#bfs-limit");
    els.start = panel.querySelector("#bfs-start");
    els.stop = panel.querySelector("#bfs-stop");
    els.clean = panel.querySelector("#bfs-clean");
    els.protect = panel.querySelector("#bfs-protect");

    panel.querySelector("#bfs-toggle").addEventListener("click", (ev) => {
      const body = panel.querySelector("#bfs-body");
      const hidden = body.style.display === "none";
      body.style.display = hidden ? "" : "none";
      ev.target.textContent = hidden ? "−" : "+";
    });

    els.export.addEventListener("click", doExport);
    els.bkhist.addEventListener("click", showBackupHistory);
    els.import.addEventListener("click", () => els.file.click());
    els.file.addEventListener("change", onImport);
    els.start.addEventListener("click", startSync);
    els.stop.addEventListener("click", () => {
      if (job) job.stop = true;
    });
    els.clean.addEventListener("click", startClean);
  }

  // ================= 导出 =================
  async function doExport() {
    const me = await apiNav();
    if (!me) {
      log("❌ 未登录，请先登录要导出的账号");
      return;
    }
    els.export.disabled = true;
    log("开始导出 " + me.uname + " (" + me.mid + ") 的关注列表…");
    try {
      const map = await fetchFollowings(me.mid, (n, total) =>
        setProgress("导出中 " + n + "/" + total)
      );
      // 分组信息：拉取失败自动降级为不含分组（靠 special 标记兜底特别关注）
      let tagInfo = null;
      try {
        tagInfo = await fetchFollowTagMap();
      } catch (e) {
        log("⚠️ 分组信息拉取失败（" + e.message + "），本次导出不含分组");
      }
      const users = [];
      let groupedCnt = 0;
      map.forEach((info, mid) => {
        let tags = tagInfo ? tagInfo.map.get(String(mid)) || [] : [];
        if (!tags.length && info.special) tags = ["特别关注"];
        if (tags.length) groupedCnt++;
        users.push({ mid: Number(mid), uname: info.uname, tags: tags });
      });
      const payload = {
        type: "bilibili-follow-sync",
        version: 2,
        exportedAt: new Date().toISOString(),
        account: { mid: Number(me.mid), uname: me.uname },
        total: users.length,
        users: users,
      };
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download =
        "bilibili-follows-" + me.mid + "-" + new Date().toISOString().slice(0, 10) + ".json";
      a.click();
      URL.revokeObjectURL(a.href);
      setProgress("导出完成：" + users.length + " 人");
      const groupNote = tagInfo
        ? "，含分组 " + groupedCnt + " 人、" + tagInfo.customNames.size + " 个自定义分组"
        : "（不含分组）";
      log("✅ 已导出 " + users.length + " 个关注" + groupNote + "，请保存好该JSON文件");
    } catch (e) {
      log("导出失败：" + e.message);
    } finally {
      els.export.disabled = false;
    }
  }

  // ================= 导入比对 =================
  async function onImport(ev) {
    const f = ev.target.files && ev.target.files[0];
    ev.target.value = "";
    if (!f) return;

    let data;
    try {
      data = JSON.parse(await f.text());
    } catch (e) {
      log("❌ 文件不是合法JSON");
      return;
    }
    if (!data || data.type !== "bilibili-follow-sync" || !Array.isArray(data.users)) {
      log("❌ 文件格式不对，应由本脚本的「导出」功能生成");
      return;
    }

    const me = await apiNav();
    if (!me) {
      log("❌ 未登录，请先登录要同步到的账号（小号）");
      return;
    }
    els.user.textContent = "已登录：" + me.uname + " (" + me.mid + ")";

    if (String(data.account && data.account.mid) === me.mid) {
      log("⚠️ 该文件来自当前登录账号自身，无需同步");
      return;
    }

    els.import.disabled = true;
    log("正在拉取当前账号(" + me.uname + ")的关注列表以比对…");
    try {
      const have = await fetchFollowings(me.mid, (n, t) =>
        setProgress("比对中 " + n + "/" + t)
      );
      const todo = data.users.filter((u) => !have.has(String(u.mid)));
      job = {
        todo: todo,
        fileMids: new Set(data.users.map((u) => String(u.mid))),
        index: 0,
        ok: 0,
        permFail: 0,
        fail: 0,
        riskStreak: 0,
        riskTotal: 0,
        running: false,
        stop: false,
        from: data.account,
      };
      renderSummary();
      els.clean.disabled = false; // 反向清理在比对后即可用（待关注为 0 也可能有多余关注）
      if (todo.length === 0) {
        setProgress("无需同步：当前账号已关注文件中的全部用户");
        log("✅ 比对完成，没有需要新增的关注");
      } else {
        setProgress("待关注 " + todo.length + " 人，点击「开始同步」");
        const needGroup = todo.filter((u) => u.tags && u.tags.length).length;
        log(
          "比对完成：文件共 " + data.users.length + " 人，已关注 " +
          (data.users.length - todo.length) + " 人，待关注 " + todo.length + " 人" +
          (needGroup ? "（其中 " + needGroup + " 人需归组）" : "")
        );
        els.start.disabled = false;
      }
    } catch (e) {
      log("比对失败：" + e.message);
    } finally {
      els.import.disabled = false;
    }
  }

  // ================= 批量关注 =================
  async function startSync() {
    if (!job || job.running || job.todo.length === 0) return;

    const csrf = getCookie("bili_jct");
    if (!csrf) {
      log("❌ 未获取到csrf(bili_jct)，请刷新页面后重试");
      return;
    }

    const base = parseInt(els.speed.value, 10);
    const dailyLimit = parseInt(els.limit.value, 10) || DAILY_LIMIT_DEFAULT;
    let interval = base;

    // 小号现有分组（名 -> tagid），用于把新关注的人归组
    let altTags = new Map();
    try {
      altTags = await loadTagMapByName();
    } catch (e) {
      log("⚠️ 读取小号分组列表失败（" + e.message + "），归组时将按需创建同名分组");
    }

    job.running = true;
    job.stop = false;
    els.start.disabled = true;
    els.stop.disabled = false;
    log("▶️ 开始同步，共 " + job.todo.length + " 人，间隔 " + base + "ms");

    while (job.index < job.todo.length) {
      if (job.stop) {
        log("⏹ 已手动停止。重新导入同一文件即可断点续跑。");
        break;
      }
      if (getDailyCount() >= dailyLimit) {
        log("🛑 已达到今日尝试上限(" + dailyLimit + ")。B站每日配额约300次，请明天重新导入续跑。");
        break;
      }

      const item = job.todo[job.index];
      bumpDailyCount();
      const res = await followOne(item.mid, csrf);

      if (res.code === 0) {
        job.ok++;
        job.riskStreak = 0;
        const suffix = item.tags && item.tags.length
          ? await applyGroups(item, csrf, altTags)
          : "";
        log("✅ " + item.uname + suffix);
        if (job.ok % 30 === 0 && interval > Math.round(base * 0.7)) {
          interval -= 100;
          log("连续顺利，间隔调整为 " + interval + "ms");
        }
      } else if (PERMANENT_CODES.has(res.code)) {
        job.permFail++;
        log("⚪ 跳过（账号已注销）：" + item.uname);
      } else if (RISK_CODES.has(res.code)) {
        job.riskStreak++;
        job.riskTotal++;
        if (job.riskStreak >= 2 || job.riskTotal >= 4) {
          log("🛑 被风控拦截(" + job.riskTotal + "次)。疑似今日配额已用完，已自动停止；明天重新导入同一文件即可续跑。");
          break;
        }
        job.index--; // 重试当前项
        interval = Math.min(8000, Math.round(interval * 2) + 200);
        log("⚠️ 风控拦截 code=" + res.code + "，暂停120秒后重试，间隔调整为 " + interval + "ms");
        await SLEEP(120000);
      } else {
        job.fail++;
        job.riskStreak = 0;
        log("❌ " + item.uname + " code=" + res.code + " " + res.message);
      }

      renderProgress(interval);
      await SLEEP(interval + rand(-interval * 0.1, interval * 0.15));
    }

    const finished = job.index >= job.todo.length;
    job.running = false;
    els.stop.disabled = true;
    els.start.disabled = true; // 续跑统一走重新导入比对，天然断点续跑
    renderSummary();
    if (finished) {
      setProgress("全部完成 ✅ 成功" + job.ok + " · 注销跳过" + job.permFail + " · 失败" + job.fail);
      log("🎉 全部完成：成功 " + job.ok + "，注销跳过 " + job.permFail + "，其他失败 " + job.fail);
    }
  }

  // ================= 反向清理（取关多余） =================
  // extras = 当前账号有、但主号文件里没有的关注。删操作：预览 + 确认 + 保护机制
  async function startClean() {
    if (!job || job.running || !job.fileMids) return;
    const csrf = getCookie("bili_jct");
    if (!csrf) {
      log("❌ 未获取到csrf(bili_jct)，请刷新页面后重试");
      return;
    }
    const me = await apiNav();
    if (!me) {
      log("❌ 未登录，请先登录要清理的账号");
      return;
    }

    els.clean.disabled = true;
    els.start.disabled = true;
    log("🔄 反向清理：正在重新拉取当前账号最新关注列表…");
    try {
      const have = await fetchFollowings(me.mid, (n, t) =>
        setProgress("清理比对中 " + n + "/" + t)
      );

      // 保护机制：默认把小号的特别关注排除在取关名单外
      const protect = new Set();
      if (els.protect.checked) {
        try {
          setProgress("拉取特别关注名单…");
          for (const mid of await fetchTagMembers(TAG_SPECIAL, "特别关注")) protect.add(mid);
          log("🛡 已保护特别关注（" + protect.size + " 人不参与取关）");
        } catch (e) {
          log("⚠️ 特别关注名单拉取失败（" + e.message + "），保护未生效");
        }
      }

      const extras = [];
      have.forEach((info, mid) => {
        if (!job.fileMids.has(mid) && !protect.has(mid)) extras.push({ mid: mid, uname: info.uname });
      });
      if (extras.length === 0) {
        log("✅ 没有多余关注：当前账号的关注都在主号文件里（或全部被保护），无需清理");
        setProgress("无需清理");
        return;
      }
      log(
        "📋 待取关 " + extras.length + " 人：" +
        extras.slice(0, 30).map((u) => u.uname).join("、") +
        (extras.length > 30 ? " …等（完整名单已打印到控制台，按 F12 查看）" : "")
      );
      console.log("[反向清理] 完整名单：", extras);

      if (
        !confirm(
          "反向清理将取关 " + extras.length + " 人（名单见面板日志）。\n" +
          "取关后需手动重新关注才能恢复，确认执行吗？"
        )
      ) {
        log("已取消反向清理");
        return;
      }

      const base = parseInt(els.speed.value, 10);
      const dailyLimit = parseInt(els.limit.value, 10) || DAILY_LIMIT_DEFAULT;
      let interval = base;
      let ok = 0, skip = 0, fail = 0, riskStreak = 0;
      job.running = true;
      job.stop = false;
      els.stop.disabled = false;
      log("▶️ 开始反向清理，共 " + extras.length + " 人，间隔 " + base + "ms");

      for (let i = 0; i < extras.length; i++) {
        if (job.stop) {
          log("⏹ 已手动停止，剩余用户未受影响；重新点击④会重新比对");
          break;
        }
        if (getDailyCount() >= dailyLimit) {
          log("🛑 已达今日尝试上限(" + dailyLimit + ")，明天重新点击④继续");
          break;
        }
        const item = extras[i];
        bumpDailyCount(); // 取关是否计入B站配额未实测，按计入保守处理
        const res = await modifyRelation(item.mid, 2, csrf);

        if (res.code === 0) {
          ok++;
          riskStreak = 0;
        } else if (RISK_CODES.has(res.code)) {
          riskStreak++;
          if (riskStreak >= 2) {
            log("🛑 连续被风控拦截，疑似今日配额已用完，已停止；明天重新点击④继续");
            break;
          }
          i--; // 重试当前项
          interval = Math.min(8000, Math.round(interval * 2) + 200);
          log("⚠️ 风控拦截 code=" + res.code + "，暂停120秒后重试，间隔调整为 " + interval + "ms");
          await SLEEP(120000);
          continue;
        } else if (res.code === 22013 || res.code === 40061) {
          skip++;
          log("⚪ 跳过（账号已注销/不存在，关系将保留）：" + item.uname);
        } else {
          fail++;
          riskStreak = 0;
          log("❌ " + item.uname + " code=" + res.code + " " + res.message);
        }

        setProgress(
          "清理进度 " + (i + 1) + "/" + extras.length +
          " · 已取关 " + ok + " · 跳过 " + skip + " · 失败 " + fail +
          " · 间隔 " + interval + "ms · 今日已尝试 " + getDailyCount()
        );
        await SLEEP(interval + rand(-interval * 0.1, interval * 0.15));
      }

      job.running = false;
      els.stop.disabled = true;
      log("🎉 反向清理结束：取关 " + ok + "，跳过 " + skip + "，失败 " + fail);
      setProgress("清理完成：取关 " + ok + " · 跳过 " + skip + " · 失败 " + fail);
    } catch (e) {
      log("反向清理失败：" + e.message);
      job.running = false;
      els.stop.disabled = true;
    } finally {
      els.clean.disabled = false;
    }
  }

  // ================= 自动备份（浏览器本地） =================
  // 快照存 localStorage：不含分组信息（轻量兜底备份，完整备份请用「① 导出」）
  const BACKUP_KEY = "bfs_backup_history";
  const BACKUP_MAX = 30;          // 最多保留快照数
  const BACKUP_BUDGET = 3000000;  // 序列化后体积预算（约3MB），超出丢弃最旧

  function loadBackupHistory() {
    try {
      return JSON.parse(localStorage.getItem(BACKUP_KEY) || "{}");
    } catch (e) {
      return {};
    }
  }

  function saveBackupHistory(hist) {
    let text = JSON.stringify(hist);
    while (text.length > BACKUP_BUDGET && hist.snapshots.length > 1) {
      hist.snapshots.shift();
      text = JSON.stringify(hist);
    }
    try {
      localStorage.setItem(BACKUP_KEY, text);
      return true;
    } catch (e) {
      return false;
    }
  }

  async function autoBackup(me) {
    const dayKey = "bfs_bk_" + me.mid + "_" + new Date().toISOString().slice(0, 10);
    if (localStorage.getItem(dayKey)) return;
    try {
      const map = await fetchFollowings(me.mid);
      const users = [];
      map.forEach((info, mid) => users.push({ mid: Number(mid), uname: info.uname }));
      const hist = loadBackupHistory();
      if (!hist.snapshots) hist.snapshots = [];
      hist.snapshots.push({
        ts: new Date().toISOString(),
        mid: Number(me.mid),
        uname: me.uname,
        total: users.length,
        users: users,
      });
      while (hist.snapshots.length > BACKUP_MAX) hist.snapshots.shift();
      if (saveBackupHistory(hist)) {
        localStorage.setItem(dayKey, "1");
        log("📦 今日自动备份完成（" + users.length + " 人，存于浏览器本地）");
      } else {
        log("⚠️ 自动备份写入失败（浏览器本地存储空间不足）");
      }
    } catch (e) {
      log("⚠️ 自动备份失败（不影响同步功能）：" + e.message);
    }
  }

  function showBackupHistory() {
    const snaps = (loadBackupHistory().snapshots || []).slice().reverse(); // 1 = 最新
    if (snaps.length === 0) {
      log("📦 还没有历史备份。每天首次打开本页面会自动备份一次。");
      return;
    }
    log("📦 历史备份（共 " + snaps.length + " 份，1=最新）：");
    snaps.forEach((s, i) => {
      log(
        "  " + (i + 1) + ". " + s.ts.slice(0, 10) + " " + s.ts.slice(11, 16) +
        " · " + s.uname + " · " + s.total + " 人"
      );
    });
    const idx = parseInt(prompt("输入要下载的备份序号（1=最新，留空取消）：", "1"), 10);
    if (!idx || idx < 1 || idx > snaps.length) {
      log("未下载。");
      return;
    }
    const s = snaps[idx - 1];
    const payload = {
      type: "bilibili-follow-sync",
      version: 1, // 自动备份快照不含分组，按 v1 语义导入
      exportedAt: s.ts,
      account: { mid: s.mid, uname: s.uname },
      total: s.total,
      users: s.users,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "bilibili-follows-" + s.mid + "-" + s.ts.slice(0, 10) + ".json";
    a.click();
    URL.revokeObjectURL(a.href);
    log("✅ 已下载备份 " + s.ts.slice(0, 10) + "（" + s.total + " 人，不含分组）");
  }

  // ================= 启动 =================
  buildPanel();
  apiNav().then((me) => {
    els.user.textContent = me
      ? "已登录：" + me.uname + " (" + me.mid + ")"
      : "未登录（请先登录）";
    if (me) autoBackup(me); // 每天首次打开本页面自动备份一次
  });
})();
