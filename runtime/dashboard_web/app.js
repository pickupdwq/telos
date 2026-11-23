"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  projects: [], filter: "all", owner: null, project: null, task: null, query: "",
  projectLimit: 10, ownerLimit: 24, detail: null, detailStatus: "", loaded: false,
};
const day = 86400;
const authorize = "在终端运行 telos dashboard --open，用当前浏览器打开授权地址。";
const shortDate = new Intl.DateTimeFormat("zh-CN", {month: "short", day: "numeric"});
const clock = new Intl.DateTimeFormat("zh-CN", {hour: "2-digit", minute: "2-digit", hour12: false});
const eventNames = {status:"状态", lifecycle:"状态", message:"执行者", files:"文件变更", command:"命令结果", tool:"工具", error:"错误", plan:"计划", diagnostic:"提示", stderr:"诊断", reasoning:"活动", output:"输出", checks:"检查", callback:"回传"};
const kinds = {project:"项目", discussion:"讨论", standalone:"独立任务"};
const views = [["all","全部"],["active","运行中"],["attention","待处理"],["history","历史"]];
const signatures = new Map();
let detailRequest = 0;
let summaryRequest = false;
let lastSync = null;
let focusItem = null;

function element(tag, className, value) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (value != null) result.textContent = String(value);
  return result;
}
function button(className, label, action) {
  const result = element("button", className, label);
  result.type = "button";
  result.addEventListener("click", action);
  return result;
}
function pressed(node, value) { node.setAttribute("aria-pressed", String(value)); }
function when(seconds) {
  if (!(seconds > 0)) return "时间未知";
  const date = new Date(seconds * 1000);
  if (!Number.isFinite(date.valueOf())) return "时间未知";
  return date.toDateString() === new Date().toDateString() ? clock.format(date) : shortDate.format(date) + " " + clock.format(date);
}
function age(item) { return item.updated > 0 ? Date.now() / 1000 - item.updated : Infinity; }
function historical(item) { return item.tone === "done" || (item.tone !== "active" && age(item) > day); }
function attention(item) { return item.tone === "issue" || item.tone === "wait"; }
function inView(p, filter) {
  if (filter === "all") return true;
  if (filter === "history") return historical(p);
  if (p.tone === "done") return false;
  const pool = [p, ...p.tasks];
  if (filter === "active") return pool.some(t => t.tone === "active");
  return pool.some(attention);
}
function ranking(p) {
  if (p.tone === "done") return 4;
  if (p.tone === "active" || p.tasks.some(t => t.tone === "active")) return 0;
  if (historical(p)) return 3;
  return attention(p) || p.tasks.some(t => attention(t) && !historical(t)) ? 1 : 2;
}
function ownerId(p) { return p.owner?.id || "unbound"; }
function ownerName(p) { return p.owner?.label || "未绑定会话"; }
function sessionLabel(label,id) {
  return id && !["manual","unbound"].includes(id) ? label + " · " + idText(id) : label;
}
function repoBase(p) { return String(p.repo || "").split("/").filter(Boolean).pop() || ""; }
function idText(value) { return String(value || "").slice(0, 8); }
function singleLine(value) { return String(value || "").replace(/\s+/g, " ").trim(); }
function short(value, limit) {
  const clean = singleLine(value);
  return clean.length > limit ? clean.slice(0, limit) + "…" : clean;
}
function searchable(p) {
  return [p.id,p.title,p.repo,p.owner?.id,p.owner?.model,ownerName(p),p.status,...p.tasks.flatMap(t => [t.title,t.model,t.role,t.id,t.key,t.status,t.wait])].join(" ").toLocaleLowerCase();
}
function selection(items) {
  return items.filter(p => (state.owner === null || ownerId(p) === state.owner) && inView(p,state.filter) && (!state.query || p.search.includes(state.query)));
}
function relativeStatus(item) {
  const label = item.status || "状态未知";
  return historical(item) && item.tone !== "done" ? "上次：" + label : label;
}
function badge(item) {
  const validTone = ["active","wait","issue","done","idle"].includes(item.tone) ? item.tone : "idle";
  const tone = historical(item) && item.tone !== "done" ? "idle" : validTone;
  return element("span","badge " + tone,relativeStatus(item));
}
function duration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return Math.floor(seconds) + "秒";
  if (seconds < 3600) return Math.floor(seconds / 60) + "分";
  const hours = Math.floor(seconds / 3600);
  return hours + "时" + (Math.floor(seconds % 3600 / 60) || "") + (seconds % 3600 >= 60 ? "分" : "");
}
const timingParts = [["work","执行"],["checks","检查"],["wait","等待"]];
function textTiming(timing) {
  return timingParts.map(([key, label]) => label + " " + duration(timing?.[key])).join(" · ");
}
function empty(text) { return element("p","empty",text); }
function replaceWhen(id, signature, build) {
  const serialized = JSON.stringify(signature);
  if (signatures.get(id) === serialized) return;
  signatures.set(id,serialized);
  const container = $(id), active = document.activeElement;
  const focusKey = container.contains(active) ? active.dataset.focusKey : null;
  const oldScroll = container.scrollTop, oldLeft = container.scrollLeft;
  container.replaceChildren(...build());
  if (focusKey) {
    Array.from(container.querySelectorAll("[data-focus-key]")).find(node => node.dataset.focusKey === focusKey)?.focus({preventScroll:true});
  }
  container.scrollTop = oldScroll;
  container.scrollLeft = oldLeft;
}
function sessionRows() {
  const groups = new Map();
  for (const p of state.projects) {
    const id = ownerId(p);
    if (!groups.has(id)) groups.set(id,{id,label:ownerName(p),model:p.owner?.model || "",count:0});
    groups.get(id).count++;
  }
  return [...groups.values()];
}
function chooseOwner(id) {
  state.owner = id;
  state.projectLimit = 10;
  render();
}
function drawOwners(groups) {
  $("owner-count").textContent = String(groups.length);
  const shown = groups.slice(0,state.ownerLimit);
  const selected = groups.find(g => g.id === state.owner);
  if (selected && !shown.includes(selected)) shown.push(selected);
  replaceWhen("owners",[shown,state.owner,groups.length,state.projects.length],() => {
    const rows = [{id:null,label:"全部会话",count:state.projects.length},...shown].map(g => {
      const row = button("owner-row",null,() => chooseOwner(g.id));
      row.dataset.focusKey = g.id || "all";
      pressed(row,g.id === state.owner);
      const text = element("span","owner-copy");
      text.append(element("span","owner-name",sessionLabel(g.label,g.id)));
      if (g.model) text.append(element("span","owner-model",g.model));
      row.title = [g.label,g.model,g.id].filter(Boolean).join(" · ");
      const initial = g.id ? (g.label.includes("Claude") ? "C" : g.label.includes("Codex") ? "X" : "·") : "≡";
      row.append(element("span","avatar",initial),text,element("span","mono",g.count));
      return row;
    });
    if (shown.length < groups.length) rows.push(button("more","更多会话",() => { state.ownerLimit += 24; render(); }));
    return rows;
  });
}
function chooseFilter(id) { state.filter = id; state.projectLimit = 10; render(); }
function drawFilters() {
  const projects = state.projects.filter(p => (!state.owner || ownerId(p) === state.owner) && (!state.query || p.search.includes(state.query)));
  const counts = views.map(([key]) => projects.filter(p => inView(p,key)).length);
  replaceWhen("filters",[counts,state.filter],() => views.map(([key,label],i) => {
    const tab = button("filter",label,() => chooseFilter(key));
    tab.dataset.focusKey = key;
    pressed(tab,state.filter === key);
    tab.append(element("small","",counts[i]));
    tab.title = key === "history" ? "已结束，或超过一天没有更新且不在运行的记录" : label + "的项目、讨论或独立任务";
    return tab;
  }));
}
function pickProject(p, matchSearch = false) {
  state.project = p.id;
  const match = matchSearch && state.query ? p.tasks.find(t => [t.title,t.key,t.role,t.model,t.id].join(" ").toLocaleLowerCase().includes(state.query)) : null;
  state.task = match?.id || null;
}
function chooseTask(p,t) {
  state.project = p.id;
  state.task = t.id;
  render();
  if (window.matchMedia("(max-width:620px)").matches) $("inspector").scrollIntoView({block:"start"});
}
function clearsDependencies(p, task) {
  if (!task?.unblocks?.length) return "";
  const names = task.unblocks.map(key => short(p.tasks.find(item => item.key === key)?.title || key, 22));
  return "通过后，" + names.join("、") + "的前置任务就齐了";
}
function drawPriority(projects) {
  // Follow the current scope. Old records are history, not a present-day blocker.
  focusItem = null;
  for (const p of projects) {
    if (p.tone === "done") continue;
    const candidates = p.tasks.filter(t => attention(t) && t.wait);
    const task = candidates.find(t => t.tone === "issue") ||
      candidates.find(t => !["pending","ready"].includes(t.phase)) || candidates[0];
    if (task || (attention(p) && p.wait)) { focusItem = {p,t:task}; break; }
  }
  $("priority").hidden = !focusItem;
  if (!focusItem) return;
  const {p,t} = focusItem;
  $("priority-text").textContent = [short(t?.title || p.title,30),t?.wait || p.wait,clearsDependencies(p,t)].filter(Boolean).join(" · ");
  $("priority-text").title = $("priority-text").textContent;
  $("priority-open").textContent = t ? "定位任务 →" : "定位项目 →";
}
function projectRow(p) {
  const open = p.id === state.project;
  const item = element("article","project" + (open ? " open" : ""));
  const select = button("project-select",null,() => { pickProject(p); render(); });
  select.setAttribute("aria-expanded",String(open)); select.dataset.focusKey = "p:" + p.id;
  const top = element("span","project-top"), meta = element("span","project-id");
  meta.append(element("span","kind",kinds[p.kind] || "项目"));
  if (p.repo) { const repo = element("span","repo",repoBase(p)); repo.title = p.repo; meta.append(repo); }
  top.append(meta,badge(p));
  const title = element("span","project-name",short(p.title,150) || idText(p.id));
  title.title = p.title;
  select.append(top,title,element("span","project-bottom",[sessionLabel(ownerName(p),p.owner?.id),p.tasks.length + " 项任务",when(p.updated)].join(" · ")));
  item.append(select);
  if (!open) return item;
  const body = element("div","project-body");
  if (p.wait) body.append(element("p","project-wait",(historical(p) && p.tone !== "done" ? "上次记录：" : "") + p.wait));
  if (p.kind !== "standalone") body.append(element("p","project-timing","任务累计：" + textTiming(p.timing)));
  if (p.tasks.length) {
    const list = element("div","task-list");
    for (const t of p.tasks) {
      const row = button("task-row",null,() => chooseTask(p,t));
      row.dataset.focusKey = "t:" + t.id; pressed(row,state.task === t.id);
      const taskTop = element("span","task-top");
      const name = element("span","task-name",short(t.title,105) || t.key || idText(t.id));
      taskTop.append(name,badge(t));
      row.append(taskTop,element("span","task-sub",[t.role, t.model || "执行者未记录",t.key && t.key !== t.id ? t.key : ""].filter(Boolean).join(" · ")));
      if (t.activity) row.append(element("span","task-activity","最近动作：" + short(t.activity,110)));
      if (t.wait) row.append(element("span","task-wait",short(t.wait,150)));
      list.append(row);
    }
    body.append(list);
  } else body.append(element("p","minor","暂未分配任务"));
  item.append(body);
  return item;
}
function drawProjects(items) {
  $("project-count").textContent = String(items.length);
  // Keep a selected project visible if newly arrived jobs change its rank.
  const index = items.findIndex(p => p.id === state.project);
  const shown = items.slice(0,Math.max(state.projectLimit,index + 1));
  const summaries = shown.map(p => [
    p.id,p.title,p.repo,ownerName(p),p.kind,p.phase,p.status,p.tone,historical(p),p.updated,p.tasks.length,
    ...(p.id === state.project ? [p.wait,textTiming(p.timing),p.tasks.map(t => [t.id,t.key,t.title,t.role,t.model,t.status,t.tone,historical(t),t.wait,t.activity])] : [])
  ]);
  replaceWhen("projects",[summaries,state.project,state.task,items.length,state.loaded],() => {
    if (!items.length) return [empty(!state.loaded ? "正在读取项目…" : !state.projects.length ? "还没有 Telos 运行记录。启动任务后，它会显示在这里。" : "没有符合当前筛选条件的项目。")];
    const rows = shown.map(projectRow);
    if (shown.length < items.length) rows.push(button("more","查看更多项目（还有 " + (items.length-shown.length) + " 项）",() => { state.projectLimit = shown.length + 10; render(); }));
    return rows;
  });
}
function fact(list,title,value) {
  if (value) list.append(element("dt","",title),element("dd","",value));
}
function timingSection(timing) {
  const section = element("section","section"), row = element("div","duration-row");
  for (const [key,label] of timingParts) {
    const value = element("span","",label);
    value.append(element("b","",duration(timing?.[key])));
    row.append(value);
  }
  section.append(element("h4","","耗时"),row);
  return section;
}
// The terminal recorder uses machine phase names for short lifecycle entries.
function eventText(event) {
  const raw = String(event.text || "");
  if (event.kind !== "status") return raw;
  const phases = {ready:"等待派发", running:"执行中", responded:"执行者已提交结果",
    awaiting_review:"等待大总管审核", accepted:"任务已通过", not_accepted:"任务未通过",
    discussion_result:"已提交讨论意见", needs_clarification:"等待答复", checkpoint:"已保存进度",
    error:"执行出错", blocked_environment:"执行环境受阻", protocol_error:"回复格式异常"};
  const [phase, mode] = raw.split(" / ");
  const modes = {active:"", paused:"后续派发已暂停", cancelled:"后续派发已取消"};
  return Object.hasOwn(phases, phase) && Object.hasOwn(modes, mode)
    ? [phases[phase], modes[mode]].filter(Boolean).join(" · ") : raw;
}
function eventList(events) {
  const section = element("section","section");
  section.append(element("h4","","最近活动"));
  if (!events?.length) {
    section.append(element("p","minor","还没有活动记录。"));
    return section;
  }
  const list = element("ol","stream");
  for (const event of [...events].reverse()) {
    const item = element("li"), line = element("div","activity-label");
    line.append(element("time","",when(event.at)),element("b","",eventNames[event.kind] || event.kind || "活动"));
    item.append(line);
    const text = eventText(event);
    if (text.length > 260 || text.split("\n").length > 6) {
      item.append(element("p","event-text",text.slice(0,190).split("\n").slice(0,4).join("\n") + "…"));
      const expand = element("details","description");
      expand.dataset.expand = "event:" + event.at + ":" + event.kind;
      expand.append(element("summary","","展开记录"),element("p","event-text",text));
      item.append(expand);
    } else if (text) item.append(element("p","event-text",text));
    list.append(item);
  }
  section.append(list);
  return section;
}
function projectDetail(p) {
  const nodes = [badge(p),element("h3","detail-title",short(p.title,120) || idText(p.id))];
  if (String(p.title || "").length > 120) {
    const details = element("details","description");
    details.dataset.expand = "project:" + p.id;
    details.append(element("summary","","展开项目摘要"),element("p","",p.title)); nodes.push(details);
  }
  const facts = element("dl","facts");
  fact(facts,"大总管",sessionLabel(ownerName(p),p.owner?.id));
  fact(facts,"模型",p.owner?.model);
  fact(facts,"工作目录",p.repo);
  if (p.title_source === "initial_message") fact(facts,"标题来源","首次派发指令");
  fact(facts,"最近更新",when(p.updated));
  fact(facts,historical(p) && p.tone !== "done" ? "上次等待" : "等待原因",p.wait);
  nodes.push(facts);
  const timing = timingSection(p.timing);
  timing.querySelector("h4").textContent = "任务累计耗时";
  nodes.push(timing);
  const section = element("section","section");
  section.append(element("h4","","各任务最近进展"));
  const list = element("ol","stream");
  for (const t of [...p.tasks].sort((a,b) => b.updated-a.updated)) {
    const item = element("li"), line = element("div","activity-label");
    line.append(element("time","",when(t.updated)),badge(t));
    const link = button("task-link",short(t.title,80) || t.key || idText(t.id),() => chooseTask(p,t));
    link.dataset.focusKey = "detail-task:" + t.id;
    item.append(line,link);
    if (t.wait) item.append(element("p","minor",short(t.wait,100)));
    list.append(item);
  }
  section.append(p.tasks.length ? list : element("p","minor","暂未分配任务。"));
  nodes.push(section);
  return nodes;
}
function drawDetail(p,t) {
  $("detail-heading").textContent = t ? "任务详情" : "项目详情";
  $("detail-id").textContent = idText(t?.id || p?.id);
  const data = state.detail?.id === t?.id ? state.detail : null;
  const container = $("detail");
  const opened = new Set(Array.from(container.querySelectorAll("details[open]"),n => n.dataset.expand));
  replaceWhen("detail",[p?.id,p && ownerName(p),p?.owner?.id,t,t && historical(t),!t && p ? [p.title,p.repo,p.owner?.model,p.updated,p.status,p.wait,textTiming(p.timing),p.tasks.map(t => [t.id,t.title,t.status,t.wait,t.updated])] : null,data,state.detailStatus],() => {
    if (!p) return [empty("选择一个项目或任务，查看执行者和最近活动。")];
    if (!t) {
      const nodes = projectDetail(p);
      for (const node of nodes) if (node.matches("details")) node.open = opened.has(node.dataset.expand);
      return nodes;
    }
    const meta = element("div","detail-meta");
    meta.append(badge(t),element("small","","更新于 " + when(t.updated)));
    const nodes = [meta,element("h3","detail-title",short(t.title,120) || t.key || idText(t.id))];
    if (String(t.title || "").length > 120) {
      const details = element("details","description");
      details.dataset.expand = "title:" + t.id; details.open = opened.has(details.dataset.expand);
      details.append(element("summary","","展开任务摘要"),element("p","",t.title)); nodes.push(details);
    }
    const facts = element("dl","facts");
    fact(facts,"大总管",sessionLabel(ownerName(p),p.owner?.id));
    fact(facts,"执行者",t.model || "未记录");
    if (p.title_source === "initial_message") fact(facts,"标题来源","首次派发指令");
    fact(facts,"最近动作",t.activity);
    fact(facts,"角色",t.role);
    fact(facts,"任务标识",t.key && t.key !== t.id ? t.key : null);
    fact(facts,historical(t) && t.tone !== "done" ? "上次等待" : "等待原因",t.wait);
    fact(facts,"下游影响",clearsDependencies(p,t));
    fact(facts,"结果回传",data?.callback);
    fact(facts,"检查结果",data?.checks);
    nodes.push(facts);
    if (data) {
      if (state.detailStatus === "error") nodes.push(element("p","minor","活动暂时未能刷新，下面显示上次读取的内容。"));
      if (state.detailStatus === "unauthorized") nodes.push(element("p","minor",authorize));
      nodes.push(timingSection(data.timing));
      const stream = eventList(data.events);
      for (const d of stream.querySelectorAll("details")) d.open = opened.has(d.dataset.expand);
      nodes.push(stream);
    } else {
      const text = {load:"正在读取活动…",missing:"还没有这项任务的运行记录。",error:"暂时读不到活动，稍后会自动重试。",unauthorized:authorize};
      nodes.push(element("p","minor",text[state.detailStatus] || "正在读取活动…"));
    }
    return nodes;
  });
}
let currentDetailId = null;
function render() {
  const groups = sessionRows();
  if (state.owner !== null && !groups.some(g => g.id === state.owner)) state.owner = null;
  const items = selection(state.projects);
  let p = items.find(i => i.id === state.project);
  if (!p && items.length) { p = items[0]; pickProject(p,true); }
  if (!p) { state.project = null; state.task = null; }
  if (p && state.task && !p.tasks.some(t => t.id === state.task)) state.task = null;
  const t = p?.tasks.find(task => task.id === state.task);
  if ((t?.id || null) !== currentDetailId) {
    currentDetailId = t?.id || null;
    state.detail = null; state.detailStatus = "load"; detailRequest++;
    $("detail").scrollTop = 0;
    if (t) loadDetail(t.id);
  }
  drawOwners(groups); drawFilters(); drawPriority(items); drawProjects(items); drawDetail(p,t);
  if (state.loaded) {
    const count = kind => state.projects.filter(p => p.kind === kind).length;
    $("summary").textContent = [count("project") + " 个项目",count("discussion") + " 场讨论",count("standalone") + " 项独立任务"].join(" · ");
  }
}
async function loadDetail(id) {
  const request = ++detailRequest;
  try {
    const response = await window.dashboardFetch("/api/tasks/" + encodeURIComponent(id),{cache:"no-store"});
    if (response.status === 401) throw new Error("unauthorized");
    if (![200,404].includes(response.status)) throw new Error("detail unavailable");
    const data = response.ok ? await response.json() : null;
    if (request !== detailRequest || id !== currentDetailId) return;
    state.detail = data; state.detailStatus = data ? "ready" : "missing";
  } catch (error) {
    if (request !== detailRequest || id !== currentDetailId) return;
    state.detailStatus = error.message === "unauthorized" ? "unauthorized" : "error";
    if (state.detailStatus === "unauthorized") state.detail = null;
  }
  const p = state.projects.find(i => i.id === state.project), t = p?.tasks.find(i => i.id === id);
  drawDetail(p,t);
}
async function refresh() {
  if (summaryRequest) return;
  summaryRequest = true; $("refresh").disabled = true;
  const previous = currentDetailId;
  try {
    const response = await window.dashboardFetch("/api/overview",{cache:"no-store"});
    if (response.status === 401) throw new Error("unauthorized");
    if (!response.ok) throw new Error("overview unavailable");
    const overview = await response.json();
    if (!Array.isArray(overview.projects)) throw new Error("invalid overview");
    state.projects = overview.projects.map(p => {
      const project = {...p,tasks:Array.isArray(p.tasks) ? p.tasks : []};
      project.search = searchable(project);
      return project;
    }).sort((a,b) => ranking(a)-ranking(b) || b.updated-a.updated || String(a.id).localeCompare(String(b.id)));
    state.loaded = true; lastSync = new Date();
    $("connection").textContent = "已连接 · 自动刷新"; $("light").className = "light online";
    $("synced").textContent = "更新于 " + clock.format(lastSync);
    const warnings = (overview.warnings || []).filter(Boolean);
    $("notice").hidden = warnings.length === 0; $("notice").textContent = warnings.join("\n");
    render();
    if (currentDetailId && previous === currentDetailId) await loadDetail(currentDetailId);
  } catch (error) {
    $("light").className = "light offline"; $("connection").textContent = "连接中断";
    $("notice").hidden = false;
    $("notice").textContent = lastSync ? "暂时连不上 Telos，当前显示上次读取的记录。连接恢复后会自动更新。" : "暂时无法读取本机记录，请确认看板服务仍在运行。";
    if (error.message === "unauthorized") { $("connection").textContent = "尚未授权"; $("notice").textContent = authorize; }
    if (!state.loaded) { $("summary").textContent = "本机数据暂不可用"; $("projects").replaceChildren(empty("没有读到项目记录。")); }
  } finally {
    summaryRequest = false; $("refresh").disabled = false;
  }
}
$("refresh").addEventListener("click",refresh);
$("query").addEventListener("input",() => {
  state.query = $("query").value.trim().toLocaleLowerCase(); state.projectLimit = 10;
  render();
});
$("priority-open").addEventListener("click",() => {
  if (!focusItem) return;
  const {p,t} = focusItem;
  if (t) chooseTask(p,t);
  else { pickProject(p); render(); }
  if (!window.matchMedia("(max-width:620px)").matches) {
    document.querySelector(".project.open")?.scrollIntoView({block:"nearest"});
  }
});
document.addEventListener("visibilitychange",() => { if (!document.hidden) refresh(); });
render(); refresh();
window.setInterval(() => { if (!document.hidden) refresh(); },4000);
