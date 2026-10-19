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

    def card(self, x, y, width, height, tag, title, lines, footer, role="runtime"):
        self.box(x, y, width, height, edge=role)
        self.ax.plot([x+23, x+72], [y+23, y+23], color=THEME[role], linewidth=3, zorder=4)
        self.text(x+86, y+23, tag, 14, role, width=width-110, bold=True)
        self.text(x+23, y+70, title, 26, "ink", width=width-46, bold=True)
        for i, line in enumerate(lines):
            self.text(x+23, y+118+i*37, line, 20.5, width=width-46)
        self.ax.plot([x+23, x+width-23], [y+height-42, y+height-42],
                     color=THEME["border"], linewidth=.8, zorder=4)
        self.text(x+23, y+height-20, footer, 15.5, role, width=width-46)

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
        "title": "One supervisor. Evidence before acceptance.",
        "subtitle": "Your original chat stays in control. Telos coordinates execution and enforces the gates.",
        "roles": [
            ("MODEL ROLE 01", "GPT supervisor", ["Freeze goals, scope and checks", "Review evidence and grade"], "Skill guides planning and review", "supervisor"),
            ("LOCAL MCP RUNTIME", "Telos bridge", ["Dispatch work; track progress", "Enforce scope and acceptance"], "Program logic, not another model", "runtime"),
            ("MODEL ROLE 02", "Task executor", ["Work: scoped tools + worktree", "Exam: same thread, no tools"], "Fixed model, endpoint and effort", "executor"),
        ],
        "return": "Events + evidence → original chat",
        "capabilities": "WHAT THE LOCAL RUNTIME ENFORCES",
        "scope_note": "One executor per task · project scheduling",
        "controls": [
            ("SCOPE & ORCHESTRATION", "Task boundaries", ["Frozen scope, checks and model", "Single / parallel / phased", "Code, API and page audits"], "Scoped work; read-only audit source", "runtime"),
            ("EFFICIENCY & RECOVERY", "Usage and control", ["Preflight before model calls", "Compact results; full evidence", "Request and token budgets"], "Durable events; pause / resume / cancel", "runtime"),
            ("INFERENCE RELAY", "Model access", ["DeepSeek · Ark · OpenAI", "Local: Responses / Chat", "Many addresses; one active"], "Credentials stay in the bridge", "runtime"),
        ],
        "state": "LOCAL STATE   /   Git snapshots · private evidence · usage and receipts · verified public lessons",
        "gate": "ACCEPTANCE GATE / ORIGINAL SUPERVISOR DECIDES",
        "sequence": "Checks → same-thread exam → supervisor review",
        "criteria": "All checks pass · ≥90 by default · critical minima · no blockers · source and evidence match",
        "output": "Accepted code / audit evidence + reusable lessons. Merge, push and deploy are separate actions.",
        "foot": "Two model roles. macOS sandbox isolation. Model endpoints are external to the Telos runtime.",
    },
    "zh": {
        "title": "大总管掌控任务，证据决定验收",
        "subtitle": "保留原始对话作为大总管；Telos 负责协调执行，并强制落实边界与验收规则。",
        "roles": [
            ("MODEL ROLE 01", "GPT 大总管", ["理解需求，冻结范围与检查", "审核源码、证据并逐题评分"], "Skill 指导规划、走查与验收", "supervisor"),
            ("LOCAL MCP RUNTIME", "Telos 桥接程序", ["分派任务，跟踪执行进度", "约束权限，落实验收门禁"], "由程序协调，不增加模型角色", "runtime"),
            ("MODEL ROLE 02", "任务执行者", ["工作：受限工具 + 独立工作区", "闭卷：同一会话，禁止工具"], "固定入口、模型与推理强度", "executor"),
        ],
        "return": "完成事件与证据回到原始对话",
        "capabilities": "本地运行时负责落实的能力",
        "scope_note": "每任务一个执行者 · 支持项目调度",
        "controls": [
            ("SCOPE & ORCHESTRATION", "任务边界与编排", ["冻结范围、检查与模型配置", "单任务 / 并行 / 分阶段", "代码、接口、页面交互与样式走查"], "开发限定写入范围；走查源码只读", "runtime"),
            ("EFFICIENCY & RECOVERY", "用量与执行控制", ["调用模型前完成环境预检", "精简返回，完整证据可补查", "记录用量，限制请求数与 token"], "持久回传与回执；暂停 / 恢复 / 取消", "runtime"),
            ("INFERENCE RELAY", "模型入口与转发", ["DeepSeek / 火山 / OpenAI", "本地：Responses / Chat", "可保存多个地址，仅启用一个"], "上游凭据只留在桥接程序中", "runtime"),
        ],
        "state": "本地状态与产物  /  Git 快照 · 私有验收证据 · 用量与事件回执 · 已验证的公开经验",
        "gate": "验收门禁 / 原大总管拥有最终决定权",
        "sequence": "真实检查 → 原执行者同会话闭卷 → 大总管审核评分",
        "criteria": "检查全通过 · 默认 ≥90 分 · 关键题达标 · 无阻塞项 · 源码与证据一致",
        "output": "产出已验收代码或走查证据，并积累公开经验。合并、推送、部署另行执行。",
        "foot": "只有两类模型角色；工作由 macOS 沙箱隔离，模型服务位于 Telos 运行时之外。",
    },
}


