from __future__ import annotations

import html
from pathlib import Path

from .models import IMAGE_EXTS, VIDEO_EXTS
from .storage import atomic_json_write


GALLERY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light dark; --bg: #111; --fg: #eee; --muted: #9a9a9a; --card: #1c1c1c; }}
    @media (prefers-color-scheme: light) {{
      :root {{ --bg: #f6f6f4; --fg: #1b1b1b; --muted: #666; --card: #fff; }}
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font: 15px/1.45 system-ui, sans-serif; background: var(--bg); color: var(--fg); }}
    header {{ padding: 28px 24px 8px; }}
    h1 {{ font-size: 1.25rem; font-weight: 600; margin: 0 0 6px; }}
    p {{ margin: 0; color: var(--muted); }}
    main {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; padding: 20px 24px 40px; }}
    figure {{ margin: 0; background: var(--card); border-radius: 10px; overflow: hidden; }}
    img, video {{ display: block; width: 100%; height: 220px; object-fit: cover; background: #000; }}
    figcaption {{ padding: 8px 10px 10px; font-size: 12px; color: var(--muted); word-break: break-all; }}
    a {{ color: inherit; text-decoration: none; }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <p>{count} files · {board} · {thread}</p>
  </header>
  <main>
    {items}
  </main>
</body>
</html>
"""


def write_thread_archive(directory: Path, payload: dict, title: str) -> None:
    posts = []
    for post in payload.get("posts", []):
        if not isinstance(post, dict):
            continue
        posts.append(
            {
                "no": post.get("no"),
                "time": post.get("time"),
                "name": post.get("name"),
                "sub": post.get("sub"),
                "com": post.get("com"),
                "filename": post.get("filename"),
                "ext": post.get("ext"),
                "tim": post.get("tim"),
                "fsize": post.get("fsize"),
                "w": post.get("w"),
                "h": post.get("h"),
                "md5": post.get("md5"),
            }
        )
    atomic_json_write(directory / "thread.json", {"title": title, "posts": posts})


def write_gallery(directory: Path, media: dict[str, dict], title: str, board: str, thread_no: int) -> None:
    items: list[str] = []
    for key, row in media.items():
        name = str(row.get("filename") or "")
        path = directory / name
        if not name or not path.is_file():
            continue
        ext = path.suffix.lower()
        caption = html.escape(name)
        href = html.escape(name)
        if ext in IMAGE_EXTS:
            items.append(
                f'<figure><a href="{href}"><img src="{href}" alt="{caption}"></a><figcaption>{caption}</figcaption></figure>'
            )
        elif ext in VIDEO_EXTS:
            items.append(
                f'<figure><video src="{href}" controls preload="metadata"></video><figcaption>{caption}</figcaption></figure>'
            )
    (directory / "gallery.html").write_text(
        GALLERY_HTML.format(
            title=html.escape(title),
            count=len(items),
            board=html.escape(f"/{board}/"),
            thread=thread_no,
            items="\n    ".join(items) or "<p>No media saved yet.</p>",
        ),
        encoding="utf-8",
    )
