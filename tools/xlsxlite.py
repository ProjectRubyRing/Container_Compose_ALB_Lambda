"""
xlsxlite — 追加インストール不要の最小 xlsx ライタ (Python 標準ライブラリのみ)

xlsx は「XML を集めた zip」なので、openpyxl や pandas を入れなくても
zipfile + 文字列組み立てだけで書式付きのブックを生成できる。
検証環境に pip install を強制しないため、レポート出力はこれを使う。

対応しているもの:
  - 複数シート / 文字列・数値・真偽値セル
  - 名前付きスタイル (見出し・罫線・等幅・PASS/FAIL 色分け など)
  - 列幅 / 行高 / ウィンドウ枠の固定 / オートフィルタ / セル結合

使い方:
    wb = Workbook()
    sh = wb.add_sheet("結果")
    sh.widths([6, 40, 12])
    sh.row(["#", "項目", "判定"], style="header")
    sh.row([1, "HTTP ステータス", C("PASS", "ok")])
    sh.freeze(rows=1)
    wb.save("report.xlsx")
"""

import datetime
import re
import zipfile

__all__ = ["Workbook", "Sheet", "C", "STYLES"]

# ---------------------------------------------------------------------------
# スタイル名 -> cellXfs のインデックス
# ---------------------------------------------------------------------------
STYLES = {
    "plain": 0,       # 罫線なし素の文字
    "title": 1,       # シート先頭の大見出し
    "subtitle": 2,    # 大見出しの下の補足
    "header": 3,      # 表のヘッダ行 (紺地に白文字)
    "cell": 4,        # 通常セル (罫線 + 折り返し)
    "mono": 5,        # 等幅 (本文・ヘッダ値など)
    "label": 6,       # 左側の項目名セル
    "ok": 7,          # PASS
    "ng": 8,          # FAIL
    "warn": 9,        # SKIP / 注意
    "num": 10,        # 整数 (#,##0)
    "ms": 11,         # 小数 1 桁 (応答時間など)
    "muted": 12,      # 補足の小さい文字
    "screen": 13,     # 画面表示テキスト (等幅・折り返しなし・罫線なし)
    "screenhdr": 14,  # 画面表示の見出し行
    "section": 15,    # 表中の区切り行
    "center": 16,     # 中央寄せ
}

_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2"><numFmt numFmtId="164" formatCode="#,##0"/><numFmt numFmtId="165" formatCode="0.0"/></numFmts>
<fonts count="11">
<font><sz val="11"/><color theme="1"/><name val="Yu Gothic UI"/><family val="2"/></font>
<font><b/><sz val="11"/><color theme="1"/><name val="Yu Gothic UI"/><family val="2"/></font>
<font><b/><sz val="14"/><color rgb="FF1F3A5F"/><name val="Yu Gothic UI"/><family val="2"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Yu Gothic UI"/><family val="2"/></font>
<font><sz val="10"/><color theme="1"/><name val="Consolas"/><family val="3"/></font>
<font><sz val="9"/><color rgb="FF6B7280"/><name val="Yu Gothic UI"/><family val="2"/></font>
<font><b/><sz val="11"/><color rgb="FF0F7B3F"/><name val="Yu Gothic UI"/><family val="2"/></font>
<font><b/><sz val="11"/><color rgb="FFB42318"/><name val="Yu Gothic UI"/><family val="2"/></font>
<font><b/><sz val="10"/><color rgb="FF1F3A5F"/><name val="Consolas"/><family val="3"/></font>
<font><sz val="10"/><color theme="1"/><name val="MS Gothic"/><family val="3"/></font>
<font><b/><sz val="10"/><color rgb="FF1F3A5F"/><name val="MS Gothic"/><family val="3"/></font>
</fonts>
<fills count="8">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF1F3A5F"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFEEF2F7"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE6F4EA"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFCE8E6"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFF2F4F7"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFFF4CE"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FFD0D5DD"/></left><right style="thin"><color rgb="FFD0D5DD"/></right>\
<top style="thin"><color rgb="FFD0D5DD"/></top><bottom style="thin"><color rgb="FFD0D5DD"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="17">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="5" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="3" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"\
 applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"\
 applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="4" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1"\
 applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="1" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"\
 applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="6" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"\
 applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="7" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"\
 applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="0" fillId="7" borderId="1" xfId="0" applyFill="1" applyBorder="1"\
 applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"\
 applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>
<xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"\
 applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>
<xf numFmtId="0" fontId="5" fillId="0" borderId="0" xfId="0" applyFont="1"\
 applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="9" fillId="0" borderId="0" xfId="0" applyFont="1"\
 applyAlignment="1"><alignment vertical="top"/></xf>
<xf numFmtId="0" fontId="10" fillId="0" borderId="0" xfId="0" applyFont="1"\
 applyAlignment="1"><alignment vertical="top"/></xf>
<xf numFmtId="0" fontId="1" fillId="6" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"\
 applyAlignment="1"><alignment vertical="center"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"\
 applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

# Excel の 1 セルに入る文字数の上限
MAX_CELL_CHARS = 32767
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class C:
    """セル単位でスタイルを変えたいときのラッパ: C("PASS", "ok")"""

    __slots__ = ("value", "style")

    def __init__(self, value, style="cell"):
        self.value = value
        self.style = style


def _esc(text):
    text = _ILLEGAL_XML.sub("", str(text))
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def col_letter(idx):
    """0 -> A, 25 -> Z, 26 -> AA"""
    name = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        name = chr(65 + rem) + name
    return name


class Sheet:
    def __init__(self, name):
        self.name = name
        self._rows = []          # [(cells, height)]
        self._widths = []
        self._freeze = None      # (rows, cols)
        self._autofilter = None  # (row, col_from, col_to)
        self._merges = []

    # ---- 組み立て ----
    def row(self, values, style="cell", height=None):
        """1 行追加する。値は生値か C(value, style)。"""
        cells = [v if isinstance(v, C) else C(v, style) for v in values]
        self._rows.append((cells, height))
        return len(self._rows)  # 1 始まりの行番号

    def blank(self, n=1):
        for _ in range(n):
            self._rows.append(([], None))

    def widths(self, widths):
        self._widths = list(widths)

    def freeze(self, rows=0, cols=0):
        self._freeze = (rows, cols)

    def autofilter(self, header_row, col_from=0, col_to=None):
        self._autofilter = (header_row, col_from, col_to)

    def merge(self, row, col_from, col_to):
        self._merges.append((row, col_from, col_to))

    # ---- XML 化 ----
    def _xml(self):
        out = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        ]
        ncols = max([len(r) for r, _ in self._rows] + [len(self._widths), 1])
        nrows = max(len(self._rows), 1)
        out.append('<dimension ref="A1:%s%d"/>' % (col_letter(ncols - 1), nrows))

        if self._freeze and (self._freeze[0] or self._freeze[1]):
            r, c = self._freeze
            cell = "%s%d" % (col_letter(c), r + 1)
            pane = '<pane xSplit="%d" ySplit="%d" topLeftCell="%s" activePane="bottomRight" state="frozen"/>' % (
                c, r, cell,
            )
            out.append('<sheetViews><sheetView workbookViewId="0">%s'
                       '<selection pane="bottomRight" activeCell="%s" sqref="%s"/>'
                       "</sheetView></sheetViews>" % (pane, cell, cell))
        else:
            out.append('<sheetViews><sheetView workbookViewId="0"/></sheetViews>')

        out.append('<sheetFormatPr defaultRowHeight="16.5"/>')

        if self._widths:
            out.append("<cols>")
            for i, w in enumerate(self._widths):
                out.append('<col min="%d" max="%d" width="%s" customWidth="1"/>' % (i + 1, i + 1, w))
            out.append("</cols>")

        out.append("<sheetData>")
        for r_idx, (cells, height) in enumerate(self._rows, start=1):
            if not cells:
                out.append('<row r="%d"/>' % r_idx)
                continue
            attrs = ' ht="%s" customHeight="1"' % height if height else ""
            out.append('<row r="%d"%s>' % (r_idx, attrs))
            for c_idx, cell in enumerate(cells):
                out.append(self._cell_xml(col_letter(c_idx) + str(r_idx), cell))
            out.append("</row>")
        out.append("</sheetData>")

        if self._autofilter:
            row, cf, ct = self._autofilter
            ct = ncols - 1 if ct is None else ct
            out.append('<autoFilter ref="%s%d:%s%d"/>' % (
                col_letter(cf), row, col_letter(ct), max(len(self._rows), row)))

        if self._merges:
            out.append('<mergeCells count="%d">' % len(self._merges))
            for row, cf, ct in self._merges:
                out.append('<mergeCell ref="%s%d:%s%d"/>' % (
                    col_letter(cf), row, col_letter(ct), row))
            out.append("</mergeCells>")

        out.append("</worksheet>")
        return "".join(out)

    @staticmethod
    def _cell_xml(ref, cell):
        style = STYLES.get(cell.style, STYLES["cell"])
        value = cell.value
        if value is None or value == "":
            return '<c r="%s" s="%d"/>' % (ref, style)
        if isinstance(value, bool):
            value = "TRUE" if value else "FALSE"
        elif isinstance(value, (int, float)):
            return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, style, value)
        text = str(value)
        if len(text) > MAX_CELL_CHARS:
            text = text[: MAX_CELL_CHARS - 15] + "…(以下省略)"
        return '<c r="%s" s="%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (
            ref, style, _esc(text),
        )


