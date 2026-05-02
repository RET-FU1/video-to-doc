"""
格式转换 — 将 Markdown 转为 txt / html
"""
import re


def md_to_txt(text):
    """Markdown → 纯文本"""
    # 去标题 # 号
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # 去粗体/斜体
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    # 去链接 [text](url)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    # 去图片 ![alt](url)
    text = re.sub(r"!\[.*?\]\(.+?\)", "", text)
    # 去行内代码
    text = re.sub(r"`(.+?)`", r"\1", text)
    # 去分割线
    text = re.sub(r"^[-*_]{3,}$", "", text, flags=re.MULTILINE)
    # 去引用 >
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # 去列表标记
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*\d+\.\s+", "", text, flags=re.MULTILINE)
    # 压缩多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def md_to_html(text):
    """Markdown → HTML（简化版，纯正则，零依赖）"""
    # 转义 HTML
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 代码块 ```
    def _code_block(m):
        return f"<pre><code>{m.group(1)}</code></pre>"
    text = re.sub(r"```(?:\w+)?\n?(.+?)```", _code_block, text, flags=re.DOTALL)

    # 行内代码 `
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)

    # 标题
    text = re.sub(r"^####\s+(.+)$", r"<h4>\1</h4>", text, flags=re.MULTILINE)
    text = re.sub(r"^###\s+(.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^##\s+(.+)$", r"<h2>\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"^#\s+(.+)$", r"<h1>\1</h1>", text, flags=re.MULTILINE)

    # 粗体 **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # 斜体 *text*
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)

    # 图片 ![alt](url)
    text = re.sub(r"!\[(.*?)\]\((.+?)\)", r'<img src="\2" alt="\1">', text)
    # 链接 [text](url)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', text)

    # 分割线
    text = re.sub(r"^[-*_]{3,}$", "<hr>", text, flags=re.MULTILINE)

    # 引用 > text
    text = re.sub(r"^>\s?(.+)$", r"<blockquote>\1</blockquote>", text, flags=re.MULTILINE)

    # 无序列表
    text = re.sub(r"^[\s]*[-*+]\s+(.+)$", r"<li>\1</li>", text, flags=re.MULTILINE)
    # 有序列表
    text = re.sub(r"^[\s]*\d+\.\s+(.+)$", r"<li>\1</li>", text, flags=re.MULTILINE)

    # 包裹段落
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
            if re.match(r"^<h[1-4]>|<hr>|<pre>|<blockquote>", p):
                result.append(p)
            else:
                result.append(f"<p>{p}</p>")
    if in_list:
        result.append("</ul>")

    html = "\n".join(result)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; max-width: 800px; margin: 0 auto; padding: 2em; line-height: 1.8; color: #333; }}
  h1 {{ border-bottom: 2px solid #eee; padding-bottom: 0.3em; }}
  h2 {{ border-bottom: 1px solid #eee; padding-bottom: 0.2em; }}
  code {{ background: #f4f4f4; padding: 0.15em 0.4em; border-radius: 3px; font-size: 0.9em; }}
  pre {{ background: #f4f4f4; padding: 1em; border-radius: 5px; overflow-x: auto; }}
  pre code {{ background: none; padding: 0; }}
  blockquote {{ border-left: 4px solid #ddd; margin: 0; padding: 0.5em 1em; color: #666; }}
  img {{ max-width: 100%; }}
  hr {{ border: none; border-top: 1px solid #eee; }}
  a {{ color: #0366d6; }}
</style>
</head>
<body>
{html}
</body>
</html>"""


CONVERTERS = {
    "md":  None,
    "txt": md_to_txt,
    "html": md_to_html,
}


def save_formats(md_text, base_path, formats):
    """根据选中的格式保存文件，返回保存的文件路径列表"""
    saved = []
    for fmt in formats:
        if fmt == "md":
            path = base_path.with_suffix(".md")
            path.write_text(md_text, encoding="utf-8")
            saved.append(path)
        elif fmt in CONVERTERS:
            path = base_path.with_suffix(f".{fmt}")
            converted = CONVERTERS[fmt](md_text)
            path.write_text(converted, encoding="utf-8")
            saved.append(path)
    return saved
