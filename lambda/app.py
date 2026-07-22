"""
メンテナンス応答 Lambda 関数 (ALB Lambda ターゲットグループ統合)

呼び出し元 ALB のリスナールール = どの Lambda ターゲットグループ経由で呼ばれたか
(event.requestContext.elb.targetGroupArn) によって応答を切り替える。

  - バックエンドが web  (intraweb)                -> メンテナンス画面(HTML) を返す
  - バックエンドが api  (interapi/intraapi/sfapi) -> HTTP ステータスコード + JSON を返す

環境変数:
  WEB_SERVICES        web 扱いにするサービス名 (カンマ区切り)  例: "intraweb"
  API_SERVICES        api 扱いにするサービス名 (カンマ区切り)  例: "interapi,intraapi,sfapi"
  MAINT_WEB_STATUS    web のステータスコード                  既定: 503
  MAINT_API_STATUS    api の既定ステータスコード              既定: 503
  API_STATUS_OVERRIDES サービス別ステータス JSON              例: {"sfapi":503}
  RETRY_AFTER         Retry-After ヘッダ(秒)                  既定: 300
  MAINT_WINDOW        画面/JSON に表示するメンテ時間帯の文言
"""

import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _csv(name, default):
    return [s.strip() for s in os.environ.get(name, default).split(",") if s.strip()]


WEB_SERVICES = _csv("WEB_SERVICES", "intraweb")
API_SERVICES = _csv("API_SERVICES", "interapi,intraapi,sfapi")
MAINT_WEB_STATUS = int(os.environ.get("MAINT_WEB_STATUS", "503"))
MAINT_API_STATUS = int(os.environ.get("MAINT_API_STATUS", "503"))
API_STATUS_OVERRIDES = json.loads(os.environ.get("API_STATUS_OVERRIDES", "{}"))
RETRY_AFTER = os.environ.get("RETRY_AFTER", "300")
MAINT_WINDOW = os.environ.get("MAINT_WINDOW", "本日 02:00 - 05:00 (JST)")
CONTACT = os.environ.get("CONTACT", "システム運用窓口 (内線 1234)")

# Host ヘッダからサービスを判定するためのフォールバック用マップ
HOST_MAP = json.loads(
    os.environ.get(
        "HOST_MAP",
        json.dumps(
            {
                "intraweb.example.internal": "intraweb",
                "interapi.example.com": "interapi",
                "intraapi.example.internal": "intraapi",
                "sfapi.example.com": "sfapi",
            }
        ),
    )
)


# ---------------------------------------------------------------------------
def parse_target_group_name(arn: str) -> str:
    """arn:...:targetgroup/<name>/<id> から <name> を取り出す"""
    if not arn:
        return ""
    try:
        return arn.split(":targetgroup/")[1].split("/")[0]
    except IndexError:
        return ""


def resolve_service(event) -> str:
    """呼び出し元のリスナールール(= ターゲットグループ)からサービス名を決定する。
    命名規約: tg-<service>-maintenance
    """
    arn = (event.get("requestContext", {}).get("elb", {}) or {}).get("targetGroupArn", "")
    tg_name = parse_target_group_name(arn)
    if tg_name.startswith("tg-"):
        parts = tg_name.split("-")
        if len(parts) >= 2:
            candidate = parts[1]
            if candidate in WEB_SERVICES or candidate in API_SERVICES:
                return candidate

    # フォールバック: Host ヘッダ
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    host = (headers.get("host") or "").split(":")[0].lower()
    if host in HOST_MAP:
        return HOST_MAP[host]

    return "unknown"


def service_kind(service: str) -> str:
    if service in WEB_SERVICES:
        return "web"
    if service in API_SERVICES:
        return "api"
    # 不明なものは安全側に倒して api 扱い (HTML を機械に返さない)
    return "api"


# ---------------------------------------------------------------------------
def html_body(service: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ただいまメンテナンス中です</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family: "Segoe UI", "Hiragino Kaku Gothic ProN", "Noto Sans JP", sans-serif;
         background:#f5f7fa; color:#1f2933; }}
  @media (prefers-color-scheme: dark) {{ body {{ background:#12161c; color:#e6eaf0; }} }}
  .card {{ max-width:640px; padding:48px 40px; background:rgba(255,255,255,.85);
           border-radius:16px; box-shadow:0 10px 40px rgba(0,0,0,.08); text-align:center; }}
  @media (prefers-color-scheme: dark) {{ .card {{ background:rgba(30,36,45,.9); box-shadow:none; }} }}
  h1 {{ font-size:1.6rem; margin:0 0 8px; }}
  .icon {{ font-size:3rem; }}
  p {{ line-height:1.9; margin:.4em 0; }}
  .meta {{ margin-top:24px; font-size:.85rem; opacity:.7; }}
  code {{ background:rgba(127,127,127,.15); padding:2px 6px; border-radius:4px; }}
</style>
</head>
<body>
  <div class="card">
    <div class="icon">🛠️</div>
    <h1>ただいまメンテナンス中です</h1>
    <p>システムメンテナンスのため、一時的にサービスを停止しております。</p>
    <p>メンテナンス時間帯: <strong>{MAINT_WINDOW}</strong></p>
    <p>ご不便をおかけいたしますが、しばらく経ってから再度アクセスしてください。</p>
    <p class="meta">
      お問い合わせ: {CONTACT}<br>
      service: <code>{service}</code> / responded by AWS Lambda (ALB target group)
    </p>
  </div>
</body>
</html>"""


def json_body(service: str, status: int, path: str) -> str:
    return json.dumps(
        {
            "error": {
                "code": "SERVICE_UNDER_MAINTENANCE",
                "status": status,
                "message": "The service is temporarily unavailable due to scheduled maintenance.",
                "message_ja": "システムメンテナンスのため、一時的にご利用いただけません。",
                "service": service,
                "path": path,
                "maintenance_window": MAINT_WINDOW,
                "retry_after_seconds": int(RETRY_AFTER),
                "responded_by": "aws-lambda(alb-target-group)",
            }
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
def handler(event, context):
    logger.info("event=%s", json.dumps(event, ensure_ascii=False)[:2000])

    service = resolve_service(event)
    kind = service_kind(service)
    method = (event.get("httpMethod") or "GET").upper()
    path = event.get("path", "/")
    tg_arn = (event.get("requestContext", {}).get("elb", {}) or {}).get("targetGroupArn", "")

    common_headers = {
        "Retry-After": str(RETRY_AFTER),
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "X-Maintenance": "true",
        "X-Maintenance-Service": service,
        "X-Maintenance-Backend-Kind": kind,
        "X-Maintenance-Target-Group": parse_target_group_name(tg_arn),
    }

    if kind == "web":
        status = MAINT_WEB_STATUS
        body = "" if method == "HEAD" else html_body(service)
        headers = {**common_headers, "Content-Type": "text/html; charset=utf-8"}
    else:
        status = int(API_STATUS_OVERRIDES.get(service, MAINT_API_STATUS))
        body = "" if method == "HEAD" else json_body(service, status, path)
        headers = {**common_headers, "Content-Type": "application/json; charset=utf-8"}

    logger.info(
        "maintenance response: service=%s kind=%s status=%s method=%s path=%s",
        service, kind, status, method, path,
    )

    return {
        "statusCode": status,
        "statusDescription": f"{status} Service Unavailable",
        "isBase64Encoded": False,
        "headers": headers,
        "body": body,
    }
