"""
report_excel — albcheck の実行結果を Excel ブックに書き出す

出力されるシート:
  サマリ            実行条件と PASS/FAIL の集計
  検証結果          ケース 1 件 = 1 行 (ステータス・適用ルール・転送先・応答時間…)
  チェック明細      「何を期待して実際どうだったか」を 1 行ずつ
  レスポンスヘッダ  全レスポンスヘッダ (ALB / Lambda / ECS / 標準に分類)
  レスポンス本文    受け取った本文の生データ
  画面表示          テキストブラウザ描画をそのまま (等幅フォントで貼り付け)
  Lambda契約チェック  ALB Lambda 統合の契約適合結果 (--contract 実行時)
"""

import textrender as T
from xlsxlite import C, Workbook

VERDICT_STYLE = {"PASS": "ok", "FAIL": "ng", "WARN": "warn", "INFO": "center"}


def _v(verdict):
    return C(verdict, VERDICT_STYLE.get(verdict, "center"))


def _header_group(name):
    low = name.lower()
    if low.startswith(("x-alb-", "x-amzn-")):
        return "ALB"
    if low.startswith("x-maintenance"):
        return "Lambda"
    if low.startswith("x-backend"):
        return "ECS"
    return "標準"


def build(runs, path):
    """runs: albcheck.Run のリスト -> xlsx を path に書き出す"""
    wb = Workbook("ALB × Lambda メンテナンス応答 検証レポート")
    _sheet_summary(wb, runs)
    _sheet_cases(wb, runs)
    _sheet_checks(wb, runs)
    _sheet_headers(wb, runs)
    _sheet_bodies(wb, runs)
    _sheet_screens(wb, runs)
    _sheet_contract(wb, runs)
    return wb.save(path)


# ---------------------------------------------------------------------------
def _sheet_summary(wb, runs):
    sh = wb.add_sheet("サマリ")
    sh.widths([28, 26, 12, 10, 10, 10, 46])
    row = sh.row(["ALB × Lambda メンテナンス応答 検証レポート"], style="title")
    sh.merge(row, 0, 6)
    sh.row(["ALB のリスナールールがメンテナンス中に Lambda ターゲットグループへ"
            "フォワードし、Web は HTML 画面・API は HTTP ステータスコード + JSON を"
            "返すことを確認した記録です。"], style="subtitle")
    sh.blank()

    def kv(label, value, style="cell"):
        row = sh.row([C(label, "label"), C(value, style)] + [C("", style)] * 5)
        sh.merge(row, 1, 6)

    for run in runs:
        sh.merge(sh.row(["実行条件", "値"] + [""] * 5, style="header"), 1, 6)
        kv("Lambda 実装 (variant)", run.variant)
        kv("実行開始", run.started_at)
        kv("実行終了", run.finished_at)
        kv("所要時間", "%.1f 秒" % run.elapsed if run.elapsed else "-")
        kv("画面描画の幅 / 描画方法", "%d 桁 / %s" % (run.width, run.renderer))
        kv("検証ケース数", len(run.results))
        kv("チェック項目数", run.check_total)
        kv("PASS", run.passed, "ok" if run.results else "cell")
        kv("FAIL", run.failed, "ng" if run.failed else "cell")
        kv("WARN", run.warned, "warn" if run.warned else "cell")
        sh.blank()

        if run.results:
            sh.row(["グループ別集計", "検証のねらい", "ケース数", "PASS", "FAIL", "WARN",
                    "PASS 以外のケース"], style="header")
            for name, items in _group_by(run.results):
                verdicts = [i.verdict for i in items]
                sh.row([
                    name, _GROUP_DESC.get(name, ""),
                    C(len(items), "num"),
                    C(verdicts.count("PASS"), "ok" if verdicts.count("PASS") else "cell"),
                    C(verdicts.count("FAIL"), "ng" if verdicts.count("FAIL") else "cell"),
                    C(verdicts.count("WARN"), "warn" if verdicts.count("WARN") else "cell"),
                    ", ".join(i.case.id for i in items if i.verdict != "PASS") or "-",
                ])
            sh.blank()

        if run.contract:
            sh.row(["Lambda 契約チェック", "エンドポイント", "必須NG", "推奨NG", "", "", ""],
                   style="header")
            for service, endpoint, checks, _resp, _payload in run.contract:
                ng = sum(1 for chk in checks if not chk.ok and chk.level == "必須")
                warn = sum(1 for chk in checks if not chk.ok and chk.level == "推奨")
                sh.row([service, C(endpoint, "mono"),
                        C(ng, "ng" if ng else "ok"),
                        C(warn, "warn" if warn else "ok"), "", "", ""])
            sh.blank()

    sh.merge(sh.row(["凡例", ""] + [""] * 5, style="header"), 1, 6)
    for label, desc in (
        ("PASS", "期待どおり"),
        ("FAIL", "必須項目が期待と異なる (要調査)"),
        ("WARN", "推奨項目が満たされていない (動作はするが直したい)"),
    ):
        sh.merge(sh.row([_v(label), desc] + [""] * 5), 1, 6)


