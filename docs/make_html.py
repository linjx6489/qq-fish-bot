#!/usr/bin/env python
"""把 Markdown 文档渲染成自包含 HTML（浏览器双击即开）。

用法：python docs/make_html.py
输出：docs/README.html、docs/character.html
"""

from __future__ import annotations

from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs"
OUT.mkdir(exist_ok=True)

CSS = """
body{font-family:"Microsoft YaHei",-apple-system,"Segoe UI",sans-serif;max-width:880px;margin:32px auto;padding:0 20px 60px;color:#24292f;line-height:1.75;background:#fff}
h1{border-bottom:2px solid #4A7FD1;padding-bottom:8px;color:#1B3B6F}
h2{border-bottom:1px solid #e1e4e8;padding-bottom:6px;margin-top:36px;color:#1B3B6F}
h3{margin-top:26px;color:#34558b}
code{background:#f6f8fa;padding:2px 6px;border-radius:4px;font-size:14px}
pre{background:#0d1117;color:#e6edf3;padding:14px 16px;border-radius:8px;overflow-x:auto}
pre code{background:none;color:inherit;padding:0}
table{border-collapse:collapse;width:100%;margin:14px 0}
th,td{border:1px solid #d0d7de;padding:8px 12px;text-align:left;font-size:14px}
th{background:#f0f4fa}
tr:nth-child(even){background:#fafbfc}
blockquote{border-left:4px solid #4A7FD1;margin:14px 0;padding:6px 16px;color:#57606a;background:#f6f8fa;border-radius:0 6px 6px 0}
a{color:#0969da;text-decoration:none}
a:hover{text-decoration:underline}
img{max-width:100%}
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
{body}
</body>
</html>
"""


def render(md_path: Path, title: str) -> None:
    html_body = markdown.markdown(
        md_path.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
    )
    (OUT / f"{md_path.stem}.html").write_text(
        TEMPLATE.format(title=title, css=CSS, body=html_body), encoding="utf-8"
    )
    print(f"已生成 docs/{md_path.stem}.html（{md_path.stat().st_size} -> {(OUT / (md_path.stem + '.html')).stat().st_size} 字节）")


if __name__ == "__main__":
    render(ROOT / "README.md", "吃白饭的肥鱼 · QQ 自动回复机器人")
    render(ROOT / "assets" / "character.md", "吃白饭的肥鱼 · 角色形象设定")
    print("完成。双击 docs\\*.html 即可在浏览器打开。")
