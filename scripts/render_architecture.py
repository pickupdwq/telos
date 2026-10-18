#!/usr/bin/env python3
"""Render the bilingual architecture as PNG and editable vector SVG assets."""
import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

ROOT = Path(__file__).resolve().parents[1]
COLORS = {"supervisor": ("#15324F", "#EAF1F8"), "executor": ("#146C80", "#E7F5F7"),
          "program": ("#6350A2", "#F0ECF9"), "accepted": ("#287551", "#EAF5EE")}
COPY = {
    "en": {
        "title": "TELOS  /  From intent to verified work",
        "subtitle": "Two model roles. One executor per task. The original GPT supervisor owns acceptance.",
        "cards": [
            ("01  GPT SUPERVISOR", "Freeze the contract", "Requirements + private questions\nReference answers + real checks", "supervisor"),
            ("02  CONFIGURABLE EXECUTOR", "Implement the task", "One persistent conversation\nIsolated Git worktree", "executor"),
            ("03  LOCAL PROGRAM", "Run executable checks", "Snapshot a fixed commit\nOnly passing checks reach the exam", "program"),
            ("04  SAME EXECUTOR", "Answer closed-book", "Same thread, model and effort\nNo tools or new source / references", "executor"),
            ("08  VERIFIED EXPERIENCE", "Reuse public lessons", "Accepted work + provenance\nSame repository, relevant paths", "accepted"),
            ("07  ACCEPTED RESULT", "Keep a reviewed commit", "Recoverable isolated worktree\nMerge / push / deploy separately", "accepted"),
            ("06  LOCAL PROGRAM", "Enforce acceptance", "Checks + score + critical minima\nNo blockers; evidence unchanged", "program"),
            ("05  ORIGINAL GPT SUPERVISOR", "Review and grade", "Inspect pinned source + evidence\nScore answers; identify blockers", "supervisor"),
        ],
        "repair": "Failed checks or rejected review → public repair feedback → same executor",
        "reuse": "reuse",
        "boundary": "LOCAL TRUST BOUNDARIES",
        "detail": "Private evaluation state  •  macOS Seatbelt  •  Credential relay  •  Persistent events and receipts",
        "foot": "Project mode: up to 3 concurrent jobs, dependency / write-scope checks, then separate integration acceptance.",
        "memory": "Closed-book preserves earlier work history. Tests and supervisor source review remain mandatory.",
    },
    "zh": {
        "title": "TELOS  /  从目标到可验证的成果",
        "subtitle": "两类模型角色 · 每项任务一个执行者 · 原 GPT 大总管负责最终验收",
        "cards": [
            ("01  GPT 大总管", "冻结任务合同", "需求 + 私有问卷\n参考答案 + 可执行检查", "supervisor"),
            ("02  可切换执行者", "实现任务", "一个持续会话\n独立 Git 工作区", "executor"),
            ("03  本地程序", "运行真实检查", "固定源码提交\n检查通过后才进入问卷", "program"),
            ("04  同一个执行者", "闭卷回答问卷", "同会话、同模型、同强度\n禁用工具，不追加源码或答案", "executor"),
            ("08  已验证经验", "复用公开经验", "已验收成果 + 可追溯来源\n匹配同仓库及相关修改范围", "accepted"),
            ("07  已验收成果", "保留审核通过的提交", "隔离工作区，可恢复\n合并、推送、部署另行执行", "accepted"),
            ("06  本地程序", "强制验收门禁", "真实检查 + 分数 + 关键题最低分\n无阻塞项，源码与证据未改变", "program"),
            ("05  原 GPT 大总管", "审核源码并评分", "查看固定源码与私有证据\n逐题评分，指出阻塞问题", "supervisor"),
        ],
        "repair": "检查失败或审核不通过 → 公开返工建议 → 原执行者继续实现",
        "reuse": "复用",
        "boundary": "本地权限边界",
        "detail": "私有验收状态  ·  macOS Seatbelt 沙箱  ·  凭据转发  ·  持久化事件与回执",
        "foot": "项目模式：最多三个并发作业，约束依赖和修改范围，集成后再做整体验收。",
        "memory": "闭卷保留此前工作记忆；真实检查与大总管源码审核始终必需。",
    },
}


