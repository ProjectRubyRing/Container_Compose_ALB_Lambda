"""
textrender — レスポンス本文をテキストブラウザ風にきれいに描画する (標準ライブラリのみ)

メンテナンス画面が「実際にどう見えるか」を、ブラウザを開かずターミナルで確認するための
モジュール。日本語 (全角) を含む行でも枠がずれないよう、East Asian Width を見て
表示幅を計算する。

描画結果は (スタイル名, 文字列) の行リストで返すので、
  - ターミナル出力 : ANSI 色を付けて表示   -> to_ansi()
  - Excel 出力     : 素のテキストを等幅で出力 (tools/report_excel.py)
の両方に同じ描画結果を使い回せる。

w3m / lynx / links がインストールされていればそちらでの描画も選べる (renderer 引数)。
"""

import html as html_mod
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from html.parser import HTMLParser

__all__ = [
    "render_body", "render_html", "render_json", "render_plain",
    "frame", "to_ansi", "display_width", "wrap_text", "enable_ansi",
    "available_external_renderers",
]

# ---------------------------------------------------------------------------
# 表示幅 (East Asian Width)
# ---------------------------------------------------------------------------
_ZERO_WIDTH = {"\u200b", "\u200d", "\ufeff"}


def char_width(ch):
    """端末上での 1 文字の表示幅 (0 / 1 / 2)"""
    if ch in _ZERO_WIDTH:
        return 0
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0          # 結合文字・異体字セレクタ (U+FE0F 等)
    code = ord(ch)
    if 0x1F000 <= code <= 0x1FAFF:
        return 2          # 絵文字はほぼ全角
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text):
    return sum(char_width(c) for c in text)


def trim_to_width(text, width):
    """表示幅が width を超えないよう末尾を切り詰める"""
    out, used = [], 0
    for ch in text:
        w = char_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def pad_to_width(text, width):
    return text + " " * max(0, width - display_width(text))


def wrap_text(text, width):
    """全角混じりでも崩れない折り返し。
    半角語は語境界で、全角文字は任意位置で折る (テキストブラウザと同じ挙動)。"""
    if width <= 0:
        return [text]
    lines, line, line_w = [], "", 0
    # 「半角語のかたまり」または「1 文字」単位に分割する
    for token in re.findall(r"[\x21-\x7e]+|\s|.", text, re.S):
        if token == "\n":
            lines.append(line.rstrip())
            line, line_w = "", 0
            continue
        tw = display_width(token)
        if token.isspace():
            if line_w == 0:
                continue          # 行頭の空白は捨てる
            if line_w + tw > width:
                lines.append(line.rstrip())
                line, line_w = "", 0
                continue
        elif line_w + tw > width and line_w > 0:
            lines.append(line.rstrip())
            line, line_w = "", 0
        if tw > width:            # 1 語が幅を超える場合は強制分割
            for ch in token:
                cw = char_width(ch)
                if line_w + cw > width:
                    lines.append(line.rstrip())
                    line, line_w = "", 0
                line += ch
                line_w += cw
            continue
        line += token
        line_w += tw
    if line.strip() or not lines:
        lines.append(line.rstrip())
    return lines


# ---------------------------------------------------------------------------
# HTML -> ブロック列
# ---------------------------------------------------------------------------
# 中身をまったく表示しない要素 (終了タグを持つものだけ列挙する)
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
# 何もしない要素。空要素 (終了タグを持たない) をここに入れないと入れ子が壊れる
_IGNORE_TAGS = {
    "html", "head", "body", "meta", "link", "base", "col", "colgroup",
    "source", "track", "wbr", "input", "param", "area", "embed", "tbody",
    "thead", "tfoot", "span", "a", "strong", "b", "em", "i", "code", "small",
}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "ul", "ol", "dl", "form", "figure", "figcaption", "address", "nav",
}


