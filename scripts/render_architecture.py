#!/usr/bin/env python3
"""Render bilingual system architecture and lifecycle diagrams as PNG/SVG.

Matplotlib is a documentation-only dependency. Geometry and text live here;
preview images and outlined vector artwork share the same editable source.
"""
import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets"
WIDTH = 1800
THEME = {
    "background": "#081321", "panel": "#101F32", "panel_alt": "#12263A",
    "border": "#283C52", "ink": "#F0F5FC", "body": "#BACBDD", "muted": "#8399B1",
    "supervisor": "#89AEFF", "executor": "#5DE2D0", "runtime": "#C1A7FF",
    "artifact": "#8BDBAA", "policy": "#F3C586", "line": "#64809E",
}


class Canvas:
    def __init__(self, height, language, font=None):
        self.height, self.language = height, language
        plt.rcParams.update({
            "font.family": font or ("Heiti SC" if language == "zh" else "DejaVu Sans"),
            "font.weight": "normal", "svg.fonttype": "path",
            "svg.hashsalt": "telos-system-architecture-v2", "axes.unicode_minus": False,
        })
        self.fig, self.ax = plt.subplots(figsize=(WIDTH / 100, height / 100))
        self.fig.patch.set_facecolor(THEME["background"])
        self.ax.set(xlim=(0, WIDTH), ylim=(height, 0), aspect="equal")
        self.ax.axis("off")
        self.fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
        self.labels = []
        for x in range(40, WIDTH, 80):
            self.ax.plot([x, x], [0, height], color="#25415A", linewidth=.35, alpha=.15, zorder=0)
        for y in range(40, height, 80):
            self.ax.plot([0, WIDTH], [y, y], color="#25415A", linewidth=.35, alpha=.15, zorder=0)

    def text(self, x, y, value, size=18, color="body", *, width=None,
             bold=False, mono=False, align="left", rotation=0):
        artist = self.ax.text(
            x, y, value, fontsize=size, color=THEME.get(color, color),
            fontfamily="DejaVu Sans Mono" if mono else None,
            fontweight="bold" if bold and self.language == "en" else "normal",
            ha=align, va="center", linespacing=1.5, rotation=rotation, zorder=5,
        )
        self.labels.append((artist, width))
        return artist

    def box(self, x, y, width, height, *, fill="panel", edge="border", radius=16):
        self.ax.add_patch(FancyBboxPatch(
            (x, y), width, height, boxstyle=f"round,pad=0,rounding_size={radius}",
            linewidth=1.05, facecolor=THEME.get(fill, fill), edgecolor=THEME.get(edge, edge), zorder=2,
        ))

    def arrow(self, start, end, color="line", dashed=False, both=False):
        self.ax.add_patch(FancyArrowPatch(
            start, end, arrowstyle="<|-|>" if both else "-|>", mutation_scale=15,
            linewidth=1.3, color=THEME[color], linestyle=(0, (5, 4)) if dashed else "-",
            shrinkA=2, shrinkB=2, zorder=3,
        ))

    def route(self, points, color="line", dashed=False):
        self.ax.plot(*zip(*points), color=THEME[color], linewidth=1.15,
                     linestyle=(0, (5, 4)) if dashed else "-", zorder=1)
        self.arrow(points[-2], points[-1], color, dashed)

    def card(self, x, y, width, height, tag, title, lines, module, role="runtime"):
        self.box(x, y, width, height)
        self.ax.plot([x+23, x+72], [y+23, y+23], color=THEME[role], linewidth=3, zorder=4)
        self.text(x+86, y+23, tag, 12.5, role, width=width-110, bold=True)
        self.text(x+23, y+70, title, 23, "ink", width=width-46, bold=True)
        for i, line in enumerate(lines):
            self.text(x+23, y+118+i*35, line, 17.5, width=width-46)
        self.ax.plot([x+23, x+width-23], [y+height-42, y+height-42],
                     color=THEME["border"], linewidth=.8, zorder=4)
        self.text(x+23, y+height-20, module, 12.5, "muted", mono=True, width=width-46)

    def layer(self, number, y, title, subtitle):
        self.text(80, y, number, 17, "muted", mono=True)
        self.text(133, y, title, 22, "ink", bold=True, width=980)
        self.text(1720, y, subtitle, 12.5, "muted", align="right", width=600)
        self.ax.plot([80, 1720], [y+27, y+27], color=THEME["border"], linewidth=.8, zorder=1)

    def header(self, label, title, subtitle):
        self.text(80, 53, "TELOS", 19, "executor", bold=True)
        self.text(185, 53, "/  " + label, 13, "muted", mono=True)
        self.text(80, 116, title, 34, "ink", bold=True, width=1640)
        self.text(80, 174, subtitle, 17.5, width=1640)

    def save(self, name):
        # Fail on overflowing labels rather than silently reducing legibility.
        self.fig.canvas.draw()
        renderer = self.fig.canvas.get_renderer()
        inverse = self.ax.transData.inverted()
        errors = []
        for artist, limit in self.labels:
            bounds = artist.get_window_extent(renderer).transformed(inverse)
            if limit is not None and bounds.width > limit + 2:
                errors.append(f"Label width overflow: {artist.get_text()!r} ({bounds.width:.1f} > {limit})")
            if min(bounds.x0, bounds.x1) < 8 or max(bounds.x0, bounds.x1) > WIDTH-8:
                errors.append(f"Label outside canvas: {artist.get_text()!r} ({bounds.x0:.1f}, {bounds.x1:.1f})")
            if min(bounds.y0, bounds.y1) < 8 or max(bounds.y0, bounds.y1) > self.height-8:
                errors.append(f"Label outside canvas: {artist.get_text()!r} ({bounds.y0:.1f}, {bounds.y1:.1f})")
        if errors:
            raise ValueError("\n".join(errors))
        OUTPUT.mkdir(parents=True, exist_ok=True)
        for extension in ("png", "svg"):
            path = OUTPUT / f"{name}-{self.language}.{extension}"
            metadata = {"Creator": "Telos architecture generator", "Date": None} if extension == "svg" else None
            self.fig.savefig(path, dpi=180, metadata=metadata)
            if extension == "svg":
                path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()).rstrip()+"\n")
        plt.close(self.fig)


