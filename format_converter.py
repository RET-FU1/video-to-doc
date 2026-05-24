"""
格式转换 — 将 Markdown 转为 txt / html
html 模式使用 md2html 模板，提供暗色模式、TOC 侧栏、代码复制等交互功能
"""
import re
from pathlib import Path
from datetime import date as date_type


_TEMPLATE_PATH = Path(__file__).parent / "template.html"


def _kebab_id(text):
    """从标题文本提取 ASCII 单词生成 kebab-case id，纯中文则返回 None"""
    words = re.findall(r"[a-zA-Z0-9]+", text)
    if words:
        return "-".join(w.lower() for w in words)
    return None


def _count_reading_minutes(text):
    """估算阅读时间（分钟），按 250 字符/分钟"""
    chars = len(re.sub(r"\s", "", text))
    return max(1, round(chars / 250))


def _extract_subtitle(md_text):
    """提取 h1 之后第一个非空段落作为副标题，最长 200 字符"""
    heading_seen = False
    lines = []
    for line in md_text.split("\n"):
        stripped = line.strip()
        if not heading_seen:
            if re.match(r"^#\s+", stripped):
                heading_seen = True
            continue
        if not stripped or re.match(r"^#{1,6}\s", stripped):
            if lines:
                break
            continue
        lines.append(stripped)
        if len(" ".join(lines)) > 200:
            break
    subtitle = " ".join(lines).strip()
    return subtitle[:200] if subtitle else ""


