"use strict";

const $ = (id) => document.getElementById(id);
const state = {
    projects: [],
    rows: [],
    filter: "all",
    owner: "",
    project: "",
    query: "",
    loaded: false,
    connected: false,
    lastSync: 0,
    focus: null,
    dialog: null,
    detail: null,
    detailState: "",
    preview: null,
    expanded: false,
};
const signatures = new Map();
const views = [
    ["all", "全部"],
    ["active", "运行中"],
    ["attention", "待处理"],
    ["history", "历史"],
];
const kindNames = { project: "项目", discussion: "讨论", standalone: "独立任务" };
const eventNames = {
    status: "状态",
    lifecycle: "状态",
    message: "执行者",
    files: "文件变更",
    command: "命令结果",
    tool: "工具",
    error: "错误",
    plan: "计划",
    diagnostic: "提示",
    checks: "检查",
    callback: "回传",
};
const clock = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
const calendar = new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric" });
const authorize = "在终端运行 telos dashboard --open，用当前浏览器打开授权地址。";
let loading = false,
    detailRequest = 0,
    returnFocus = null,
    priorityItem = null;

function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = String(text);
    return node;
}
function btn(cls, text, action) {
    const node = el("button", cls, text);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
}
function update(id, signature, build) {
    const sig = JSON.stringify(signature);
    if (signatures.get(id) === sig) return;
    const box = $(id),
        focus = box.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
    const open = new Set(Array.from(box.querySelectorAll("details[open]"), (n) => n.dataset.expand));
    const top = box.scrollTop;
    box.replaceChildren(...build());
    signatures.set(id, sig);
    box.scrollTop = top;
    if (open.size) for (const d of box.querySelectorAll("details")) d.open = open.has(d.dataset.expand);
    if (focus)
        Array.from(box.querySelectorAll("[data-focus]"))
            .find((n) => n.dataset.focus === focus)
            ?.focus({ preventScroll: true });
}
function clean(v) {
    return String(v || "")
        .replace(/\s+/g, " ")
        .trim();
}
function short(v, n) {
    const s = clean(v);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
function shortId(id) {
    return String(id || "").slice(0, 8);
}
function ownerId(p) {
    return p.owner?.id || "unbound";
}
function ownerLabel(p) {
    return p.owner?.label || "未绑定大总管";
}
function supervisor(p) {
    return ["unbound", "manual"].includes(ownerId(p))
        ? ownerLabel(p)
        : ownerLabel(p) + " · " + shortId(ownerId(p));
}
function taskId(t) {
    return t.display_id || "任务";
}
function taskTitle(t) {
    return t.display_title || (/[\u3400-\u9fff]/.test(t.title) ? t.title : taskId(t));
}
function sourceDescription(t) {
    const raw = clean(t.title);
    return raw &&
        raw !== clean(taskTitle(t)) &&
        raw !== t.id &&
        raw !== t.key &&
        !/^[\da-f-]{16,}$/i.test(raw)
        ? raw
        : "";
}
function projectTitle(p) {
    if (p.kind === "standalone" && p.tasks[0]) return sourceDescription(p.tasks[0]) || taskTitle(p.tasks[0]);
    return p.display_title || p.title || "未命名项目";
}
function repoBase(p) {
    return (
        String(p.repo || "")
            .split("/")
            .filter(Boolean)
            .pop() || ""
    );
}
function when(s) {
    if (!(s > 0)) return "时间未知";
    const d = new Date(s * 1000);
    if (!Number.isFinite(d.valueOf())) return "时间未知";
    return (d.toDateString() === new Date().toDateString() ? "" : calendar.format(d) + " ") + clock.format(d);
}
function running(row) {
    return row.t
        ? row.t.phase === "running" && row.t.tone === "active"
        : row.p.phase === "verifying" && row.p.tone === "active";
}
function attention(item) {
    return item.tone === "issue" || item.tone === "wait";
}
function historical(item) {
    return (
        item.tone === "done" || (item.tone !== "active" && Date.now() / 1000 - (item.updated || 0) > 86400)
    );
}
function phaseText(item) {
    if (!state.connected && item.tone === "active") return "上次：" + (item.status || "运行中");
    return item.status || "状态未知";
}
function status(item) {
    const tone =
        ["active", "wait", "done", "issue"].includes(item.tone) && (item.tone !== "active" || state.connected)
            ? item.tone
            : "idle";
    return el("span", "status " + tone, phaseText(item));
}
function recent(row) {
    return row.t?.updated || row.p.updated || 0;
}
function ref(p, t) {
    return { pid: p.id, tid: t?.id || null };
}
function same(a, b) {
    return !!a && !!b && a.pid === b.pid && a.tid === b.tid;
}
function resolve(key) {
    if (!key) return null;
    const p = state.projects.find((x) => x.id === key.pid);
    if (!p) return null;
    const t = key.tid ? p.tasks.find((x) => x.id === key.tid) : null;
    return key.tid && !t ? null : { p, t };
}
function readRows(projects) {
    const rows = [];
    for (const p of projects) {
        const items = p.tasks.map((t) => ({ p, t }));
        if (!items.length || p.phase === "verifying") items.push({ p, t: null });
        for (const row of items) {
            const t = row.t;
            row.search = [
                p.title,
                p.id,
                p.repo,
                p.owner?.id,
                p.owner?.model,
                ownerLabel(p),
                t?.title,
                t?.key,
                t?.id,
                t?.role,
                t?.model,
                t?.display_id,
                t?.display_title,
                t?.status,
                t?.wait,
            ]
                .join(" ")
                .toLocaleLowerCase();
            rows.push(row);
        }
    }
    return rows.sort(
        (a, b) =>
            order(a) - order(b) ||
            recent(b) - recent(a) ||
            String(a.t?.id || a.p.id).localeCompare(String(b.t?.id || b.p.id)),
    );
}
function order(row) {
    if (running(row)) return 0;
    const item = row.t || row.p;
    if (["pending", "ready", "starting"].includes(item.phase)) return 2;
    if (attention(item)) return 1;
    return item.tone === "done" ? 4 : 3;
}
function inView(row, key) {
    const item = row.t || row.p;
    if (key === "active") return running(row);
    if (key === "attention") return attention(item);
    if (key === "history") return historical(item);
    return true;
}
function scopedRows() {
    return state.rows.filter(
        (r) =>
            (!state.owner || ownerId(r.p) === state.owner) &&
            (!state.project || r.p.id === state.project) &&
            (!state.query || r.search.includes(state.query)),
    );
}
function workerModel(row) {
    if (!row.t) return row.p.phase === "verifying" ? "本机验证进程" : "尚未分配";
    return (
        row.t.model || (["pending", "ready", "starting"].includes(row.t.phase) ? "尚未运行" : "型号未记录")
    );
}
function workerLabel(row) {
    if (!row.t) return row.p.phase === "verifying" ? "Telos · 项目检查" : "尚无执行者记录";
    return [row.t.role, taskId(row.t)].filter(Boolean).join(" · ");
}
function elapsed(start, end) {
    if (!Number.isFinite(start) || start <= 0 || !Number.isFinite(end) || end < start) return "—";
    const seconds = Math.floor(end - start),
        mm = Math.floor(seconds / 60);
    return String(mm).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0");
}
function timer(row, preview = false) {
    const item = row.t,
        active = running(row);
    const start = preview ? state.preview?.start : item ? item.run_started_at : row.p.verification_started_at;
    let label = active ? "本轮已处理" : "上轮耗时";
    let end = item?.run_finished_at;
    if (preview) {
        label = "预览用时";
        end = Date.now() / 1000;
    } else if (active) {
        end = state.connected ? Date.now() / 1000 : state.lastSync;
        if (!state.connected) label = "上次已处理";
    } else if (["pending", "ready", "starting"].includes((item || row.p).phase) && !start) label = "尚未开工";
    const box = el("span", "clock");
    box.append(el("small", "", label));
    const num = el("strong", "", elapsed(start, end));
    if (Number.isFinite(start) && (preview || (active && state.connected)))
        num.dataset.started = String(start);
    if (num.textContent === "—") num.title = label === "尚未开工" ? label : "没有完整的本轮时间记录";
    box.append(num);
    return box;
}
function tick() {
    if (document.hidden || !state.connected) return;
    for (const node of document.querySelectorAll("[data-started]"))
        node.textContent = elapsed(Number(node.dataset.started), Date.now() / 1000);
}
function identify(cls, caption, model, sub) {
    const box = el("div", "actor flow-node " + cls),
        avatar = el("i", "avatar"),
        copy = el("div", "actor-copy");
    avatar.setAttribute("aria-hidden", "true");
    copy.append(el("span", "", caption), el("strong", "", model), el("span", "", sub));
    box.append(avatar, copy);
    return box;
}
function wire(second) {
    const n = el("span", "link" + (second ? " second" : ""));
    n.setAttribute("aria-hidden", "true");
    n.append(el("i"));
    return n;
}
function actionText(row) {
    const item = row.t || row.p;
    if (row.t?.activity) return "最近动作：" + clean(row.t.activity);
    if (item.wait) return item.wait;
    if (!row.t && row.p.phase === "verifying") return "正在运行项目整体检查";
    return "最近状态：" + phaseText(item);
}
function focusDetail(p, t) {
    if (!$("inspector").open) returnFocus = document.activeElement?.dataset.focus || null;
    state.focus = ref(p, t);
    state.dialog = ref(p, t);
    state.detail = null;
    state.detailState = t ? "load" : "";
    detailRequest++;
    state.preview = null;
    if (!$("inspector").open) $("inspector").showModal();
    $("detail").scrollTop = 0;
    render();
    if (t) fetchDetail(t.id);
}
function flowLine(row, mode) {
    const { p, t } = row;
    const moving = mode === "live" || mode === "preview";
    const line = el(
        "div",
        "flow-row" + (moving ? " moving" : "") + (mode === "preview" ? " previewing" : ""),
    );
    line.append(
        identify("owner", "大总管", p.owner?.model || ownerLabel(p), supervisor(p)),
        wire(false),
        identify(
            "worker",
            t ? "执行者" : p.phase === "verifying" ? "整体验证" : "执行者",
            workerModel(row),
            workerLabel(row),
        ),
        wire(true),
    );
    const task = btn("flow-select", null, () => focusDetail(p, t));
    const number = t ? taskId(t) : "项目";
    const project = p.kind === "standalone" ? "独立任务" : short(projectTitle(p), 42);
    const label =
        mode === "preview"
            ? "动画预览 · 非实时"
            : mode === "live"
              ? phaseText(t || p)
              : state.connected
                ? "记录 · " + phaseText(t || p)
                : "离线记录";
    task.append(el("span", "task-meta", [number, project, label].join(" · ")));
    task.append(el("span", "flow-task", t ? taskTitle(t) : projectTitle(p)));
    if (t && sourceDescription(t)) task.append(el("span", "flow-action", short(sourceDescription(t), 90)));
    task.append(el("span", "flow-action", short(actionText(row), 100)));
    task.title = "查看详情";
    task.dataset.focus = "flow:" + (t?.id || p.id);
    line.append(task, timer(row, mode === "preview"));
    return line;
}
function eligiblePreview(row) {
    return (
        row?.t &&
        row.t.model &&
        !["manual", "unbound"].includes(ownerId(row.p)) &&
        !["ready", "pending", "starting", "missing"].includes(row.t.phase) &&
        !running(row)
    );
}
function flowSignature(row) {
    const { p, t } = row;
    return [
        p.id,
        p.title,
        p.kind,
        p.owner,
        p.phase,
        p.verification_started_at,
        t?.id,
        t?.display_title,
        t?.display_id,
        t?.title,
        t?.model,
        t?.role,
        t?.activity,
        t?.phase,
        t?.status,
        t?.wait,
        p.status,
        p.wait,
        t?.run_started_at,
        t?.run_finished_at,
    ];
}
function drawFlow() {
    const active = state.rows.filter(running);
    let selected = resolve(state.focus);
    if (selected && !selected.t && selected.p.phase !== "verifying" && selected.p.tasks.length) {
        selected = state.rows.find((row) => row.p.id === selected.p.id && row.t);
    }
    if (active.length || !state.connected) state.preview = null;
    const playable = eligiblePreview(selected)
        ? selected
        : state.rows.reduce(
              (latest, row) =>
                  eligiblePreview(row) && (!latest || recent(row) > recent(latest)) ? row : latest,
              null,
          );
    const preview = state.preview && resolve(state.preview);
    let rows = [...active];
    const staticRow = preview || selected || playable || state.rows[0];
    if (
        staticRow &&
        !rows.some((r) => same(ref(r.p, r.t), ref(staticRow.p, staticRow.t))) &&
        (selected || !active.length)
    )
        rows.unshift(staticRow);
    if (selected)
        rows.sort(
            (a, b) =>
                Number(same(ref(b.p, b.t), ref(selected.p, selected.t))) -
                Number(same(ref(a.p, a.t), ref(selected.p, selected.t))),
        );
    const display = state.expanded ? rows : rows.slice(0, 3);
    const head = $("flow-state");
    head.className = preview ? "preview" : state.connected && active.length ? "live" : "";
    head.textContent = preview
        ? "动画预览 · 非实时"
        : !state.loaded
          ? "等待数据"
          : !state.connected
            ? "离线 · 上次记录"
            : active.length
              ? active.length + " 路正在工作"
              : "当前空闲 · 非实时";
    update(
        "flows",
        [
            state.connected,
            state.loaded,
            state.connected ? 0 : state.lastSync,
            display.map(flowSignature),
            state.preview,
            rows.length,
            state.expanded,
            state.focus,
            !!playable,
        ],
        () => {
            const nodes = display.map((row) =>
                flowLine(
                    row,
                    preview && same(ref(row.p, row.t), state.preview)
                        ? "preview"
                        : running(row) && state.connected
                          ? "live"
                          : "record",
                ),
            );
            if (!nodes.length)
                nodes.push(el("p", "empty", state.loaded ? "还没有可展示的协作记录。" : "正在读取执行关系…"));
            const tools = el("div", "flow-tools");
            if (rows.length > 3)
                tools.append(
                    btn(
                        "text-button",
                        state.expanded ? "收起" : "其余 " + (rows.length - 3) + " 路 ↓",
                        () => {
                            state.expanded = !state.expanded;
                            drawFlow();
                        },
                    ),
                );
            if (!active.length && playable && state.connected) {
                const toggle = btn("preview-btn", preview ? "结束预览" : "预览运行动画", () => {
                    state.preview = state.preview
                        ? null
                        : { ...ref(playable.p, playable.t), start: Date.now() / 1000 };
                    drawFlow();
                    tick();
                });
                toggle.setAttribute("aria-pressed", String(!!preview));
                toggle.dataset.focus = "preview";
                tools.append(toggle);
            }
            if (tools.childElementCount) nodes.push(tools);
            return nodes;
        },
    );
}
function identity(cls, label, model, context) {
    const node = el("span", "identity " + cls);
    node.append(el("span", "role-label", label), el("b", "", model), el("small", "", context));
    node.title = [label, model, context].join(" · ");
    return node;
}
function card(row) {
    const { p, t } = row,
        item = t || p;
    const live = state.connected && running(row);
    const tile = el(
        "article",
        "card" + (live ? " live-card" : "") + (same(state.focus, ref(p, t)) ? " selected" : ""),
    );
    const head = el("div", "card-head");
    const project = btn(
        "project-link",
        kindNames[p.kind] +
            (p.kind === "standalone" ? " · " + (repoBase(p) || "未记录目录") : " · " + projectTitle(p)),
        () => focusDetail(p, null),
    );
    project.dataset.focus = "project:" + (t?.id || p.id);
    project.title = "查看项目：" + projectTitle(p);
    head.append(project, status(item));
    const body = btn("card-open", null, () => focusDetail(p, t));
    body.dataset.focus = "card:" + (t?.id || p.id);
    const title = el("span", "title-line"),
        name = t ? taskTitle(t) : p.phase === "verifying" ? "项目整体验证" : "尚未分配任务";
    if (t && name !== taskId(t)) title.append(el("span", "task-number", taskId(t)));
    title.append(el("strong", "card-title", short(name, 145)));
    body.append(title);
    if (t && sourceDescription(t)) body.append(el("span", "card-action", short(sourceDescription(t), 150)));
    const actors = el("span", "card-actors");
    actors.append(
        identity("owner", "大总管", p.owner?.model || "型号未记录", supervisor(p)),
        identity("worker", "执行者", workerModel(row), workerLabel(row)),
    );
    body.append(actors);
    const footer = el("span", "card-foot");
    footer.append(el("span", "card-action", short(actionText(row), 100)));
    if (running(row)) {
        const time = timer(row),
            display = time.querySelector("strong");
        display.className = "card-clock";
        display.setAttribute("aria-label", time.querySelector("small").textContent);
        const clockLabel = el("span", "card-clock");
        clockLabel.append(time.querySelector("small").textContent + " ", display);
        footer.append(clockLabel);
    }
    body.append(footer);
    tile.append(head, body);
    return tile;
}
function selectOptions(id, items, selected, title) {
    update(id, items, () =>
        [["", title], ...items].map(([value, name]) => {
            const option = el("option", "", short(name, 92));
            option.value = value;
            option.title = name;
            return option;
        }),
    );
    $(id).value = selected;
}
function drawControls(scope) {
    const groups = new Map(),
        projects = [];
    for (const p of state.projects) {
        groups.set(ownerId(p), supervisor(p) + (p.owner?.model ? " · " + p.owner.model : ""));
        if (!state.owner || state.owner === ownerId(p))
            projects.push([p.id, kindNames[p.kind] + " · " + projectTitle(p)]);
    }
    selectOptions("owner", [...groups], state.owner, "全部会话");
    selectOptions("project-filter", projects, state.project, "全部项目");
    const counts = views.map(([key]) => scope.filter((row) => inView(row, key)).length);
    update("filters", [counts, state.filter], () =>
        views.map(([key, label], i) => {
            const node = btn("filter", label, () => {
                state.filter = key;
                render();
            });
            node.append(el("small", "", counts[i]));
            node.setAttribute("aria-pressed", String(key === state.filter));
            node.dataset.focus = "filter:" + key;
            return node;
        }),
    );
}
function dependency(p, t) {
    const downstream = (t?.unblocks || [])
        .map((key) => p.tasks.find((child) => child.key === key))
        .filter(Boolean);
    return downstream.length
        ? "通过后，" + downstream.map((child) => short(taskTitle(child), 25)).join("、") + " 的前置任务就齐了"
        : "";
}
function drawPriority(rows) {
    const candidates = rows.filter((r) => attention(r.t || r.p) && (r.t || r.p).wait);
    priorityItem =
        candidates.find((r) => (r.t || r.p).tone === "issue") ||
        candidates.find((r) => !["pending", "ready"].includes((r.t || r.p).phase)) ||
        candidates[0];
    $("priority").hidden = !priorityItem;
    if (priorityItem) {
        const { p, t } = priorityItem;
        $("priority-text").textContent = [
            short(t ? taskTitle(t) : projectTitle(p), 32),
            (t || p).wait,
            dependency(p, t),
        ]
            .filter(Boolean)
            .join(" · ");
    }
}
function render() {
    const scope = scopedRows(),
        visible = scope.filter((r) => inView(r, state.filter));
    drawFlow();
    drawControls(scope);
    drawPriority(visible);
    $("task-count").textContent = String(visible.filter((row) => row.t).length);
    const signatures = visible.map((row) => [
        flowSignature(row),
        row.t?.tone,
        row.t?.updated,
        row.p.tone,
        workerLabel(row),
        repoBase(row.p),
    ]);
    update("cards", [signatures, state.focus, state.loaded, state.connected], () => {
        if (visible.length) return visible.map(card);
        let text = state.loaded ? "还没有任务记录。" : "正在读取任务…";
        if (state.rows.length) text = "没有符合筛选条件的任务。";
        const message = el("p", "empty", text);
        if (state.rows.length)
            message.append(
                btn("text-button empty-action", "清除筛选", () => {
                    state.query = state.owner = state.project = "";
                    state.filter = "all";
                    $("query").value = "";
                    render();
                }),
            );
        return [message];
    });
    if (state.loaded) {
        const count = (k) => state.projects.filter((p) => p.kind === k).length;
        $("summary").textContent = [
            count("project") + " 个项目",
            count("discussion") + " 场讨论",
            count("standalone") + " 项独立任务",
        ].join(" · ");
    }
    if (state.dialog) drawDetail();
}
function fact(list, key, value) {
    if (value) list.append(el("dt", "", key), el("dd", "", value));
}
function timingSection(timing, title) {
    const box = el("section", "section"),
        row = el("div", "duration-row");
    for (const [key, label] of [
        ["work", "执行"],
        ["checks", "检查"],
        ["wait", "等待"],
    ]) {
        const val = timing?.[key],
            tag = el("span", "", label);
        let text = "—";
        if (Number.isFinite(val) && val >= 0) {
            const hours = Math.floor(val / 3600),
                minutes = Math.floor((val % 3600) / 60);
            text = hours
                ? hours + "时" + (minutes ? minutes + "分" : "")
                : minutes
                  ? minutes + "分"
                  : Math.floor(val) + "秒";
        }
        tag.append(el("b", "", text));
        row.append(tag);
    }
    box.append(el("h4", "", title), row);
    return box;
}
function eventText(event) {
    const raw = String(event.text || "");
    if (event.kind !== "status") return raw;
    const phases = {
        ready: "等待派发",
        running: "执行中",
        responded: "执行者已提交结果",
        awaiting_review: "等待大总管审核",
        accepted: "任务已通过",
        not_accepted: "任务未通过",
        discussion_result: "已提交讨论意见",
        needs_clarification: "等待答复",
        checkpoint: "已保存进度",
        error: "执行出错",
        blocked_environment: "执行环境受阻",
        protocol_error: "回复格式异常",
    };
    const [phase, mode] = raw.split(" / ");
    const modes = { active: "", paused: "后续派发已暂停", cancelled: "后续派发已取消" };
    return Object.hasOwn(phases, phase) && Object.hasOwn(modes, mode)
        ? [phases[phase], modes[mode]].filter(Boolean).join(" · ")
        : raw;
}
function eventList(events) {
    const section = el("section", "section");
    section.append(el("h4", "", "最近活动"));
    if (!events?.length) {
        section.append(el("p", "minor", "还没有活动记录。"));
        return section;
    }
    const list = el("ol", "stream");
    for (const event of [...events].reverse()) {
        const item = el("li"),
            line = el("div", "activity-label");
        line.append(
            el("time", "", when(event.at)),
            el("b", "", eventNames[event.kind] || event.kind || "活动"),
        );
        item.append(line);
        const text = eventText(event);
        if (text.length > 260 || text.split("\n").length > 6) {
            item.append(el("p", "event-text", text.slice(0, 190).split("\n").slice(0, 4).join("\n") + "…"));
            const expand = el("details", "description");
            expand.dataset.expand = "event:" + event.at + ":" + event.kind;
            expand.append(el("summary", "", "展开记录"), el("p", "event-text", text));
            item.append(expand);
        } else if (text) item.append(el("p", "event-text", text));
        list.append(item);
    }
    section.append(list);
    return section;
}
function drawDetail() {
    const row = resolve(state.dialog);
    if (!row) {
        $("inspector").close();
        return;
    }
    const { p, t } = row,
        item = t || p,
        data = state.detail;
    $("detail-heading").textContent = t ? "任务详情" : "项目详情";
    $("detail-id").textContent = t ? taskId(t) : kindNames[p.kind];
    update(
        "detail",
        [
            flowSignature(row),
            t?.updated,
            p.updated,
            p.timing,
            t?.timing,
            t?.unblocks,
            p.tasks.map((child) => [
                child.id,
                child.display_id,
                child.display_title,
                child.phase,
                child.updated,
            ]),
            state.connected,
            state.detailState,
            data,
        ],
        () => {
            const nodes = [status(item), el("h3", "detail-title", t ? taskTitle(t) : projectTitle(p))];
            if (t && sourceDescription(t)) nodes.push(el("p", "event-text", sourceDescription(t)));
            if (!t && p.title && projectTitle(p) !== p.title) nodes.push(el("p", "event-text", p.title));
            const facts = el("dl", "facts");
            if (t && p.kind !== "standalone") {
                const project = btn("task-link", projectTitle(p), () => focusDetail(p, null)),
                    dd = el("dd");
                dd.append(project);
                facts.append(el("dt", "", "所属项目"), dd);
            }
            fact(facts, "大总管", supervisor(p));
            fact(facts, "大总管模型", p.owner?.model);
            if (t) {
                fact(facts, "执行者", workerLabel(row));
                fact(facts, "执行者模型", workerModel(row));
                fact(facts, "最近动作", t.activity);
                fact(facts, "任务标识", taskId(t));
                fact(facts, "下游影响", dependency(p, t));
            } else fact(facts, "工作目录", p.repo);
            if (p.title_source === "initial_message") fact(facts, "标题来源", "首次派发指令");
            fact(facts, "等待原因", item.wait);
            fact(facts, "最近更新", when(recent(row)));
            fact(facts, "结果回传", data?.callback);
            fact(facts, "检查结果", data?.checks);
            nodes.push(
                facts,
                timingSection(t ? data?.timing || t.timing : p.timing, t ? "累计耗时" : "任务累计耗时"),
            );
            if (t) {
                if (state.detailState === "unauthorized" || state.detailState === "error")
                    nodes.push(
                        el(
                            "p",
                            "minor",
                            state.detailState === "unauthorized"
                                ? authorize
                                : "暂时读不到活动，稍后会自动重试。",
                        ),
                    );
                if (data) nodes.push(eventList(data.events));
                else if (!["unauthorized", "error"].includes(state.detailState))
                    nodes.push(
                        el(
                            "p",
                            "minor",
                            state.detailState === "missing" ? "还没有这项任务的运行记录。" : "正在读取活动…",
                        ),
                    );
            } else {
                const section = el("section", "section");
                section.append(el("h4", "", "各任务最近进展"));
                const list = el("ol", "stream");
                for (const child of [...p.tasks].sort((a, b) => b.updated - a.updated)) {
                    const line = el("li"),
                        meta = el("div", "activity-label");
                    meta.append(el("time", "", when(child.updated)), status(child));
                    const name = taskTitle(child),
                        label = name === taskId(child) ? name : taskId(child) + " · " + name;
                    const link = btn("task-link", short(label, 90), () => focusDetail(p, child));
                    link.dataset.focus = "detail:" + child.id;
                    line.append(meta, link);
                    if (child.wait) line.append(el("p", "minor", child.wait));
                    list.append(line);
                }
                section.append(p.tasks.length ? list : el("p", "minor", "暂未分配任务。"));
                nodes.push(section);
            }
            return nodes;
        },
    );
}
async function get(path) {
    const res = await window.dashboardFetch(path, { cache: "no-store", signal: AbortSignal.timeout(12000) });
    if (res.status === 401) throw new Error("unauthorized");
    if (res.status === 404) return null;
    if (!res.ok) throw new Error("unavailable");
    return res.json();
}
async function fetchDetail(id) {
    const request = ++detailRequest;
    try {
        const detail = await get("/api/tasks/" + encodeURIComponent(id));
        if (request !== detailRequest || state.dialog?.tid !== id) return;
        state.detail = detail;
        state.detailState = detail ? "ready" : "missing";
    } catch (error) {
        if (request !== detailRequest || state.dialog?.tid !== id) return;
        state.detailState = error.message === "unauthorized" ? "unauthorized" : "error";
        if (state.detailState === "unauthorized") state.detail = null;
    }
    drawDetail();
}
async function refresh() {
    if (loading) return;
    loading = true;
    $("refresh").disabled = true;
    try {
        const overview = await get("/api/overview");
        if (!Array.isArray(overview?.projects)) throw new Error("invalid overview");
        state.projects = overview.projects.map((p) => ({
            ...p,
            tasks: Array.isArray(p.tasks) ? p.tasks : [],
        }));
        state.rows = readRows(state.projects);
        state.loaded = true;
        state.connected = true;
        state.lastSync = Date.now() / 1000;
        $("connection").textContent = "已连接 · 自动刷新";
        $("light").className = "online";
        $("synced").textContent = "更新于 " + clock.format(new Date(state.lastSync * 1000));
        $("notice").textContent = (overview.warnings || []).join("\n");
        $("notice").hidden = !$("notice").textContent;
        render();
        if (state.dialog?.tid) fetchDetail(state.dialog.tid);
    } catch (error) {
        state.connected = false;
        state.preview = null;
        $("light").className = "offline";
        $("connection").textContent = error.message === "unauthorized" ? "尚未授权" : "连接中断";
        $("notice").hidden = false;
        $("notice").textContent =
            error.message === "unauthorized"
                ? authorize
                : state.loaded
                  ? "连接中断，显示上次读取的记录；动画和计时已停止。"
                  : "无法读取本机记录，请确认看板服务正在运行。";
        if (!state.loaded) $("summary").textContent = "本机数据暂不可用";
        render();
    } finally {
        loading = false;
        $("refresh").disabled = false;
    }
}
$("refresh").addEventListener("click", refresh);
$("query").addEventListener("input", () => {
    state.query = $("query").value.trim().toLocaleLowerCase();
    render();
});
$("owner").addEventListener("change", () => {
    state.owner = $("owner").value;
    state.project = "";
    render();
});
$("project-filter").addEventListener("change", () => {
    state.project = $("project-filter").value;
    render();
});
$("priority-open").addEventListener("click", () => {
    if (priorityItem) focusDetail(priorityItem.p, priorityItem.t);
});
$("close-detail").addEventListener("click", () => $("inspector").close());
$("inspector").addEventListener("click", (event) => {
    if (event.target !== $("inspector")) return;
    const rect = $("inspector").getBoundingClientRect();
    if (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
    )
        $("inspector").close();
});
$("inspector").addEventListener("close", () => {
    state.dialog = null;
    state.detail = null;
    detailRequest++;
    if (returnFocus)
        Array.from(document.querySelectorAll("[data-focus]"))
            .find((n) => n.dataset.focus === returnFocus)
            ?.focus({ preventScroll: true });
    returnFocus = null;
});
document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
});
window.addEventListener("offline", () => {
    state.connected = false;
    state.preview = null;
    $("light").className = "offline";
    $("connection").textContent = "正在确认本机连接";
    render();
    refresh();
});
render();
refresh();
window.setInterval(() => {
    if (!document.hidden) refresh();
}, 4000);
window.setInterval(tick, 1000);