SYSTEM = {
    "en": {
        "title": "Architecture for verifiable AI execution",
        "subtitle": "Two model roles. A deterministic control plane. Acceptance bound to source and evidence.",
        "legend": ["GPT supervisor", "Executor session", "Local runtime", "Artifacts & provenance"],
        "layers": [
            ("CONTROL SURFACE", "MCP / ORIGINAL THREAD OWNERSHIP"),
            ("ORCHESTRATION PLANE", "CONTRACTS / STATE / DEPENDENCIES"),
            ("EXECUTION & VERIFICATION PLANE", "ISOLATION / INFERENCE / POLICY"),
            ("ARTIFACT & EXPERIENCE PLANE", "LOCAL FILES / GIT / PROVENANCE"),
        ],
        "cards": [
            ("MODEL ROLE 01", "GPT supervisor", ["Original flagship conversation", "Plans, reviews source and scores", "Sole authority for final acceptance"], "supervisor.py", "supervisor"),
            ("MCP INTERFACE", "Tool gateway", ["stdio / JSON-RPC · 24 MCP tools", "Trusted threadId binds the owner", "open · send · evaluate · grade"], "telos.py", "runtime"),
            ("ASYNC CONTROL", "Completion & recovery", ["Native CLI queue / Desktop IPC*", "Durable outbox + event receipts", "Idempotent actions; reconciliation"], "codex_rpc.py / desktop_rpc.py", "runtime"),
            ("IMMUTABLE CONTRACT", "Contract & model policy", ["Private questions, references, checks", "Frozen provider / model / effort", "SHA-256 contract integrity check"], "telos.py / providers.py", "runtime"),
            ("FINITE-STATE MACHINE", "Task lifecycle controller", ["Persisted phases + process tracking", "Bounded retries; default 5 rounds", "pause / resume / cancel controls"], "telos.py / processes.py", "runtime"),
            ("DEPENDENCY DAG", "Project scheduler", ["Dependencies + allowed write paths", "Up to 3 concurrent model jobs", "Scratch integration + project gate"], "projects.py", "runtime"),
            ("MODEL ROLE 02", "Persistent executor", ["One frozen binding per task", "WORK: tools + isolated worktree", "EXAM: same thread, tools=[]", "No new source, references or checks"], "executor.py / closed_book.py", "executor"),
            ("INFERENCE BOUNDARY", "Authenticated relay", ["Local token; upstream keys in bridge", "Fixed HTTPS Responses endpoint", "DeepSeek / Ark / OpenAI / custom", "Exam request + response validation"], "isolation.py / providers.py", "runtime"),
            ("POLICY ENFORCEMENT", "Evidence & acceptance", ["Pinned commit → executable checks", "Supervisor scores; program enforces", "Default ≥90 + critical minima", "No blockers; hashes unchanged"], "telos.py / check_runtime.py", "runtime"),
            ("WORKSPACE ARTIFACTS", "Git worktree & snapshots", ["Per-task worktrees / isolated homes", "Pinned commits + test snapshots", "Verified archives before cleanup"], "~/.cache/telos/ / storage.py", "artifact"),
            ("PRIVATE EVIDENCE", "State & audit records", ["Contracts, packets, events, receipts", "Commit / contract_hash / packet_id", "JSON state + filesystem locks"], "~/.local/state/telos/", "artifact"),
            ("VERIFIED EXPERIENCE", "Provenance & retrieval", ["Promote only after acceptance", "Repository + path-scoped selection", "≤3 lessons / 6,000 characters"], "experience.py", "artifact"),
        ],
        "reuse": "Verified public lessons → future work context",
        "events": "completion events",
        "boundary": "CROSS-CUTTING TRUST BOUNDARIES",
        "boundaries": [
            ("FILESYSTEM", "macOS Seatbelt", "Scoped reads / writes"),
            ("EXAM PROTOCOL", "No tool execution", "Same work history retained"),
            ("CREDENTIALS", "Keys stay in bridge", "Shell excludes relay tokens"),
            ("DELIVERY", "Accepted commit", "Merge / push / deploy separately"),
        ],
        "notes": [
            "Logical modules of one local Python application; remote inference providers are external dependencies.",
            "* Desktop IPC is version-dependent; real Desktop automatic return has not yet been verified.",
            "Experience is evidence-backed retrieval. No weight training, vector database or extra reviewer model.",
        ],
    },
    "zh": {
        "title": "面向可验证 AI 执行的分层架构",
        "subtitle": "两类模型角色 · 确定性控制平面 · 源码、证据与最终验收绑定",
        "legend": ["GPT 大总管", "执行者会话", "本地运行时", "产物与来源记录"],
        "layers": [
            ("交互与控制入口", "MCP / ORIGINAL THREAD OWNERSHIP"),
            ("任务编排平面", "CONTRACTS / STATE / DEPENDENCIES"),
            ("执行与验证平面", "ISOLATION / INFERENCE / POLICY"),
            ("产物与经验平面", "LOCAL FILES / GIT / PROVENANCE"),
        ],
        "cards": [
            ("MODEL ROLE 01", "GPT 大总管", ["原始 GPT 旗舰对话与身份绑定", "需求规划 · 源码审核 · 逐题评分", "最终验收权始终保留在原会话"], "supervisor.py", "supervisor"),
            ("MCP INTERFACE", "工具网关与会话绑定", ["stdio / JSON-RPC · 24 个 MCP 工具", "可信 threadId 绑定原始调用者", "open · send · evaluate · grade"], "telos.py", "runtime"),
            ("ASYNC CONTROL", "完成回传与恢复", ["CLI 原生队列 / Desktop IPC*", "持久化发件箱 + 事件回执", "操作幂等 · 状态核对 · 故障恢复"], "codex_rpc.py / desktop_rpc.py", "runtime"),
            ("IMMUTABLE CONTRACT", "冻结合同与服务商策略", ["私有问卷、参考答案及可执行检查", "固定服务商 / 模型 / 推理强度", "SHA-256 合同完整性校验"], "telos.py / providers.py", "runtime"),
            ("FINITE-STATE MACHINE", "任务生命周期控制器", ["阶段状态持久化 + 子进程跟踪", "有界重试 · 默认最多 5 轮评估", "暂停 / 恢复 / 取消"], "telos.py / processes.py", "runtime"),
            ("DEPENDENCY DAG", "项目依赖图调度器", ["依赖拓扑 + 可修改路径约束", "最多 3 个并发模型作业", "独立工作区集成 + 整体验收门禁"], "projects.py", "runtime"),
            ("MODEL ROLE 02", "持久化执行者会话", ["每项任务固定一个执行者绑定", "WORK：工具 + 隔离 Git 工作区", "EXAM：恢复同会话，tools=[]", "不追加源码、参考答案或检查结果"], "executor.py / closed_book.py", "executor"),
            ("INFERENCE BOUNDARY", "鉴权推理转发层", ["临时令牌 · 上游密钥仅由桥接层持有", "固定 HTTPS Responses 接口", "DeepSeek / 火山 / OpenAI / 自定义", "闭卷请求策略 + 响应完整性校验"], "isolation.py / providers.py", "runtime"),
            ("POLICY ENFORCEMENT", "证据与验收策略门禁", ["固定提交 → 真实可执行检查", "大总管评分，程序强制执行规则", "默认 ≥90 分 + 关键题最低分", "无阻塞项，源码与证据哈希未改变"], "telos.py / check_runtime.py", "runtime"),
            ("WORKSPACE ARTIFACTS", "Git 工作区与快照", ["每任务独立 worktree / 运行目录", "固定提交 + 验收快照", "验证归档后才允许清理工作区"], "~/.cache/telos/ / storage.py", "artifact"),
            ("PRIVATE EVIDENCE", "私有状态与审计记录", ["合同、验收包、事件、回执", "提交 / contract_hash / packet_id", "JSON 状态文件 + 文件系统锁"], "~/.local/state/telos/", "artifact"),
            ("VERIFIED EXPERIENCE", "带来源的经验检索", ["整体通过验收后才允许复用", "按仓库与修改路径匹配", "最多 3 条经验 / 6,000 字符"], "experience.py", "artifact"),
        ],
        "reuse": "已验证的公开经验 → 后续任务的工作上下文",
        "events": "完成事件回传",
        "boundary": "跨层权限与信任边界  /  TRUST BOUNDARIES",
        "boundaries": [
            ("FILESYSTEM", "macOS Seatbelt", "限制文件读取与写入范围"),
            ("EXAM PROTOCOL", "闭卷阶段禁止工具", "保留此前工作会话记忆"),
            ("CREDENTIALS", "密钥留在桥接层", "shell 不继承转发令牌"),
            ("DELIVERY", "产出已验收提交", "合并、推送、部署另行执行"),
        ],
        "notes": [
            "这些是本地 Python 程序的逻辑模块；远端推理服务商是外部依赖，不代表分布式微服务部署。",
            "* Desktop IPC 依赖客户端协议版本；真实桌面自动回传仍待端到端验证。",
            "经验机制是有证据来源的检索复用，不训练模型权重，不依赖向量数据库，不增加独立复核模型。",
        ],
    },
}