def _build_toc_and_body(md_text):
    """解析 markdown，返回 (toc_html, body_html)"""
    # === 第一遍：扫描原始 markdown 提取标题信息 ===
    toc_entries = []  # [(id, text, level_class), ...]
    heading_counter = 0
    for line in md_text.split("\n"):
        m = re.match(r"^(#{2,3})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            heading_counter += 1
            kid = _kebab_id(text) or f"section-{heading_counter}"
            lvl_class = "lvl-2" if level == 2 else "lvl-3"
            toc_entries.append((kid, text, lvl_class))

    # === 第二遍：HTML 转换 ===
    text = md_text
    # 转义 HTML
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 代码块占位
    code_blocks = []

    def _store_block(m):
        code_blocks.append(m.group(0))
        return f"\x00B{len(code_blocks) - 1}\x00"

    text = re.sub(r"```.*?```", _store_block, text, flags=re.DOTALL)

    # 行内代码占位
    code_spans = []

    def _store_code(m):
        code_spans.append(m.group(1))
        return f"\x00C{len(code_spans) - 1}\x00"

    text = re.sub(r"`(.+?)`", _store_code, text)

    # 标题：h2/h3 带 id，h4 不带，h1 移除（模板 doc-title 已有）
    def _replace_heading(m):
        nonlocal heading_counter
        level = len(m.group(1))
        heading_text = m.group(2)
        # 在 toc_entries 中找到匹配的 id
        for kid, t, _ in toc_entries:
            if t == heading_text:
                return f'<h{level} id="{kid}">{heading_text}</h{level}>'
        return f"<h{level}>{heading_text}</h{level}>"

    text = re.sub(r"^(#{2,3})\s+(.+)$", _replace_heading, text, flags=re.MULTILINE)
    text = re.sub(r"^####\s+(.+)$", r"<h4>\1</h4>", text, flags=re.MULTILINE)
    text = re.sub(r"^#\s+(.+)$", "", text, flags=re.MULTILINE)

    # 粗体 / 斜体
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)

    # 图片 / 链接
    text = re.sub(r"!\[(.*?)\]\((.+?)\)", r'<img src="\2" alt="\1">', text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', text)

    # 分割线
    text = re.sub(r"^[-*_]{3,}$", "<hr>", text, flags=re.MULTILINE)
    # 引用
    text = re.sub(r"^>\s?(.+)$", r"<blockquote>\1</blockquote>", text, flags=re.MULTILINE)
    # 列表
    text = re.sub(r"^[\s]*[-*+]\s+(.+)$", r"<li>\1</li>", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*\d+\.\s+(.+)$", r"<li>\1</li>", text, flags=re.MULTILINE)

    # 还原代码占位
    for i, code in enumerate(code_spans):
        text = text.replace(f"\x00C{i}\x00", f"<code>{code}</code>")
    for i, block in enumerate(code_blocks):
        inner = re.sub(r"^```\w*\n?", "", block)
        inner = re.sub(r"```$", "", inner)
        text = text.replace(f"\x00B{i}\x00", f"<pre><code>{inner}</code></pre>")

    # 段落包裹
    paragraphs = text.split("\n\n")
    result = []
    in_list = False
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue

        if re.match(r"^<li>", p):
            if not in_list:
                result.append("<ul>")
                in_list = True
            result.append(p)
        else:
            if in_list:
                result.append("</ul>")
                in_list = False
            if re.match(r"^<h[2-4]|<hr>|<pre>|<blockquote>|<ul>|<ol>", p):
                result.append(p)
            else:
                result.append(f"<p>{p}</p>")
    if in_list:
        result.append("</ul>")

    body_html = "\n".join(result)
    toc_html = "\n".join(
        f'<a href="#{kid}" class="{lvl}">{text}</a>'
        for kid, text, lvl in toc_entries
    )

    return toc_html, body_html


# ---- 公开 API ----


def md_to_txt(text):
    """Markdown → 纯文本"""
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    text = re.sub(r"!\[.*?\]\(.+?\)", "", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"^[-*_]{3,}$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def md_to_html(md_text, title=None, source_file=None, date=None):
    """Markdown → 完整的交互式 HTML 页面（md2html 模板）"""
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")

    # 从 markdown 提取 h1（如果调用方未提供 title）
    h1_match = re.search(r"^#\s+(.+)$", md_text, re.MULTILINE)
    if not title and h1_match:
        title = h1_match.group(1).strip()
    title = title or "文档"

    subtitle = _extract_subtitle(md_text)
    minutes = _count_reading_minutes(md_text)
    read_time = f"约 {minutes} 分钟阅读"
    source = source_file or ""
    today = date or date_type.today().isoformat()
    toc_html, body_html = _build_toc_and_body(md_text)

    # 填充模板
    html = template
    for placeholder, value in {
        "{{LANG}}": "zh",
        "{{REC_LABEL}}": "★ 推荐",
        "{{TITLE}}": title,
        "{{SUBTITLE}}": subtitle,
        "{{DOC_TYPE}}": "NOTES",
        "{{SOURCE_FILE}}": source,
        "{{DATE}}": today,
        "{{READ_TIME}}": read_time,
        "{{BRAND_LABEL}}": "笔记",
        "{{TOC_TITLE}}": "目录",
        "{{PRINT_TOOLTIP}}": "打印 / 保存 PDF",
        "{{THEME_TOOLTIP}}": "切换主题",
        "{{CLOSE_LABEL}}": "关闭",
        "{{SKIP_LINK_LABEL}}": "跳到正文",
        "{{FOOTER_NOTE}}": f"来源: {source}" if source else "Generated by Video-to-Doc",
        "<!-- TOC_ENTRIES -->": toc_html,
    }.items():
        html = html.replace(placeholder, value)

    # 替换内容区
    html = html.replace("<!-- CONTENT_START -->", "")
    html = html.replace("<!-- CONTENT_END -->", "")
    html = html.replace(
        "<!-- Inject components here. See components.md for the catalog. -->",
        body_html,
    )

    return html


def save_formats(md_text, base_path, formats, meta=None):
    """根据选中的格式保存文件，返回保存的文件路径列表"""
    if meta is None:
        meta = {}
    saved = []
    title = meta.get("title", "")
    source_file = meta.get("source_file", base_path.name)

    for fmt in formats:
        if fmt == "md":
            path = base_path.with_suffix(".md")
            path.write_text(md_text, encoding="utf-8")
            saved.append(path)
        elif fmt == "html":
            path = base_path.with_suffix(".html")
            converted = md_to_html(md_text, title=title, source_file=source_file)
            path.write_text(converted, encoding="utf-8")
            saved.append(path)
        elif fmt == "txt":
            path = base_path.with_suffix(".txt")
            converted = md_to_txt(md_text)
            path.write_text(converted, encoding="utf-8")
            saved.append(path)
    return saved