class Workbook:
    def __init__(self, title="report"):
        self.title = title
        self.sheets = []

    def add_sheet(self, name):
        sheet = Sheet(self._safe_name(name))
        self.sheets.append(sheet)
        return sheet

    def _safe_name(self, name):
        name = re.sub(r"[\[\]:*?/\\]", "_", str(name))[:31] or "Sheet"
        existing = {s.name for s in self.sheets}
        if name not in existing:
            return name
        for i in range(2, 100):
            candidate = "%s_%d" % (name[:28], i)
            if candidate not in existing:
                return candidate
        return name[:28] + "_x"

    def save(self, path):
        if not self.sheets:
            self.add_sheet("Sheet1")
        n = len(self.sheets)

        types = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '<Default Extension="xml" ContentType="application/xml"/>',
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.spreadsheetml.sheet.main+xml"/>',
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.spreadsheetml.styles+xml"/>',
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-'
            'package.core-properties+xml"/>',
        ]
        for i in range(1, n + 1):
            types.append(
                '<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.'
                'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i
            )
        types.append("</Types>")

        rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/'
            'relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            "</Relationships>"
        )

        sheets_xml = "".join(
            '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (_esc(s.name), i, i)
            for i, s in enumerate(self.sheets, start=1)
        )
        workbook = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>%s</sheets></workbook>" % sheets_xml
        )

        wb_rels = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
        ]
        for i in range(1, n + 1):
            wb_rels.append(
                '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/'
                '2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i, i)
            )
        wb_rels.append(
            '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/styles" Target="styles.xml"/>' % (n + 1)
        )
        wb_rels.append("</Relationships>")

        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        core = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
            'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/'
            'XMLSchema-instance">'
            "<dc:title>%s</dc:title><dc:creator>albcheck</dc:creator>"
            '<cp:lastModifiedBy>albcheck</cp:lastModifiedBy>'
            '<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created>'
            '<dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified>'
            "</cp:coreProperties>" % (_esc(self.title), now, now)
        )

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "".join(types))
            z.writestr("_rels/.rels", rels)
            z.writestr("docProps/core.xml", core)
            z.writestr("xl/workbook.xml", workbook)
            z.writestr("xl/_rels/workbook.xml.rels", "".join(wb_rels))
            z.writestr("xl/styles.xml", _STYLES_XML)
            for i, sheet in enumerate(self.sheets, start=1):
                z.writestr("xl/worksheets/sheet%d.xml" % i, sheet._xml())
        return path
