"""GUI 设计令牌（颜色 / 字体）。

单独成文件的原因：这些值被主窗口、自定义控件、应用内目录浏览器三处共用，
放在 gui.py 里会让拆分后的模块互相 import gui，形成环。
"""
# ── 设计令牌 ──────────────────────────────────────────────
C = {
    "bg":         "#F6F6F7",   # 页面底色（近白灰）
    "card":       "#FFFFFF",   # 卡片
    "border":     "#E7E7E9",   # 细边框
    "border_2":   "#D9D9DE",   # 输入框边框
    "text":       "#18181B",   # 主文字
    "muted":      "#6F6F76",   # 次要文字
    "faint":      "#9A9AA2",   # 提示文字
    "accent":     "#4F46E5",   # 强调色（靛蓝）
    "accent_2":   "#4338CA",   # hover
    "accent_soft": "#EEF0FF",  # 浅强调底
    "hover":      "#F0F0F2",   # 中性 hover
    "danger":     "#DC2626",
    "danger_2":   "#B91C1C",
    "log_bg":     "#17171A",
    "log_fg":     "#D4D4D8",
    "log_warn":   "#FBBF24",
    "log_err":    "#F87171",
    "log_ok":     "#4ADE80",
    "log_stage":  "#93C5FD",
}

F = {
    "title":   ("Microsoft YaHei UI", 15, "bold"),
    "section": ("Microsoft YaHei UI", 10, "bold"),
    "body":    ("Microsoft YaHei UI", 10),
    "chip":    ("Microsoft YaHei UI", 9),
    "small":   ("Microsoft YaHei UI", 9),
    "mono":    ("Consolas", 9),
}