def system_diagram(language, font=None):
    copy = SYSTEM[language]
    c = Canvas(2110, language, font)
    c.header("SYSTEM ARCHITECTURE / v3", copy["title"], copy["subtitle"])
    for x, label, role in zip((80, 475, 870, 1265), copy["legend"], ("supervisor", "executor", "runtime", "artifact")):
        c.ax.add_patch(Circle((x+7, 232), 5.5, color=THEME[role], zorder=5))
        c.text(x+25, 232, label, 15.5, role)
    xs, ys, heights = (80, 640, 1200), (340, 700, 1060, 1470), (245, 245, 295, 245)
    for row, (y, height) in enumerate(zip(ys, heights)):
        c.layer(f"0{row+1}", y-55, *copy["layers"][row])
        for col, x in enumerate(xs):
            c.card(x, y, 520, height, *copy["cards"][row*3+col])
    c.arrow((605, 462), (635, 462), "supervisor", both=True)
    c.route([(1460, 335), (1460, 323), (340, 323), (340, 335)], "runtime", dashed=True)
    c.arrow((900, 593), (900, 688), "runtime")
    c.arrow((605, 822), (635, 822), "runtime")
    c.arrow((1165, 822), (1195, 822), "runtime", both=True)
    c.arrow((605, 1207), (635, 1207), "executor", both=True)
    c.arrow((1165, 1207), (1195, 1207), "runtime")
    c.route([(1725, 1190), (1760, 1190), (1760, 462), (1725, 462)], "runtime", dashed=True)
    c.text(1768, 845, copy["events"], 11.5, "runtime", rotation=90, align="center")
    c.route([(1460, 1720), (1460, 1752), (40, 1752), (40, 822), (75, 822)], "artifact", dashed=True)
    c.box(520, 1730, 760, 42, fill="background", edge="background", radius=5)
    c.text(900, 1751, copy["reuse"], 14.5, "artifact", align="center", width=720)
    c.text(80, 1810, copy["boundary"], 18, "policy", bold=True)
    for i, (tag, title, detail) in enumerate(copy["boundaries"]):
        x = 80+i*420
        c.box(x, 1850, 380, 118, fill="panel_alt", radius=12)
        c.text(x+18, 1873, tag, 11, "policy", bold=True)
        c.text(x+18, 1909, title, 18, "ink", width=344)
        c.text(x+18, 1944, detail, 14, "body", width=344)
    for i, line in enumerate(copy["notes"]):
        c.text(80, 2010+i*31, line, 13.5, "muted", width=1640)
    c.save("architecture")


