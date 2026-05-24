#!/usr/bin/env python3
"""Generate 4K wallpaper: Cursor slash commands reference."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 3840, 2160
OUT = Path(__file__).resolve().parents[1] / "assets" / "cursor-slash-commands-wallpaper-4k.png"

# macOS font fallbacks
FONT_PATHS = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]
MONO_PATHS = [
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]


def load_font(size: int, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths = MONO_PATHS if mono else FONT_PATHS
    for p in paths:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size, index=0)
            except OSError:
                continue
    return ImageFont.load_default()


SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "模式与系统",
        [
            ("/plan", "Plan 模式 — 先规划再编码"),
            ("/ask", "Ask 模式 — 只读探索"),
            ("/model", "设置或列出 AI 模型"),
            ("/auto-run [on|off|status]", "自动运行工具开关"),
            ("/sandbox", "沙箱与网络访问配置"),
            ("/max-mode [on|off]", "Max Mode 开关"),
            ("/new-chat", "新建对话"),
            ("/compress", "压缩对话释放上下文"),
            ("/summarize", "手动总结长对话"),
            ("/vim", "Vim 键位开关"),
        ],
    ),
    (
        "配置与管理",
        [
            ("/rules", "创建或编辑 Rules"),
            ("/commands", "创建或编辑自定义 Commands"),
            ("/create-rule", "交互生成 .cursor/rules"),
            ("/create-skill", "交互创建 Skill"),
            ("/migrate-to-skills", "Rules/Commands → Skills"),
            ("/mcp list", "浏览 MCP 服务器"),
            ("/mcp enable <name>", "启用 MCP"),
            ("/mcp disable <name>", "禁用 MCP"),
            ("/usage", "用量与统计"),
            ("/help [cmd]", "查看帮助"),
        ],
    ),
    (
        "工作流与 Git",
        [
            ("/worktree", "独立 git worktree 运行 Agent"),
            ("/best-of-n", "多模型并行对比结果"),
            ("/multitask", "并行子 Agent（3.3+）"),
            ("/commit", "根据改动生成 commit message"),
            ("/pr", "生成 PR 描述"),
            ("/debug", "Debug 模式定位根因"),
            ("/btw", "旁路提问不打断主任务"),
            ("/resume", "恢复历史对话"),
            ("/feedback", "提交产品反馈"),
        ],
    ),
    (
        "CLI / 其他",
        [
            ("/about", "环境与 CLI 信息"),
            ("/copy-request-id", "复制请求 ID"),
            ("/copy-conversation-id", "复制对话 ID"),
            ("/setup-terminal", "配置终端快捷键"),
            ("/logout", "登出 Cursor"),
            ("/quit", "退出 CLI"),
        ],
    ),
    (
        "Skills 与自定义",
        [
            ("/<skill-name>", "执行 .cursor/skills 等目录中的 Skill"),
            ("/@skill-name", "或用 @ 附加 Skill 为上下文"),
            ("~/.cursor/skills/", "全局 Skills"),
            (".cursor/skills/", "项目 Skills"),
            (".cursor/commands/*.md", "自定义 Command（文件名=命令名）"),
            ("~/.cursor/commands/", "全局自定义 Commands"),
        ],
    ),
    (
        "快捷对照",
        [
            ("Shift + Tab", "切换 Agent / Ask / Plan 等模式"),
            ("Cmd + .", "模式菜单（macOS）"),
            ("Cmd + /", "循环切换模型"),
            ("@ 文件 / @Docs", "附加上下文（非 / 命令）"),
        ],
    ),
]


def draw_section(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    col_w: int,
    title: str,
    items: list[tuple[str, str]],
    title_font,
    cmd_font,
    desc_font,
    colors: dict[str, str],
) -> int:
    draw.text((x, y), title, font=title_font, fill=colors["accent"])
    y += 52
    for cmd, desc in items:
        draw.text((x, y), cmd, font=cmd_font, fill=colors["cmd"])
        y += 38
        # wrap long desc if needed — keep single line for wallpaper density
        draw.text((x + 8, y), desc, font=desc_font, fill=colors["muted"])
        y += 34
    return y + 18


def main() -> None:
    colors = {
        "bg": "#0d1117",
        "bg2": "#161b22",
        "accent": "#58a6ff",
        "cmd": "#79c0ff",
        "muted": "#8b949e",
        "title": "#e6edf3",
        "sub": "#7ee787",
    }

    img = Image.new("RGB", (W, H), colors["bg"])
    draw = ImageDraw.Draw(img)

    # subtle gradient bands
    for i in range(H):
        t = i / H
        r = int(13 + 8 * t)
        g = int(17 + 10 * t)
        b = int(23 + 14 * t)
        draw.line([(0, i), (W, i)], fill=(r, g, b))

    title_font = load_font(72)
    subtitle_font = load_font(32)
    section_font = load_font(36)
    cmd_font = load_font(28, mono=True)
    desc_font = load_font(24)
    footer_font = load_font(22)

    # header bar
    draw.rectangle([0, 0, W, 200], fill=colors["bg2"])
    draw.text((80, 48), "Cursor  /  快捷命令完整参考", font=title_font, fill=colors["title"])
    draw.text(
        (80, 130),
        "在 Chat / Agent 输入框输入 /  ·  4K 3840×2160  ·  cursor.com/docs",
        font=subtitle_font,
        fill=colors["muted"],
    )
    draw.text((W - 520, 130), "macOS  ·  May 2026", font=subtitle_font, fill=colors["sub"])

    # 3 columns layout
    col_w = (W - 200) // 3
    col_x = [80, 80 + col_w + 40, 80 + 2 * (col_w + 40)]
    col_y = [240, 240, 240]

    col_idx = 0
    for si, (section_title, items) in enumerate(SECTIONS):
        x = col_x[col_idx]
        y = col_y[col_idx]
        new_y = draw_section(
            draw, x, y, col_w, section_title, items,
            section_font, cmd_font, desc_font, colors,
        )
        col_y[col_idx] = new_y
        # balance columns: after 2 sections col0, after 2 more col1, rest col2
        if si in (1, 3):
            col_idx = min(col_idx + 1, 2)

    # footer
    draw.rectangle([0, H - 88, W, H], fill=colors["bg2"])
    footer = (
        "完整列表以输入 / 后弹出的菜单为准（含本机 Skills）  ·  "
        "/ 执行命令  ·  @ 附加上下文  ·  Cmd+R Cmd+S 打开快捷键设置"
    )
    draw.text((80, H - 62), footer, font=footer_font, fill=colors["muted"])

    # corner logo mark
    draw.ellipse([W - 120, 52, W - 60, 112], outline=colors["accent"], width=3)
    draw.text((W - 108, 68), "/", font=load_font(40, mono=True), fill=colors["accent"])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print(f"Saved: {OUT} ({W}x{H})")


if __name__ == "__main__":
    main()
