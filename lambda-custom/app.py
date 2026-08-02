"""
自作 Lambda のひな形 (custom variant)
=====================================

このファイルを **自由に書き換えて** 検証してください。ALB シミュレータは
`POST /admin/lambda {"variant":"custom"}` でこのコンテナへ invoke 先を切り替えます。
ターゲットグループ ARN は builtin と同じものが渡るため、
「実 ALB からの呼び出し」とまったく同じイベントで自作実装を試せます。

    # 反映（コード編集後）
    docker compose up -d --build custom-lambda
    # 切り替え
    python tools/albcheck.py variant custom
    # 画面と詳細を確認
    python tools/albcheck.py check intraweb /dashboard

------------------------------------------------------------------------------
守るべき「ALB Lambda ターゲットグループ統合」の契約
------------------------------------------------------------------------------
受け取るイベント (dict):
    requestContext.elb.targetGroupArn   呼び出し元ターゲットグループ ARN
    httpMethod / path / queryStringParameters / headers / body / isBase64Encoded

返す値 (dict):
    statusCode        int (必須)
    statusDescription str (任意 / 例 "503 Service Unavailable")
    headers           dict[str, str]   ※ 値は必ず文字列
    multiValueHeaders dict[str, list[str]] (任意)
    body              str (必須 / bytes を返す場合は base64 にして isBase64Encoded=True)
    isBase64Encoded   bool

この契約を満たしているかは次で自動チェックできます:
    python tools/albcheck.py contract --variant custom
"""

import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# 設定 (docker-compose.yml の custom-lambda: environment: で変更できます)
# ---------------------------------------------------------------------------
WEB_SERVICES = [s.strip() for s in os.environ.get("WEB_SERVICES", "intraweb").split(",") if s.strip()]
MAINT_WINDOW = os.environ.get("MAINT_WINDOW", "本日 02:00 - 05:00 (JST)")
CONTACT = os.environ.get("CONTACT", "システム運用窓口 (内線 1234)")
RETRY_AFTER = os.environ.get("RETRY_AFTER", "300")
STATUS = int(os.environ.get("MAINT_STATUS", "503"))
# 画面に出す任意の見出し。自作であることが一目で分かるようにしておく
TITLE = os.environ.get("MAINT_TITLE", "メンテナンスのお知らせ")


def target_group_name(event) -> str:
    """arn:...:targetgroup/<name>/<id> から <name> を取り出す"""
    arn = ((event.get("requestContext") or {}).get("elb") or {}).get("targetGroupArn", "")
    try:
        return arn.split(":targetgroup/")[1].split("/")[0]
    except (IndexError, AttributeError):
        return ""


def resolve_service(event) -> str:
    """tg-<service>-maintenance -> <service>"""
    parts = target_group_name(event).split("-")
    return parts[1] if len(parts) >= 2 else "unknown"


# ---------------------------------------------------------------------------
def html_body(service: str) -> str:
    """Web 向けのメンテナンス画面。
    テキストブラウザ表示でも崩れないよう、見出し / 段落 / 定義リストで組む。"""
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{TITLE} - {service}</title>
<style>
  body {{ font-family: "Segoe UI", "Yu Gothic UI", sans-serif; margin: 0;
         background: #0f172a; color: #e2e8f0; display: flex; min-height: 100vh;
         align-items: center; justify-content: center; }}
  .panel {{ max-width: 680px; padding: 40px; background: #1e293b;
            border: 1px solid #334155; border-radius: 12px; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 16px; }}
  dt {{ font-weight: bold; margin-top: 12px; color: #94a3b8; }}
  dd {{ margin: 4px 0 0 0; }}
  .note {{ margin-top: 24px; font-size: .85rem; color: #94a3b8; }}
</style>
</head>
<body>
  <div class="panel">
    <h1>{TITLE}</h1>
    <p>ただいまシステムメンテナンスを実施しております。
       ご利用の皆さまにはご不便をおかけし申し訳ございません。</p>
    <dl>
      <dt>対象サービス</dt><dd>{service}</dd>
      <dt>メンテナンス時間帯</dt><dd>{MAINT_WINDOW}</dd>
      <dt>お問い合わせ</dt><dd>{CONTACT}</dd>
    </dl>
    <p class="note">この画面は自作 Lambda (custom variant) が生成しています。</p>
  </div>
</body>
</html>"""


def json_body(service: str, path: str) -> str:
    """API 向けのエラー JSON。自組織のエラー規約に合わせて書き換えてください。"""
    return json.dumps(
        {
            "error": {
                "code": "SERVICE_UNDER_MAINTENANCE",
                "status": STATUS,
                "message": "The service is temporarily unavailable due to scheduled maintenance.",
                "message_ja": "システムメンテナンスのため、一時的にご利用いただけません。",
                "service": service,
                "path": path,
                "maintenance_window": MAINT_WINDOW,
                "retry_after_seconds": int(RETRY_AFTER),
                "responded_by": "custom-lambda",
            }
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
def handler(event, context):
    service = resolve_service(event)
    kind = "web" if service in WEB_SERVICES else "api"
    method = (event.get("httpMethod") or "GET").upper()
    path = event.get("path", "/")

    headers = {
        "Retry-After": str(RETRY_AFTER),
        "Cache-Control": "no-store",
        "X-Maintenance": "true",
        "X-Maintenance-Service": service,
        "X-Maintenance-Backend-Kind": kind,
        "X-Maintenance-Target-Group": target_group_name(event),
        # 差し替わったことを検証で確認するための目印
        "X-Maintenance-Impl": "custom",
    }

    if kind == "web":
        headers["Content-Type"] = "text/html; charset=utf-8"
        body = "" if method == "HEAD" else html_body(service)
    else:
        headers["Content-Type"] = "application/json; charset=utf-8"
        body = "" if method == "HEAD" else json_body(service, path)

    logger.info("custom lambda: service=%s kind=%s status=%s %s %s",
                service, kind, STATUS, method, path)

    return {
        "statusCode": STATUS,
        "statusDescription": f"{STATUS} Service Unavailable",
        "isBase64Encoded": False,
        "headers": headers,
        "body": body,
    }