LIFECYCLE = {
    "en": {
        "title": "Execution lifecycle & acceptance protocol",
        "subtitle": "Logical sequence · MCP roundtrips condensed · one executor thread across both phases",
        "lanes": [("MODEL 01", "GPT supervisor", "Plan / review / grade", "supervisor"),
                  ("LOCAL RUNTIME", "Telos coordinator", "State / dispatch / gates", "runtime"),
                  ("MODEL 02", "Executor session", "Work → closed-book", "executor"),
                  ("LOCAL RUNTIME", "Checks & evidence", "Snapshot / tests / hashes", "artifact")],
        "steps": [
            (0, 1, "Freeze requirements, private references and checks", "open / immutable contract", "supervisor"),
            (1, 2, "Dispatch public brief into an isolated worktree", "send / frozen executor binding", "executor"),
            (2, 1, "Return work and persist completion event", "event_id / supervisor receive", "executor"),
            (1, 3, "Pin commit and run private executable checks", "evaluate / snapshot / checks", "runtime"),
            (3, 1, "Checks must pass before entering the exam", "failed checks → public repair", "artifact"),
            (1, 2, "Resume SAME thread with question IDs and prompts", "tools=[] / tool_choice=none", "executor"),
            (2, 1, "Return answers; validate JSON and tool-free transport", "event_id / receive / transport evidence", "executor"),
            (1, 0, "Review pinned source and private evidence packet", "review / contract_hash / packet_id", "supervisor"),
            (0, 1, "Submit question scores, blockers and public lessons", "grade / acceptance invariants", "supervisor"),
        ],
        "invariant": "ACCEPTANCE = checks passed + score threshold + critical minima + zero blockers + unchanged evidence",
        "accepted": ("ACCEPT", "Commit + evidence retained", ["Verified lessons become reusable.", "Source-branch integration is a separate action."]),
        "repair": ("REPAIR", "Public feedback → same executor", ["Frozen criteria remain unchanged; rounds are bounded.", "Timeouts and infrastructure retries never imply acceptance."]),
        "project": "PROJECT GATE / accepted children → scratch integration → whole-project checks → original supervisor acceptance",
        "foot": "Closed-book retains prior work history. A questionnaire supplements executable tests and source review.",
    },
    "zh": {
        "title": "执行生命周期与验收协议",
        "subtitle": "逻辑时序 · 合并展示 MCP 请求与响应 · 工作与闭卷阶段恢复同一个执行者会话",
        "lanes": [("MODEL 01", "GPT 大总管", "规划 / 审核 / 评分", "supervisor"),
                  ("LOCAL RUNTIME", "Telos 协调程序", "状态 / 调度 / 门禁", "runtime"),
                  ("MODEL 02", "执行者会话", "实现 → 同会话闭卷", "executor"),
                  ("LOCAL RUNTIME", "真实检查与证据", "快照 / 测试 / 哈希", "artifact")],
        "steps": [
            (0, 1, "冻结需求、私有参考答案与真实检查", "open / immutable contract", "supervisor"),
            (1, 2, "将公开任务说明交给隔离工作区中的执行者", "send / frozen executor binding", "executor"),
            (2, 1, "提交工作结果，写入持久化完成事件", "event_id / 大总管 receive", "executor"),
            (1, 3, "固定源码提交，运行私有可执行检查", "evaluate / snapshot / checks", "runtime"),
            (3, 1, "真实检查通过后才允许进入闭卷阶段", "检查失败 → 公开返工建议", "artifact"),
            (1, 2, "恢复同一会话，仅追加题号与题目", "tools=[] / tool_choice=none", "executor"),
            (2, 1, "返回答案，验证 JSON 与无工具执行证据", "event_id / receive / transport evidence", "executor"),
            (1, 0, "原大总管审核固定源码与私有验收证据", "review / contract_hash / packet_id", "supervisor"),
            (0, 1, "提交逐题分数、阻塞项和公开经验", "grade / acceptance invariants", "supervisor"),
        ],
        "invariant": "验收不变量 = 真实检查通过 + 总分达标 + 关键题达标 + 无阻塞项 + 源码与证据未改变",
        "accepted": ("ACCEPT", "保留验收提交与证据", ["已验证经验可用于后续任务。", "合并到源分支是独立操作。"]),
        "repair": ("REPAIR", "公开返工建议 → 原执行者", ["冻结标准保持不变，重试受预算限制。", "超时或基础设施重试不会自动通过验收。"]),
        "project": "项目整体验收 / 子任务通过 → 独立工作区集成 → 整体检查 → 原大总管最终验收",
        "foot": "闭卷保留此前工作会话记忆；问卷用于补充真实测试和源码审核。",
    },
}