class _HtmlToBlocks(HTMLParser):
    """テキストブラウザ相当のブロック分解。整形は layout() 側で行う。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.blocks = []          # [(kind, text, level)]
        self._buf = []
        self._skip = 0
        self._in_title = False
        self._pre = 0
        self._list = []           # [(type, counter)]
        self._table = None        # 組み立て中の表
        self._row = None
        self._pending = "para"    # 次にフラッシュするブロック種別

    # ---- 内部ヘルパ ----
    def _flush(self, kind=None):
        text = "".join(self._buf)
        self._buf = []
        if self._pre:
            body = text.strip("\n")
        else:
            body = re.sub(r"[ \t\r\f\v]+", " ", text).strip()
        kind = kind or self._pending
        self._pending = "para"
        if self._row is not None:
            # セル内のブロック要素は 1 セルの中に収める (行として切り出さない)
            self._buf = [body + " "] if body else []
            return
        if not body:
            return
        level = len(self._list)
        if kind.startswith("h") and len(kind) == 2:
            level = int(kind[1])
        self.blocks.append((kind, body, level))

    def _add(self, kind, text="", level=0):
        self.blocks.append((kind, text, level))

    # ---- タグ ----
    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip or tag in _IGNORE_TAGS:
            return
        if tag == "title":
            self._in_title = True
            self._buf = []
            return
        if tag == "br":
            self._buf.append("\n")
            return
        if tag == "hr":
            self._flush()
            self._add("rule")
            return
        if tag == "pre":
            self._flush()
            self._pre += 1
            self._pending = "pre"
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self._pending = tag
            return
        if tag in ("ul", "ol"):
            self._flush()
            self._list.append([tag, 0])
            return
        if tag == "li":
            self._flush()
            if self._list:
                self._list[-1][1] += 1
            self._pending = "li"
            return
        if tag == "dt":
            self._flush()
            self._pending = "dt"
            return
        if tag == "dd":
            self._flush()
            self._pending = "dd"
            return
        if tag == "table":
            self._flush()
            self._table = []
            return
        if tag == "tr" and self._table is not None:
            self._row = []
            return
        if tag in ("td", "th") and self._row is not None:
            self._buf = []
            return
        if tag == "img":
            alt = dict(attrs).get("alt") or dict(attrs).get("src") or "image"
            self._buf.append("[画像: %s]" % alt)
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip or tag in _IGNORE_TAGS:
            return
        if tag == "title":
            self._in_title = False
            self.title = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self._buf = []
            return
        if tag == "pre":
            self._flush("pre")
            self._pre = max(0, self._pre - 1)
            return
        if tag in ("td", "th") and self._row is not None:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self._buf = []
            self._row.append(text)
            return
        if tag == "tr" and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
            return
        if tag == "table" and self._table is not None:
            rows, self._table = self._table, None
            if rows:
                self._add("table", rows)
            return
        if tag in ("ul", "ol"):
            self._flush()
            if self._list:
                self._list.pop()
            return
        if tag in ("li", "dt", "dd") or tag in _BLOCK_TAGS or tag in (
            "h1", "h2", "h3", "h4", "h5", "h6"
        ):
            self._flush(tag if tag in ("li", "dt", "dd") else None)

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self._buf.append(data)
            return
        self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _cell_text(cells):
    return [re.sub(r"\s+", " ", c).strip() for c in cells]


# ---------------------------------------------------------------------------
# ブロック列 -> 行 (スタイル名, 文字列)
# ---------------------------------------------------------------------------
def _layout(blocks, width):
    lines = []

    def emit(style, text=""):
        lines.append((style, text))

    def blank():
        if lines and lines[-1][1] != "":
            emit("blank", "")

    for kind, body, level in blocks:
        if kind == "rule":
            blank()
            emit("rule", "─" * width)
            continue

        if kind == "table":
            blank()
            lines.extend(_layout_table(body, width))
            blank()
            continue

        if kind == "pre":
            blank()
            for raw in str(body).split("\n"):
                emit("pre", "  " + trim_to_width(raw, width - 2))
            blank()
            continue

        if kind in ("h1", "h2", "h3", "h4", "h5", "h6"):
            blank()
            style = "h1" if kind in ("h1", "h2") else "h3"
            for ln in wrap_text(body, width):
                emit(style, ln)
            emit("rule", ("━" if kind in ("h1", "h2") else "─")
                 * min(width, max(4, display_width(body))))
            continue

        if kind == "li":
            indent = "  " * max(0, level - 1)
            marker = indent + "• "
            wrapped = wrap_text(body, width - display_width(marker))
            emit("bullet", marker + wrapped[0])
            for ln in wrapped[1:]:
                emit("bullet", " " * display_width(marker) + ln)
            continue

        if kind == "dt":
            blank()
            for ln in wrap_text(body, width):
                emit("key", ln)
            continue

        if kind == "dd":
            for ln in wrap_text(body, width - 4):
                emit("text", "    " + ln)
            continue

        blank()
        for ln in wrap_text(body, width):
            emit("text", ln)

    # 前後の空行を落とす
    while lines and lines[0][1] == "":
        lines.pop(0)
    while lines and lines[-1][1] == "":
        lines.pop()
    return lines


def _layout_table(rows, width):
    rows = [_cell_text(r) for r in rows]
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    # 列幅は内容に合わせつつ、全体が width に収まるよう按分する
    widths = [max(display_width(r[c]) for r in rows) for c in range(ncol)]
    budget = width - (3 * ncol + 1)
    if budget > 0 and sum(widths) > budget:
        scale = budget / sum(widths)
        widths = [max(4, int(w * scale)) for w in widths]

    def sep(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    out = [("rule", sep("┌", "┬", "┐"))]
    for i, row in enumerate(rows):
        cells = [trim_to_width(v, widths[c]) for c, v in enumerate(row)]
        line = "│ " + " │ ".join(pad_to_width(v, widths[c]) for c, v in enumerate(cells)) + " │"
        out.append(("thead" if i == 0 else "text", line))
        if i == 0 and len(rows) > 1:
            out.append(("rule", sep("├", "┼", "┤")))
    out.append(("rule", sep("└", "┴", "┘")))
    return out


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------
def render_html(source, width=78, renderer="builtin"):
    """HTML -> (タイトル, 行リスト)"""
    if renderer not in ("builtin", "auto"):
        lines = _render_external(source, renderer, width)
        if lines is not None:
            return _html_title(source), lines
    elif renderer == "auto":
        for cmd in available_external_renderers():
            lines = _render_external(source, cmd, width)
            if lines is not None:
                return _html_title(source), lines

    parser = _HtmlToBlocks()
    parser.feed(source)
    parser.close()
    return parser.title, _layout(parser.blocks, width)


def _html_title(source):
    m = re.search(r"<title[^>]*>(.*?)</title>", source, re.S | re.I)
    return html_mod.unescape(m.group(1)).strip() if m else ""


def render_json(source, width=78):
    """JSON -> 整形済みの行リスト。壊れた JSON はそのまま表示する。"""
    import json

    try:
        data = json.loads(source)
    except (ValueError, TypeError):
        return render_plain(source, width)
    pretty = json.dumps(data, ensure_ascii=False, indent=2)
    lines = []
    for raw in pretty.split("\n"):
        if display_width(raw) <= width:
            lines.append(("json", raw))
            continue
        # 長い値は切り捨てず、インデントを足して折り返す
        pad = " " * (len(raw) - len(raw.lstrip()) + 2)
        head, rest = trim_to_width(raw, width), None
        rest = raw[len(head):]
        lines.append(("json", head))
        while rest:
            chunk = trim_to_width(rest, max(8, width - len(pad)))
            lines.append(("json", pad + chunk))
            rest = rest[len(chunk):]
    return lines


def render_plain(source, width=78):
    lines = []
    for raw in str(source).split("\n"):
        raw = raw.rstrip()
        if not raw:
            lines.append(("blank", ""))
            continue
        for ln in wrap_text(raw, width):
            lines.append(("pre", ln))
    return lines


def render_body(content_type, body, width=78, renderer="builtin"):
    """Content-Type に応じて描画方法を切り替える -> (タイトル, 行リスト, 種別)"""
    ctype = (content_type or "").lower()
    if not body:
        return "", [("meta", "(本文なし)")], "empty"
    if "html" in ctype or re.match(r"\s*<!doctype html|\s*<html", body[:200], re.I):
        title, lines = render_html(body, width, renderer)
        return title, lines, "html"
    if "json" in ctype or body.lstrip()[:1] in ("{", "["):
        return "", render_json(body, width), "json"
    return "", render_plain(body, width), "text"


def frame(lines, width=78, title="", subtitle=""):
    """ブラウザウィンドウ風の枠で囲む。返り値も (スタイル, 文字列) の行リスト。"""
    inner = width - 4
    head = "┌─ " + trim_to_width(title, inner - 2) + " " if title else "┌"
    head = head + "─" * max(0, width - display_width(head) - 1) + "┐"
    out = [("border", head)]
    if subtitle:
        out.append(("border", "│ " + pad_to_width(trim_to_width(subtitle, inner), inner) + " │"))
        out.append(("border", "├" + "─" * (width - 2) + "┤"))
    out.append(("border", "│ " + " " * inner + " │"))
    for style, text in lines:
        for chunk in (_split_wide(text, inner) or [""]):
            out.append((style, "│ " + pad_to_width(chunk, inner) + " │"))
    out.append(("border", "│ " + " " * inner + " │"))
    out.append(("border", "└" + "─" * (width - 2) + "┘"))
    return out


def _split_wide(text, width):
    """枠内に収まらない行を分割する (整形済み行が想定より長い場合の保険)"""
    if display_width(text) <= width:
        return [text]
    out = []
    while text:
        chunk = trim_to_width(text, width)
        if not chunk:
            break
        out.append(chunk)
        text = text[len(chunk):]
    return out


# ---------------------------------------------------------------------------
# 外部テキストブラウザ
# ---------------------------------------------------------------------------
_EXTERNAL = {
    "w3m": ["w3m", "-dump", "-T", "text/html", "-cols", "{cols}"],
    "lynx": ["lynx", "-dump", "-stdin", "-width={cols}", "-nolist"],
    "links": ["links", "-dump", "-width", "{cols}"],
}


def available_external_renderers():
    return [name for name in _EXTERNAL if shutil.which(name)]


def _render_external(source, name, width):
    spec = _EXTERNAL.get(name)
    if not spec or not shutil.which(name):
        return None
    cmd = [a.replace("{cols}", str(width)) for a in spec]
    try:
        proc = subprocess.run(
            cmd, input=source.encode("utf-8"), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 and not proc.stdout:
        return None
    text = proc.stdout.decode("utf-8", "replace")
    return [("pre", ln.rstrip()) for ln in text.split("\n")]


# ---------------------------------------------------------------------------
# ANSI 出力
# ---------------------------------------------------------------------------
ANSI = {
    "border": "\033[38;5;244m",
    "rule": "\033[38;5;244m",
    "h1": "\033[1;36m",
    "h3": "\033[1m",
    "key": "\033[1;33m",
    "bullet": "\033[36m",
    "meta": "\033[38;5;244m",
    "thead": "\033[1m",
    "pre": "\033[38;5;250m",
    "text": "",
    "json": "",
    "blank": "",
}
RESET = "\033[0m"
_JSON_KEY = re.compile(r'("(?:[^"\\]|\\.)*")(\s*:)')
_JSON_VAL = re.compile(r'(:\s*)("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null)')


def _colorize_json(text):
    text = _JSON_KEY.sub("\033[36m\\1\033[0m\\2", text)
    return _JSON_VAL.sub("\\1\033[32m\\2\033[0m", text)


def to_ansi(lines, color=True):
    """行リストを端末表示用の文字列にする"""
    out = []
    for style, text in lines:
        if not color:
            out.append(text)
            continue
        if style == "json":
            out.append(_colorize_json(text))
            continue
        code = ANSI.get(style, "")
        out.append(code + text + RESET if code else text)
    return "\n".join(out)


def enable_ansi(stream=None):
    """ANSI を使ってよいか判定し、Windows コンソールでは VT を有効化する。"""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM"))
    return True