def system_diagram(language, font=None):
    copy = SYSTEM[language]
    c = Canvas(1470, language, font)
    c.header("SYSTEM ARCHITECTURE", copy["title"], copy["subtitle"])
    xs = (80, 640, 1200)
    for x, data in zip(xs, copy["roles"]):
        c.card(x, 260, 520, 260, *data)
    c.arrow((605, 390), (635, 390), "supervisor")
    c.arrow((1165, 390), (1195, 390), "executor", both=True)
    c.route([(900, 525), (900, 570), (340, 570), (340, 525)], "runtime", dashed=True)
    c.box(363, 548, 514, 44, fill="background", edge="background", radius=4)
    c.text(620, 570, copy["return"], 17, "runtime", align="center", width=500)
    c.text(80, 635, copy["capabilities"], 22, "ink", bold=True, width=1000)
    c.text(1720, 635, copy["scope_note"], 15.5, "muted", align="right", width=620)
    c.ax.plot([80, 1720], [665, 665], color=THEME["border"], linewidth=.8, zorder=1)
    for x, data in zip(xs, copy["controls"]):
        c.card(x, 700, 520, 260, *data)
    c.box(80, 1012, 1640, 60, fill="panel_alt", edge="border", radius=12)
    c.text(900, 1042, copy["state"], 17.5, "artifact", align="center", width=1570)
    c.arrow((900, 1082), (900, 1130), "artifact")
    c.box(80, 1140, 1640, 240, fill="panel_alt", edge="artifact", radius=18)
    c.text(110, 1172, copy["gate"], 15.5, "artifact", bold=True, width=1580)
    c.text(110, 1220, copy["sequence"], 28, "ink", bold=True, width=1580)
    c.text(110, 1266, copy["criteria"], 20.5, width=1580)
    c.ax.plot([110, 1690], [1298, 1298], color=THEME["border"], linewidth=.8, zorder=4)
    c.text(110, 1338, copy["output"], 18.5, "artifact", width=1580)
    c.text(80, 1430, copy["foot"], 16, "muted", width=1640)
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
    parser.add_argument("--diagram", choices=("architecture", "lifecycle", "all"), default="all",
                        help="Select the diagrams to regenerate (default: all)")
    args = parser.parse_args()
    for language in ("en", "zh"):
        selected_font = args.font if language == "zh" else None
        if args.diagram in ("architecture", "all"):
            system_diagram(language, selected_font)
        if args.diagram in ("lifecycle", "all"):
            lifecycle_diagram(language, selected_font)