def draw(language, font=None):
    copy = COPY[language]
    plt.rcParams.update({"font.family": font or ("Heiti SC" if language == "zh" else "DejaVu Sans"),
                         "svg.fonttype": "path", "svg.hashsalt": "telos-architecture-v1",
                         "axes.unicode_minus": False})
    fig, ax = plt.subplots(figsize=(18, 11.2))
    fig.patch.set_facecolor("#F8FAFD")
    ax.set_facecolor("#F8FAFD")
    ax.set_xlim(0, 16)
    ax.set_ylim(0, 10)
    ax.axis("off")
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    ax.text(.65, 9.32, copy["title"], color="#15324F", fontsize=27, weight="bold", va="center")
    ax.text(.65, 8.77, copy["subtitle"], color="#53677F", fontsize=13.5, va="center")
    xs = [.65, 4.45, 8.25, 12.05]
    width, height = 3.28, 2.05

    def card(x, y, content):
        label, title, body, role = content
        ink, fill = COLORS[role]
        ax.add_patch(FancyBboxPatch((x, y), width, height, boxstyle="round,pad=0.02,rounding_size=0.12",
                                   linewidth=1.1, edgecolor=ink, facecolor=fill))
        ax.text(x+.19, y+1.69, label, fontsize=9.1, color=ink, weight="bold", va="center")
        ax.text(x+.19, y+1.17, title, fontsize=16.2, color=ink, weight="bold", va="center")
        ax.text(x+.19, y+.55, body, fontsize=10.1, linespacing=1.6, color="#35485F", va="center")

    def arrow(start, end, dashed=False, color="#7A8CA3", radius=0):
        ax.add_patch(FancyArrowPatch(start, end, arrowstyle="-|>", mutation_scale=15,
                                    linewidth=1.5, linestyle="--" if dashed else "-",
                                    color=color, connectionstyle=f"arc3,rad={radius}"))

    for i, x in enumerate(xs):
        card(x, 6.05, copy["cards"][i])
        card(x, 2.7, copy["cards"][i+4])
    for left, right in zip(xs, xs[1:]):
        arrow((left+width+.07, 7.075), (right-.07, 7.075))
        arrow((right-.07, 3.725), (left+width+.07, 3.725))
    arrow((xs[3]+width/2, 5.98), (xs[3]+width/2, 4.82))
    # Public repair loop: deterministic checks and supervisor rejection return to work.
    ax.plot([xs[2]+width/2, xs[2]+width/2, xs[1]+width/2], [5.98, 5.5, 5.5],
            color="#A57C48", linewidth=1.4, linestyle="--")
    ax.plot([xs[2]+width/2, xs[2]+width/2], [4.82, 5.5],
            color="#A57C48", linewidth=1.4, linestyle="--")
    arrow((xs[1]+width/2, 5.5), (xs[1]+width/2, 5.98), dashed=True, color="#A57C48")
    ax.text(.85, 5.14, copy["repair"], fontsize=10.5, color="#866235", va="center")
    # Experience returns to planning, keeping private checks out of the public lessons.
    ax.plot([.57, .28, .28, .57], [3.72, 3.72, 7.07, 7.07],
            color="#287551", linewidth=1.2, linestyle="--")
    arrow((.45, 7.07), (.61, 7.07), color="#287551")
    ax.text(.14, 5.8, copy["reuse"], fontsize=8, color="#287551", rotation=90, ha="center")
    ax.add_patch(FancyBboxPatch((.65, .78), 14.68, 1.32,
                              boxstyle="round,pad=0.02,rounding_size=0.1", linewidth=0, facecolor="#EAF0F7"))
    ax.text(.9, 1.76, copy["boundary"], fontsize=10, color="#15324F", weight="bold")
    ax.text(.9, 1.35, copy["detail"], fontsize=12.3, color="#35485F")
    ax.text(.9, 1.01, copy["memory"], fontsize=10.5, color="#53677F")
    ax.text(.65, .34, copy["foot"], fontsize=10.4, color="#53677F")
    output = ROOT / "docs/assets"
    output.mkdir(parents=True, exist_ok=True)
    for extension in ("png", "svg"):
        path = output / f"architecture-{language}.{extension}"
        fig.savefig(path, dpi=160,
                    metadata={"Creator": "Telos architecture generator", "Date": None} if extension == "svg" else None)
        if extension == "svg":
            path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()).rstrip() + "\n")
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", help="Chinese font family, e.g. 'Noto Sans CJK SC'")
    args = parser.parse_args()
    draw("en")
    draw("zh", args.font)