_GROUP_DESC = {
    "1. 通常時": "デフォルトアクションで ECS サービスへフォワードされる",
    "2. メンテ中 (Web)": "Lambda がメンテナンス画面 (HTML) を返す / バイパスも効く",
    "3. メンテ中 (API)": "Lambda が HTTP ステータスコード + JSON を返す",
    "4. ALB ごとの独立性": "ALB 単位でメンテナンスを切り替えられる",
    "5. 復旧": "メンテナンス解除で通常応答へ戻る",
}


def _group_by(results):
    """出現順を保ったままグループごとにまとめる"""
    order, buckets = [], {}
    for result in results:
        if result.group not in buckets:
            buckets[result.group] = []
            order.append(result.group)
        buckets[result.group].append(result)
    return [(name, buckets[name]) for name in order]


# ---------------------------------------------------------------------------
_CASE_COLS = [
    ("実装", 10), ("グループ", 20), ("ID", 7), ("検証内容", 46), ("判定", 8),
    ("サービス", 11), ("メソッド", 9), ("パス", 18), ("ステータス", 10),
    ("Content-Type", 24), ("適用ルール", 28), ("優先度", 8), ("転送先", 28),
    ("Lambda実装", 11), ("メンテ", 8), ("応答(ms)", 10), ("サイズ", 10),
    ("リクエストヘッダ", 30), ("補足", 40),
]


def _sheet_cases(wb, runs):
    if not any(run.results for run in runs):
        return
    sh = wb.add_sheet("検証結果")
    sh.widths([w for _, w in _CASE_COLS])
    header = sh.row([n for n, _ in _CASE_COLS], style="header")
    sh.freeze(rows=header, cols=5)
    sh.autofilter(header)
    for run in runs:
        for result in run.results:
            resp, case = result.resp, result.case
            sh.row([
                run.variant, result.group, case.id, case.title, _v(result.verdict),
                case.service, case.method, case.path,
                C(resp.status or "ERR", "num" if resp.status else "ng"),
                resp.content_type or ("エラー: %s" % resp.error if resp.error else ""),
                resp.header("X-Alb-Matched-Rule"),
                resp.header("X-Alb-Rule-Priority"),
                resp.header("X-Alb-Target"),
                resp.header("X-Alb-Lambda-Variant"),
                resp.header("X-Alb-Maintenance"),
                C(round(resp.elapsed_ms, 1), "ms"),
                C(resp.size, "num"),
                "\n".join("%s: %s" % kv for kv in case.headers.items()),
                case.note,
            ])


# ---------------------------------------------------------------------------
def _sheet_checks(wb, runs):
    if not any(run.results for run in runs):
        return
    sh = wb.add_sheet("チェック明細")
    cols = [("実装", 10), ("ID", 7), ("検証内容", 44), ("確認項目", 30), ("区分", 8),
            ("判定", 8), ("期待値", 34), ("実際値", 40)]
    sh.widths([w for _, w in cols])
    header = sh.row([n for n, _ in cols], style="header")
    sh.freeze(rows=header)
    sh.autofilter(header)
    for run in runs:
        for result in run.results:
            for chk in result.checks:
                sh.row([run.variant, result.case.id, result.case.title, chk.label,
                        chk.level, _v(chk.verdict), str(chk.expected), str(chk.actual)])


