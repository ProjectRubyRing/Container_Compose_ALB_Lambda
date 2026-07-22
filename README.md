# ALB × Lambda メンテナンス応答 検証環境（Docker Compose）

ECS サービスごとに構築された **4 台の ALB**（`intraweb` / `interapi` / `intraapi` / `sfapi`）で、
**メンテナンス中に ALB のリスナールールが Lambda ターゲットグループへフォワードし、**

- バックエンドが **Web（intraweb）** → Lambda が **メンテナンス画面（HTML）** を返す
- バックエンドが **API（interapi / intraapi / sfapi）** → Lambda が **HTTP ステータスコード（503）+ JSON** を返す

という実装を、AWS に一切デプロイせずローカルで検証できる環境です。

Lambda は **AWS 公式 Lambda ベースイメージ（RIE 同梱）** で動作し、ALB からのイベントは
**実 ALB と同じ Lambda ターゲットグループ統合のイベント形式／レスポンス形式**でやり取りします。
つまり、ここで動いた `lambda/app.py` はそのまま AWS の Lambda にデプロイできます。

---

## 目次

1. [全体構成](#1-全体構成)
2. [ポート一覧（早見表）](#2-ポート一覧早見表)
3. [必要なもの](#3-必要なもの)
4. [クイックスタート（3 コマンド）](#4-クイックスタート3-コマンド)
5. [ディレクトリ構成](#5-ディレクトリ構成)
6. [メンテナンスモードの切り替え方](#6-メンテナンスモードの切り替え方)
7. [手動での動作確認手順（コピペ用）](#7-手動での動作確認手順コピペ用)
8. [自動検証スクリプト](#8-自動検証スクリプト)
9. [リスナールールの仕組みと書き方](#9-リスナールールの仕組みと書き方)
10. [Lambda がどうやって Web/API を判定しているか](#10-lambda-がどうやって-webapi-を判定しているか)
11. [Lambda を単体で叩く（ALB を経由しない）](#11-lambda-を単体で叩くalb-を経由しない)
12. [レスポンスヘッダで挙動をデバッグする](#12-レスポンスヘッダで挙動をデバッグする)
13. [ログの見方](#13-ログの見方)
14. [カスタマイズ方法](#14-カスタマイズ方法)
15. [本番 AWS への持っていき方](#15-本番-aws-への持っていき方)
16. [トラブルシューティング](#16-トラブルシューティング)
17. [環境の停止・削除](#17-環境の停止削除)
18. [この環境で再現できること／できないこと](#18-この環境で再現できることできないこと)

---

## 1. 全体構成

```
                   ┌─────────────── 通常時 ───────────────┐
                   │                                      │
 curl :8081 ─▶ alb-intraweb ─┬─ default action ──────────▶ ECS: intraweb  (HTML 200)
                             │
                             └─ [メンテ中] priority100 ──▶ Lambda ─▶ メンテナンス画面 HTML (503)
 curl :8082 ─▶ alb-interapi ─┬─ default action ──────────▶ ECS: interapi  (JSON 200)
                             └─ [メンテ中] priority100 ──▶ Lambda ─▶ JSON + 503
 curl :8083 ─▶ alb-intraapi ─┬─ default action ──────────▶ ECS: intraapi  (JSON 200)
                             └─ [メンテ中] priority100 ──▶ Lambda ─▶ JSON + 503
 curl :8084 ─▶ alb-sfapi    ─┬─ default action ──────────▶ ECS: sfapi     (JSON 200)
                             └─ [メンテ中] priority100 ──▶ Lambda ─▶ JSON + 503

                              ※ Lambda は 4 ALB 共有の 1 関数（maintenance-lambda）
                                 呼び出し元のターゲットグループ ARN で web/api を判定
```

コンテナ一覧（9 個）:

| コンテナ | 役割 |
|---|---|
| `alb-intraweb` / `alb-interapi` / `alb-intraapi` / `alb-sfapi` | ALB シミュレータ（リスナールール評価・フォワード・Lambda invoke） |
| `ecs-intraweb` / `ecs-interapi` / `ecs-intraapi` / `ecs-sfapi` | ECS サービスのモック（通常時の応答） |
| `maintenance-lambda` | メンテナンス応答 Lambda（AWS 公式 Lambda イメージ + RIE） |

---

## 2. ポート一覧（早見表）

| サービス | ALB（トラフィック） | 管理 API（メンテ切替） | バックエンド ECS |
|---|---|---|---|
| intraweb（Web） | http://localhost:8081 | http://localhost:9081 | `intraweb:8080`（内部のみ） |
| interapi（API） | http://localhost:8082 | http://localhost:9082 | `interapi:8080`（内部のみ） |
| intraapi（API） | http://localhost:8083 | http://localhost:9083 | `intraapi:8080`（内部のみ） |
| sfapi（API） | http://localhost:8084 | http://localhost:9084 | `sfapi:8080`（内部のみ） |

Lambda 直接 invoke 用: `http://localhost:9001/2015-03-31/functions/function/invocations`

---

## 3. 必要なもの

- Docker Desktop（Compose v2 同梱）… 動作確認済み: Docker 26.0.0 / Compose v2.26.1
- 初回ビルド時のみインターネット接続（`python:3.12-slim` と `public.ecr.aws/lambda/python:3.12` を取得）
- 検証コマンド用に、次のいずれか
  - Windows PowerShell（同梱の `.ps1` スクリプト）
  - bash + curl（Git Bash / WSL / macOS / Linux。同梱の `.sh` スクリプト）

> ポート 8081-8084 / 9001 / 9081-9084 が空いている必要があります。使用中の場合は
> `docker-compose.yml` の `ports:` を書き換えてください。

---

## 4. クイックスタート（3 コマンド）

```powershell
# 1) このディレクトリで起動（初回は数分かかります）
docker compose up -d --build

# 2) 起動確認（9 コンテナすべて Up になっていること）
docker compose ps

# 3) 全シナリオ自動検証（PASS=53 / FAIL=0 になれば成功）
powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1
```

bash 環境の場合:

```bash
docker compose up -d --build
docker compose ps
bash scripts/verify.sh          # PASS=35 / FAIL=0
```

---

## 5. ディレクトリ構成

```
Container_Compose_ALB_Lambda/
├─ docker-compose.yml            … 全コンテナ定義（ALB4台 + ECS4台 + Lambda1台）
├─ README.md                     … このファイル
├─ alb/                          … ALB シミュレータ
│  ├─ Dockerfile
│  ├─ requirements.txt
│  ├─ alb_sim.py                 … リスナールール評価 / ECS プロキシ / Lambda invoke / 管理 API
│  └─ config/                    … ALB ごとのリスナールール定義（ここを編集して検証する）
│     ├─ intraweb.yaml
│     ├─ interapi.yaml
│     ├─ intraapi.yaml
│     └─ sfapi.yaml
├─ lambda/                       … メンテナンス応答 Lambda（AWS へそのまま持ち込める）
│  ├─ Dockerfile                 … public.ecr.aws/lambda/python:3.12
│  └─ app.py                     … handler(event, context)
├─ backend/                      … ECS サービスのモック（4 サービス共通イメージ）
│  ├─ Dockerfile
│  └─ app.py
└─ scripts/
   ├─ mctl.ps1 / mctl.sh         … メンテナンスモード切替ツール
   └─ verify.ps1 / verify.sh     … 全シナリオ自動検証
```

---

## 6. メンテナンスモードの切り替え方

メンテナンス状態は **ALB ごとに独立** して持ちます（実運用でリスナールールを ALB 単位で
追加・削除するのと同じ感覚）。状態は Docker ボリューム `alb-state` に永続化されるため、
`docker compose restart` しても維持されます。

### 6-1. 付属ツールを使う（推奨）

PowerShell:

```powershell
.\scripts\mctl.ps1 status                # 4 ALB の状態を一覧
.\scripts\mctl.ps1 on  intraweb          # intraweb をメンテナンス中にする
.\scripts\mctl.ps1 off intraweb          # intraweb を通常に戻す
.\scripts\mctl.ps1 on  all               # 4 ALB すべてをメンテナンス中に
.\scripts\mctl.ps1 off all               # 4 ALB すべてを通常に
.\scripts\mctl.ps1 rules sfapi           # sfapi ALB の現在のリスナールールを表示
```

bash:

```bash
./scripts/mctl.sh status
./scripts/mctl.sh on  intraweb
./scripts/mctl.sh off all
./scripts/mctl.sh rules sfapi
```

`status` の出力例:

```
intraweb   MAINTENANCE  updated=2026-07-22T13:33:45+0000 admin on
interapi   NORMAL       updated=2026-07-22T13:34:02+0000 admin off
intraapi   NORMAL       updated=2026-07-22T13:34:02+0000 admin off
sfapi      NORMAL       updated=2026-07-22T13:34:02+0000 admin off
```

### 6-2. 管理 API を直接叩く

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/admin/state` | 現在のメンテナンス状態 |
| POST | `/admin/maintenance/on` | メンテナンス ON |
| POST | `/admin/maintenance/off` | メンテナンス OFF |
| POST | `/admin/maintenance` | `{"enabled":true,"note":"リリース作業"}` で理由付き設定 |
| GET | `/admin/rules` | 適用中のリスナールール一覧（`active_now` 付き） |
| POST | `/admin/reload` | YAML を再読込（コンテナ再起動なしでルール変更を反映） |

```powershell
# 例: intraweb（管理ポート 9081）
Invoke-RestMethod http://localhost:9081/admin/state
Invoke-RestMethod http://localhost:9081/admin/maintenance/on  -Method Post
Invoke-RestMethod http://localhost:9081/admin/maintenance -Method Post `
  -ContentType 'application/json' -Body '{"enabled":true,"note":"2026-08-01 定期リリース"}'
```

```bash
curl -s http://localhost:9081/admin/state
curl -s -X POST http://localhost:9081/admin/maintenance/on
curl -s -X POST http://localhost:9081/admin/maintenance \
     -H 'Content-Type: application/json' \
     -d '{"enabled":true,"note":"2026-08-01 定期リリース"}'
```

### 6-3. 起動時からメンテナンス中にしたい

`docker-compose.yml` の該当 ALB の `MAINTENANCE_DEFAULT: "true"` に変更して
`docker compose up -d` します（状態ファイルが未作成のときのみ有効）。

---

## 7. 手動での動作確認手順（コピペ用）

以下は **PowerShell** と **bash** の両方を併記しています。順番にそのまま実行すれば、
要件（Web はメンテ画面 HTML / API は HTTP ステータスコード）を目視で確認できます。

### STEP 1: 通常時（全 ALB → ECS）

```powershell
.\scripts\mctl.ps1 off all
curl.exe -i http://localhost:8081/dashboard   # intraweb: 200 + 「通常稼働中」HTML
curl.exe -i http://localhost:8082/v1/orders   # interapi: 200 + JSON
curl.exe -i http://localhost:8083/v1/orders   # intraapi: 200 + JSON
curl.exe -i http://localhost:8084/v1/orders   # sfapi   : 200 + JSON
```

```bash
./scripts/mctl.sh off all
curl -i http://localhost:8081/dashboard
curl -i http://localhost:8082/v1/orders
curl -i http://localhost:8083/v1/orders
curl -i http://localhost:8084/v1/orders
```

期待値: すべて `HTTP/1.1 200`、レスポンスヘッダに
`X-Alb-Matched-Rule: default` / `X-Alb-Target: tg-xxx-ecs(ecs)` / `X-Backend-Service: <サービス名>`。

### STEP 2: Web（intraweb）をメンテナンスにする → HTML のメンテ画面

```powershell
.\scripts\mctl.ps1 on intraweb
curl.exe -i http://localhost:8081/dashboard
Start-Process "http://localhost:8081/dashboard"   # ブラウザでメンテ画面を目視確認
```

```bash
./scripts/mctl.sh on intraweb
curl -i http://localhost:8081/dashboard
```

期待値:

```
HTTP/1.1 503 SERVICE UNAVAILABLE
Content-Type: text/html; charset=utf-8
Retry-After: 300
Cache-Control: no-store, no-cache, must-revalidate
X-Maintenance: true
X-Maintenance-Service: intraweb
X-Maintenance-Backend-Kind: web            ← Lambda が web と判定
X-Maintenance-Target-Group: tg-intraweb-maintenance
X-Alb-Matched-Rule: maintenance-catch-all  ← 優先度 100 のメンテ用リスナールール
X-Alb-Target: tg-intraweb-maintenance(lambda)

<!DOCTYPE html> … 「🛠️ ただいまメンテナンス中です」…
```

### STEP 3: API 3 本をメンテナンスにする → HTTP ステータスコード + JSON

```powershell
.\scripts\mctl.ps1 on interapi
.\scripts\mctl.ps1 on intraapi
.\scripts\mctl.ps1 on sfapi
curl.exe -i http://localhost:8082/v1/orders
curl.exe -i http://localhost:8083/v1/orders
curl.exe -i http://localhost:8084/v1/orders
```

```bash
for p in 8082 8083 8084; do curl -i "http://localhost:$p/v1/orders"; echo; done
```

期待値（例: interapi）:

```
HTTP/1.1 503 SERVICE UNAVAILABLE
Content-Type: application/json; charset=utf-8
Retry-After: 300
X-Maintenance-Backend-Kind: api            ← Lambda が api と判定（HTML は返さない）
X-Alb-Target: tg-interapi-maintenance(lambda)

{"error": {"code": "SERVICE_UNDER_MAINTENANCE", "status": 503,
 "message": "The service is temporarily unavailable due to scheduled maintenance.",
 "message_ja": "システムメンテナンスのため、一時的にご利用いただけません。",
 "service": "interapi", "path": "/v1/orders",
 "maintenance_window": "2026-08-01 02:00 - 05:00 (JST)",
 "retry_after_seconds": 300, "responded_by": "aws-lambda(alb-target-group)"}}
```

**ここが要件の肝**: 同じ 1 つの Lambda 関数なのに、intraweb（:8081）では HTML、
API（:8082-8084）では JSON + ステータスコードが返ります。

### STEP 4: 優先度の高いバイパスルールを確認

```powershell
# 4-1) ヘルスチェックはメンテ中でも ECS へ通る（priority 1）
curl.exe -i http://localhost:8081/healthz

# 4-2) 運用者セグメント 10.0.100.0/24 はメンテ中でも ECS へ（priority 10 / source-ip 条件）
curl.exe -i -H "X-Forwarded-For: 10.0.100.5"  http://localhost:8081/dashboard   # 200
curl.exe -i -H "X-Forwarded-For: 192.168.10.5" http://localhost:8081/dashboard  # 503

# 4-3) interapi は /v1/ping だけメンテ中でも ECS へ（priority 10 / path-pattern）
curl.exe -i http://localhost:8082/v1/ping        # 200
curl.exe -i http://localhost:8082/v1/orders      # 503

# 4-4) intraapi は運用ヘッダでバイパス（priority 10 / http-header）
curl.exe -i -H "X-Maintenance-Bypass: ops-secret-token" http://localhost:8083/v1/orders  # 200
curl.exe -i -H "X-Maintenance-Bypass: wrong"            http://localhost:8083/v1/orders  # 503

# 4-5) sfapi の /internal/* は ALB の fixed-response（Lambda を経由しない比較用）
curl.exe -i http://localhost:8084/internal/sync  # 503 / X-Alb-Target: fixed-response
```

> `X-Forwarded-For` は「ALB に接続してきたクライアント IP」を検証用に詐称するためのものです。
> 実 ALB の `source-ip` 条件も同じ発想で評価されます。

### STEP 5: ALB ごとに独立して切り替わることを確認

```powershell
.\scripts\mctl.ps1 off interapi
curl.exe -s -o NUL -w "interapi=%{http_code}`n" http://localhost:8082/v1/orders   # 200
curl.exe -s -o NUL -w "intraapi=%{http_code}`n" http://localhost:8083/v1/orders   # 503
```

### STEP 6: 後片付け

```powershell
.\scripts\mctl.ps1 off all
```

---

## 8. 自動検証スクリプト

`scripts/verify.ps1`（PowerShell）と `scripts/verify.sh`（bash）は、STEP 1〜6 の内容を
すべてアサーション付きで自動実行します。**実行後は必ず全 ALB を通常モードに戻します。**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1
```

検証している項目:

| # | シナリオ | 検証内容 |
|---|---|---|
| 1 | 通常時 | 4 ALB すべて 200 / ECS コンテナが応答 / 適用ルール = default |
| 2 | intraweb メンテ中 | 503・`Content-Type: text/html`・Lambda 経由・「メンテナンス中」文言・`Retry-After` |
| 2-1 | 〃 | `/healthz` は priority 1 で ECS へ通る |
| 2-2 | 〃 | 許可 IP はバイパスして 200、対象外 IP は 503 |
| 3 | API 3 本メンテ中 | 503・`Content-Type: application/json`・**HTML を返さない**・`code=SERVICE_UNDER_MAINTENANCE`・Lambda がサービス名を正しく識別 |
| 3-1 | interapi | `/v1/ping` がバイパスされる |
| 3-2 | intraapi | 正しい運用ヘッダで 200 / 誤ったヘッダで 503 |
| 3-3 | sfapi | `/internal/*` は ALB の fixed-response |
| 4 | 独立性 | interapi だけ復旧しても intraapi はメンテ継続 |
| 5 | 復旧 | 全 ALB を OFF にして 200 に戻る |

終了コードは全 PASS で `0`、1 件でも失敗すると `1`（CI に組み込めます）。

---

## 9. リスナールールの仕組みと書き方

ルールは `alb/config/<service>.yaml` に定義します。**優先度（priority）の小さい順**に評価し、
最初にマッチしたルールのアクションを実行、どれにもマッチしなければ `default_action` です
（実 ALB と同じ挙動）。

```yaml
alb:
  name: alb-intraweb          # ログ／レスポンスヘッダに出る ALB 名
  service: intraweb           # メンテナンス状態のキー
  kind: web

listener:
  port: 80
  default_action:             # 通常時の既定動作
    type: forward
    target_group: tg-intraweb-ecs

target_groups:
  tg-intraweb-ecs:
    type: ecs                 # ECS サービスへ HTTP プロキシ
    endpoint: http://intraweb:8080
  tg-intraweb-maintenance:
    type: lambda              # Lambda ターゲットグループ
    target_group_arn: arn:aws:elasticloadbalancing:...:targetgroup/tg-intraweb-maintenance/1111...
    endpoint: http://maintenance-lambda:8080/2015-03-31/functions/function/invocations

rules:
  - name: healthcheck-passthrough
    priority: 1
    conditions:
      - field: path-pattern
        values: ["/healthz", "/healthz/*"]
    action: { type: forward, target_group: tg-intraweb-ecs }

  - name: maintenance-catch-all
    priority: 100
    maintenance: true         # ★ メンテナンス ON のときだけ有効になるルール
    conditions:
      - field: path-pattern
        values: ["/*"]
    action: { type: forward, target_group: tg-intraweb-maintenance }
```

### `maintenance: true` の意味

**「メンテナンス作業時に ALB へ追加するリスナールール」** を表します。
`maintenance: true` のルールは、そのサービスのメンテナンスモードが ON のときだけ評価対象に入り、
OFF のときは存在しないものとして扱われます（= 実運用でルールを追加/削除するのと等価）。

### サポートしている条件（conditions）

| field | 書き方 | 説明 |
|---|---|---|
| `path-pattern` | `values: ["/api/*", "/v1/orders"]` | パス。`*` `?` ワイルドカード可 |
| `host-header` | `values: ["*.example.com"]` | Host ヘッダ（ポートは無視、大小区別なし） |
| `http-request-method` | `values: ["POST", "PUT"]` | HTTP メソッド |
| `source-ip` | `values: ["10.0.100.0/24"]` | クライアント IP（CIDR）。検証時は `X-Forwarded-For` で指定可 |
| `http-header` | `http_header_name: X-Foo` + `values: ["bar*"]` | 任意ヘッダ。値はワイルドカード可 |
| `query-string` | `values: [{key: debug, value: "1"}]` | クエリ文字列 |

複数条件を並べた場合は **AND**、1 条件内の `values` は **OR**（実 ALB と同じ）。

### サポートしているアクション（action）

| type | 記述 |
|---|---|
| `forward` | `{ type: forward, target_group: <名前> }`（ECS / Lambda 両対応） |
| `fixed-response` | `{ type: fixed-response, fixed_response_config: { status_code: 503, content_type: application/json, message_body: '...' } }` |
| `redirect` | `{ type: redirect, redirect_config: { protocol: http, host: example.com, path: /maintenance, status_code: HTTP_302 } }` |

### ルールを変更したら

```powershell
# YAML は read-only マウントなので、ホスト側で編集 → reload するだけ（再ビルド不要）
Invoke-RestMethod http://localhost:9081/admin/reload -Method Post
# または
docker compose restart alb-intraweb
```

現在のルールと有効/無効は次で確認できます:

```powershell
.\scripts\mctl.ps1 rules intraweb
```

---

## 10. Lambda がどうやって Web/API を判定しているか

実 ALB は Lambda ターゲットグループ経由の呼び出しで、イベントに
**`requestContext.elb.targetGroupArn`** を必ず含めます。本環境の Lambda はこれを使って
「どの ALB のどのリスナールールから呼ばれたか」を判定します。

```
tg-intraweb-maintenance  →  service = intraweb  →  WEB_SERVICES に含まれる  →  HTML を返す
tg-interapi-maintenance  →  service = interapi  →  API_SERVICES に含まれる  →  JSON + 503 を返す
tg-intraapi-maintenance  →  service = intraapi  →  API                      →  JSON + 503
tg-sfapi-maintenance     →  service = sfapi     →  API                      →  JSON + 503
```

- 命名規約は `tg-<service>-maintenance`。
- ARN から判定できない場合は **Host ヘッダ**（`HOST_MAP`）でフォールバック。
- それでも不明な場合は **api 扱い**（機械的クライアントに HTML を返さない安全側）。

Lambda の環境変数（`docker-compose.yml` の `maintenance-lambda`）:

| 変数 | 既定値 | 説明 |
|---|---|---|
| `WEB_SERVICES` | `intraweb` | HTML を返すサービス（カンマ区切り） |
| `API_SERVICES` | `interapi,intraapi,sfapi` | JSON + ステータスコードを返すサービス |
| `MAINT_WEB_STATUS` | `503` | Web のステータスコード |
| `MAINT_API_STATUS` | `503` | API の既定ステータスコード |
| `API_STATUS_OVERRIDES` | `{}` | サービス別上書き。例 `{"sfapi":429}` |
| `RETRY_AFTER` | `300` | `Retry-After` ヘッダ（秒） |
| `MAINT_WINDOW` | `2026-08-01 02:00 - 05:00 (JST)` | 画面/JSON に出すメンテ時間帯 |
| `CONTACT` | `システム運用窓口 (内線 1234)` | 画面の問い合わせ先 |
| `HOST_MAP` | 4 サービス分の JSON | Host ヘッダ → サービス名のフォールバック表 |

変更後は `docker compose up -d maintenance-lambda` で反映されます。

---

## 11. Lambda を単体で叩く（ALB を経由しない）

Lambda 単体のユニットテストとして、RIE のエンドポイントへ ELB イベントを直接 POST できます。

```bash
curl -s -XPOST "http://localhost:9001/2015-03-31/functions/function/invocations" -d '{
  "requestContext": {"elb": {"targetGroupArn":
    "arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:targetgroup/tg-sfapi-maintenance/4444444444444444"}},
  "httpMethod": "GET",
  "path": "/v1/accounts",
  "queryStringParameters": {},
  "headers": {"host": "sfapi.example.com"},
  "body": "",
  "isBase64Encoded": false
}'
```

戻り値（= 実 ALB が解釈するレスポンス形式）:

```json
{"statusCode": 503, "statusDescription": "503 Service Unavailable", "isBase64Encoded": false,
 "headers": {"Retry-After": "300", "Content-Type": "application/json; charset=utf-8",
             "X-Maintenance-Backend-Kind": "api", "X-Maintenance-Target-Group": "tg-sfapi-maintenance", ...},
 "body": "{\"error\": {\"code\": \"SERVICE_UNDER_MAINTENANCE\", ...}}"}
```

ARN の `tg-sfapi-maintenance` を `tg-intraweb-maintenance` に変えると HTML が返ることを確認できます。

PowerShell の場合:

```powershell
$evt = @{
  requestContext = @{ elb = @{ targetGroupArn = 'arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:targetgroup/tg-intraweb-maintenance/1111111111111111' } }
  httpMethod = 'GET'; path = '/dashboard'; headers = @{ host = 'intraweb.example.internal' }
  body = ''; isBase64Encoded = $false
} | ConvertTo-Json -Depth 6
Invoke-RestMethod 'http://localhost:9001/2015-03-31/functions/function/invocations' -Method Post -Body $evt
```

---

## 12. レスポンスヘッダで挙動をデバッグする

ALB シミュレータは、検証しやすいよう毎回次のヘッダを付与します。

| ヘッダ | 意味 |
|---|---|
| `X-Alb-Name` | 応答した ALB 名（例 `alb-intraweb`） |
| `X-Alb-Matched-Rule` | マッチしたリスナールール名（`default` はデフォルトアクション） |
| `X-Alb-Rule-Priority` | そのルールの優先度 |
| `X-Alb-Target` | 転送先（`tg-xxx-ecs(ecs)` / `tg-xxx-maintenance(lambda)` / `fixed-response`） |
| `X-Alb-Maintenance` | その ALB のメンテナンス状態（`true` / `false`） |
| `X-Amzn-Trace-Id` | ALB が付与するトレース ID（ECS/Lambda にも渡される） |

Lambda 側が付与するヘッダ:

| ヘッダ | 意味 |
|---|---|
| `X-Maintenance: true` | メンテナンス応答であること |
| `X-Maintenance-Service` | Lambda が識別したサービス名 |
| `X-Maintenance-Backend-Kind` | `web` / `api`（どちらの応答分岐を通ったか） |
| `X-Maintenance-Target-Group` | 呼び出し元ターゲットグループ名 |

ヘッダだけ見たいとき:

```powershell
curl.exe -s -o NUL -D - http://localhost:8081/dashboard
```

---

## 13. ログの見方

```powershell
docker compose logs -f                      # 全部
docker compose logs -f alb-intraweb         # 特定 ALB のアクセスログ
docker compose logs -f maintenance-lambda   # Lambda の実行ログ（受信イベント全文つき）
docker compose logs -f intraweb             # ECS 側のログ
```

ALB のアクセスログ例（どのルールでどこへ流れたかが 1 行で分かります）:

```
ALB=alb-intraweb maint=True client=172.24.0.1 "GET /dashboard"
  rule=maintenance-catch-all(prio=100) target=tg-intraweb-maintenance(lambda) status=503 6.8ms
```

Lambda のログ例:

```
maintenance response: service=intraweb kind=web status=503 method=GET path=/dashboard
```

---

## 14. カスタマイズ方法

| やりたいこと | 変更箇所 |
|---|---|
| メンテ画面のデザイン・文言を変える | `lambda/app.py` の `html_body()` → `docker compose up -d --build maintenance-lambda` |
| API のエラー JSON 構造を変える | `lambda/app.py` の `json_body()` → 同上 |
| API のステータスコードを 503 以外にする | `docker-compose.yml` の `MAINT_API_STATUS` または `API_STATUS_OVERRIDES`（例 `{"sfapi":429}`）→ `docker compose up -d maintenance-lambda` |
| メンテ時間帯・問い合わせ先の文言 | `MAINT_WINDOW` / `CONTACT` 環境変数 |
| バイパス条件（IP・ヘッダ・パス）を変える | `alb/config/<service>.yaml` の `rules` → `/admin/reload` |
| ECS サービスを増やす | `docker-compose.yml` に backend + alb を追加、`alb/config/` に YAML 追加、Lambda の `API_SERVICES` に追記 |
| ECS モックの応答内容を変える | `backend/app.py` → `docker compose up -d --build` |
| Web も API と同じ扱いにしたい（検証用） | `WEB_SERVICES` を空、`API_SERVICES` に全サービスを列挙 |

**ステータスコードを変えて検証する例**（sfapi だけ 429 にする）:

```yaml
# docker-compose.yml の maintenance-lambda
    environment:
      API_STATUS_OVERRIDES: '{"sfapi":429}'
```

```powershell
docker compose up -d maintenance-lambda
.\scripts\mctl.ps1 on sfapi
curl.exe -i http://localhost:8084/v1/orders     # HTTP/1.1 429 になる
```

---

## 15. 本番 AWS への持っていき方

この環境の各要素は、AWS の実リソースと次のように対応します。

| ローカル | AWS |
|---|---|
| `lambda/app.py` の `handler` | Lambda 関数（そのまま使えます。ランタイム Python 3.12） |
| `alb/config/*.yaml` の `rules` | ALB リスナールール（優先度・条件・アクションが 1:1 対応） |
| `target_groups[*].type: lambda` | Lambda ターゲットグループ（`TargetType=lambda`） |
| メンテナンス ON | メンテ用リスナールール（priority 100 等）を **追加**、または優先度を有効化 |
| メンテナンス OFF | そのリスナールールを **削除** |
| `/admin/maintenance/on` | `aws elbv2 create-rule` / `modify-rule` |

AWS 側で必要な追加設定（ローカルでは省略しているもの）:

1. Lambda に ALB からの invoke 権限を付与
   ```bash
   aws lambda add-permission --function-name maintenance-responder \
     --statement-id alb-invoke --action lambda:InvokeFunction \
     --principal elasticloadbalancing.amazonaws.com \
     --source-arn arn:aws:elasticloadbalancing:...:targetgroup/tg-intraweb-maintenance/xxxx
   ```
2. ターゲットグループの `lambda.multi_value_headers.enabled` 属性（複数値ヘッダを使う場合）
3. Lambda のタイムアウトは ALB のターゲット応答時間（既定 30 秒未満）に収める

ターゲットグループ名は `tg-<service>-maintenance` の命名規約を守ってください
（Lambda がこの名前から web/api を判定するため）。規約を変える場合は
`lambda/app.py` の `resolve_service()` を合わせて修正します。

---

## 16. トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| `docker compose up` でポート競合エラー | 8081-8084 / 9001 / 9081-9084 が使用中。`docker-compose.yml` の `ports` を変更 |
| `.ps1` が「Unexpected token」で落ちる | 実行ポリシー or 文字コード。`powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1` で実行してください（同梱スクリプトは UTF-8 BOM 付きで保存済み） |
| すべて 502 が返る | Lambda コンテナが起動途中。`docker compose logs maintenance-lambda` を確認し数秒待つ |
| メンテ ON にしたのに 200 が返る | 優先度の高いバイパスルールにマッチしている。`X-Alb-Matched-Rule` ヘッダで確認 |
| `X-Forwarded-For` を付けても source-ip バイパスが効かない | ヘッダ名のスペル、CIDR（既定 `10.0.100.0/24`）を確認。`.\scripts\mctl.ps1 rules intraweb` で条件を確認 |
| YAML を編集しても反映されない | `POST /admin/reload` するか `docker compose restart alb-xxx` |
| メンテ状態が意図せず残っている | 状態はボリューム `alb-state` に永続化。`.\scripts\mctl.ps1 off all` か `docker compose down -v` |
| Lambda のレスポンス形式を壊した | ALB は不正なレスポンスに対し 502 を返します（実 ALB と同じ）。`docker compose logs maintenance-lambda` を確認 |

---

## 17. 環境の停止・削除

```powershell
docker compose stop                 # 停止（状態は保持）
docker compose start                # 再開
docker compose down                 # コンテナ削除（メンテ状態ボリュームは残る）
docker compose down -v              # ボリューム含め完全削除
docker compose down -v --rmi local  # ビルドしたイメージも削除
```

---

## 18. この環境で再現できること／できないこと

**再現できること**

- リスナールールの優先度評価、条件（path/host/method/source-ip/header/query）の AND/OR 挙動
- ECS ターゲットグループ ↔ Lambda ターゲットグループの切り替え
- ALB → Lambda の **イベント形式**（`requestContext.elb.targetGroupArn`、`headers`、`body`、`isBase64Encoded`）
- Lambda → ALB の **レスポンス形式**（`statusCode` / `headers` / `multiValueHeaders` / `body` / `isBase64Encoded`）
- Lambda のレスポンス形式が不正な場合に ALB が 502 を返す挙動
- ALB による `X-Forwarded-For` / `X-Forwarded-Proto` / `X-Forwarded-Port` / `X-Amzn-Trace-Id` の付与
- fixed-response / redirect アクション
- ALB ごとに独立したメンテナンス切り替え

**再現していないこと（必要なら別途 AWS で確認）**

- HTTPS/ACM、SNI、TLS ポリシー
- ヘルスチェックによるターゲットの自動 unhealthy 判定、Connection draining
- WAF、認証アクション（OIDC/Cognito）、スティッキーセッション
- Lambda の同時実行数制限・コールドスタート・課金
- ALB アクセスログの S3 出力形式、CloudWatch メトリクス