def lifecycle_diagram(language, font=None):
    copy = LIFECYCLE[language]
    c = Canvas(1670, language, font)
    c.header("EXECUTION PROTOCOL / v3", copy["title"], copy["subtitle"])
    centers = (260, 680, 1100, 1520)
    for x, (tag, title, detail, role) in zip(centers, copy["lanes"]):
        c.box(x-180, 235, 360, 130, edge=role)
        c.text(x, 260, tag, 11.5, role, align="center", bold=True)
        c.text(x, 303, title, 22, "ink", align="center", width=328, bold=True)
        c.text(x, 340, detail, 14, "body", align="center", width=328)
        c.ax.plot([x, x], [378, 1280], color=THEME[role], linewidth=1, alpha=.30,
                  linestyle=(0, (4, 6)), zorder=1)
    for index, (source, target, label, detail, color) in enumerate(copy["steps"]):
        y = 425+index*96
        right = max(centers[source], centers[target])
        c.ax.add_patch(Circle((102, y+7), 20, facecolor=THEME["panel_alt"], edgecolor=THEME["border"], zorder=4))
        c.text(102, y+7, f"{index+1:02}", 12.5, color, align="center", mono=True)
        c.arrow((centers[source], y+26), (centers[target], y+26), color)
        label_x = 187 if source == 0 or target == 0 else 560
        c.box(label_x-8, y-24, 1140, 44, fill="background", edge="background", radius=4)
        c.text(label_x, y-3, label, 17.5, "ink", width=1110)
        c.text(right+25 if right < 1150 else 900, y+56, detail, 11.5, color,
               width=550 if right < 1150 else 740)
        c.ax.add_patch(Circle((centers[target], y+26), 4.5, color=THEME[color], zorder=4))
    c.box(80, 1307, 1640, 62, fill="panel_alt", edge="runtime", radius=12)
    c.text(900, 1338, copy["invariant"], 16.5, "ink", align="center", width=1590)
    for x, data, color in ((80, copy["accepted"], "artifact"), (920, copy["repair"], "policy")):
        tag, title, lines = data
        c.box(x, 1395, 800, 130, edge=color)
        c.text(x+20, 1431, tag, 12.5, color, bold=True)
        c.text(x+135, 1431, title, 20, "ink", width=640, bold=True)
        for index, line in enumerate(lines):
            c.text(x+20, 1473+index*29, line, 15, width=760)
    c.text(80, 1570, copy["project"], 14, "artifact", width=1640)
    c.text(80, 1620, copy["foot"], 14, "muted", width=1640)
    c.save("lifecycle")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", help="Chinese font family, e.g. 'Noto Sans CJK SC'")
    args = parser.parse_args()
    for language in ("en", "zh"):
        selected_font = args.font if language == "zh" else None
        system_diagram(language, selected_font)
        lifecycle_diagram(language, selected_font)