# ---------------------------------------------------------------------------
def _sheet_headers(wb, runs):
    if not any(run.results for run in runs):
        return
    sh = wb.add_sheet("レスポンスヘッダ")
    cols = [("実装", 10), ("ID", 7), ("サービス", 11), ("ステータス", 10),
            ("分類", 9), ("ヘッダ名", 30), ("値", 78)]
    sh.widths([w for _, w in cols])
    header = sh.row([n for n, _ in cols], style="header")
    sh.freeze(rows=header)
    sh.autofilter(header)
    for run in runs:
        for result in run.results:
            resp = result.resp
            items = sorted(resp.headers.items(),
                           key=lambda kv: (["ALB", "Lambda", "ECS", "標準"].index(
                               _header_group(kv[0])), kv[0].lower()))
            for name, value in items:
                sh.row([run.variant, result.case.id, result.case.service,
                        C(resp.status, "num"), _header_group(name),
                        C(name, "mono"), C(value, "mono")])


# ---------------------------------------------------------------------------
def _sheet_bodies(wb, runs):
    if not any(run.results for run in runs):
        return
    sh = wb.add_sheet("レスポンス本文")
    cols = [("実装", 10), ("ID", 7), ("サービス", 11), ("種別", 8),
            ("Content-Type", 26), ("バイト数", 10), ("本文 (生データ)", 110)]
    sh.widths([w for _, w in cols])
    header = sh.row([n for n, _ in cols], style="header")
    sh.freeze(rows=header)
    sh.autofilter(header)
    for run in runs:
        for result in run.results:
            resp = result.resp
            sh.row([run.variant, result.case.id, result.case.service,
                    result.screen_kind, resp.content_type,
                    C(resp.size, "num"), C(resp.body, "mono")], height=60)


# ---------------------------------------------------------------------------
def _sheet_screens(wb, runs):
    """テキストブラウザ描画をそのまま貼り付ける (等幅フォント)。
    枠線がずれないよう MS Gothic 相当の等幅スタイルを使う。"""
    if not any(run.results for run in runs):
        return
    sh = wb.add_sheet("画面表示")
    cols = [("実装", 10), ("ID", 7), ("サービス", 11), ("行", 6), ("画面表示 (テキストブラウザ)", 120)]
    sh.widths([w for _, w in cols])
    header = sh.row([n for n, _ in cols], style="header")
    sh.freeze(rows=header)
    sh.autofilter(header)

    for run in runs:
        for result in run.results:
            resp, case = result.resp, result.case
            subtitle = "%s %s  →  %s %s" % (
                case.method, resp.url, resp.status, resp.content_type)
            framed = T.frame(result.screen_lines, run.width,
                             title=result.screen_title or resp.content_type.split(";")[0],
                             subtitle=T.trim_to_width(subtitle, run.width - 4))
            sh.row([run.variant, case.id, case.service, "",
                    C("%s  [%s]" % (case.title, result.verdict), "screenhdr")])
            for i, (_style, text) in enumerate(framed, start=1):
                sh.row([run.variant, case.id, case.service, C(i, "screen"),
                        C(text, "screen")])
            sh.blank()


# ---------------------------------------------------------------------------
def _sheet_contract(wb, runs):
    rows = [(run, item) for run in runs for item in run.contract]
    if not rows:
        return
    sh = wb.add_sheet("Lambda契約チェック")
    cols = [("実装", 10), ("サービス", 11), ("種別", 8), ("確認項目", 30), ("区分", 8),
            ("判定", 8), ("期待値", 30), ("実際値", 40), ("エンドポイント", 60)]
    sh.widths([w for _, w in cols])
    header = sh.row([n for n, _ in cols], style="header")
    sh.freeze(rows=header)
    sh.autofilter(header)
    web = ("intraweb",)
    for run, (service, endpoint, checks, _resp, _payload) in rows:
        for chk in checks:
            sh.row([run.variant, service, "web" if service in web else "api",
                    chk.label, chk.level, _v(chk.verdict),
                    str(chk.expected), str(chk.actual), endpoint])
