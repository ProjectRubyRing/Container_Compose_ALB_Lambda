# ALB × Lambda メンテナンス応答 検証環境（Docker Compose）

ECS サービスごとに構築された **4 台の ALB**（`intraweb` / `interapi` / `intraapi` / `sfapi`）で、
**メンテナンス中に ALB のリスナールールが Lambda ターゲットグループへフォワードし、**

- バックエンドが **Web（intraweb）** → Lambda が **メンテナンス画面（HTML）** を返す
- バックエンドが **API（interapi / intraapi / sfapi）** → Lambda が **HTTP ステータスコード（503）+ JSON** を返す

という実装を、AWS に一切デプロイせずローカルで検証できる環境です。

Lambda は **AWS 公式 Lambda ベースイメージ（RIE 同梱）** で動作し、ALB からのイベントは
**実 ALB と同じ Lambda ターゲットグループ統合のイベント形式／レスポンス形式**でやり取りします。
つまり、ここで動いた `lambda/app.py` はそのまま AWS の Lambda にデプロイできます。

さらに次のことができます。

- **Lambda を自作のものに差し替えて検証する** — `lambda-custom/app.py` に自分の実装を書き、
  ターゲットグループ ARN はそのままに invoke 先だけを切り替えます。実 ALB からの呼び出しと
  同一のイベントで自作関数を試せ、ALB 統合の契約を満たしているか自動チェックもできます。
  → [11 章](#11-lambda-を自作のものに差し替えて検証する)
- **メンテナンス画面をテキストブラウザ風にターミナル表示する** — ブラウザを開かずに
  「利用者に何が見えるか」を確認できます（全角文字でも枠が崩れません）。
  → [9 章](#9-呼び出し確認ツール-albcheck画面表示とレスポンス詳細)
- **レスポンスの詳細と検証結果を Excel に出力する** — ステータス・全ヘッダ・本文・判定に加え、
  **描画した画面そのもの**もシートに残るので、そのままエビデンスにできます。
  → [10 章](#10-excel-レポートを出力する)

検証ツールは **Python 標準ライブラリのみ**で動くため `pip install` は不要です
（ホストに Python が無い場合は同梱の `inspector` コンテナで実行できます）。

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
9. [呼び出し確認ツール albcheck（画面表示とレスポンス詳細）](#9-呼び出し確認ツール-albcheck画面表示とレスポンス詳細)
10. [Excel レポートを出力する](#10-excel-レポートを出力する)
11. [Lambda を自作のものに差し替えて検証する](#11-lambda-を自作のものに差し替えて検証する)
12. [リスナールールの仕組みと書き方](#12-リスナールールの仕組みと書き方)
13. [Lambda がどうやって Web/API を判定しているか](#13-lambda-がどうやって-webapi-を判定しているか)
14. [Lambda を単体で叩く（ALB を経由しない）](#14-lambda-を単体で叩くalb-を経由しない)
15. [レスポンスヘッダで挙動をデバッグする](#15-レスポンスヘッダで挙動をデバッグする)
16. [ログの見方](#16-ログの見方)
17. [カスタマイズ方法](#17-カスタマイズ方法)
18. [本番 AWS への持っていき方](#18-本番-aws-への持っていき方)
19. [トラブルシューティング](#19-トラブルシューティング)
20. [環境の停止・削除](#20-環境の停止削除)
21. [この環境で再現できること／できないこと](#21-この環境で再現できることできないこと)

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
                              ※ invoke 先は builtin(同梱) / custom(自作) を無停止で切替可
```

コンテナ一覧（常時 10 個 + ツール 1 個）:

| コンテナ | 役割 |
|---|---|
| `alb-intraweb` / `alb-interapi` / `alb-intraapi` / `alb-sfapi` | ALB シミュレータ（リスナールール評価・フォワード・Lambda invoke） |
| `ecs-intraweb` / `ecs-interapi` / `ecs-intraapi` / `ecs-sfapi` | ECS サービスのモック（通常時の応答） |
| `maintenance-lambda` | メンテナンス応答 Lambda（AWS 公式 Lambda イメージ + RIE） |
| `custom-lambda` | **自作 Lambda**（差し替え検証用 / `lambda-custom/app.py`） |
| `inspector` | 検証ツール albcheck 実行用（`profiles: tools` なので `run` したときだけ起動） |

メンテナンス中にどちらの Lambda が呼ばれるかは ALB ごとに切り替えられます。
ターゲットグループ ARN は同じものが渡るため、自作 Lambda にも実 ALB と同一のイベントが届きます。

---

## 2. ポート一覧（早見表）

| サービス | ALB（トラフィック） | 管理 API（メンテ切替） | バックエンド ECS |
|---|---|---|---|
| intraweb（Web） | http://localhost:8081 | http://localhost:9081 | `intraweb:8080`（内部のみ） |
| interapi（API） | http://localhost:8082 | http://localhost:9082 | `interapi:8080`（内部のみ） |
| intraapi（API） | http://localhost:8083 | http://localhost:9083 | `intraapi:8080`（内部のみ） |
| sfapi（API） | http://localhost:8084 | http://localhost:9084 | `sfapi:8080`（内部のみ） |

Lambda 直接 invoke 用（RIE）:

| Lambda 実装 (variant) | コンテナ | invoke URL |
|---|---|---|
| `builtin`（既定 / 同梱実装） | `maintenance-lambda` | `http://localhost:9001/2015-03-31/functions/function/invocations` |
| `custom`（自作実装） | `custom-lambda` | `http://localhost:9002/2015-03-31/functions/function/invocations` |

どちらの Lambda を呼ぶかは ALB ごとに切り替えられます（→ [11 章](#11-lambda-を自作のものに差し替えて検証する)）。

---

## 3. 必要なもの

- Docker Desktop（Compose v2 同梱）… 動作確認済み: Docker 26.0.0 / Compose v2.26.1
  - Linux では **podman / nerdctl でも動きます**（`report.sh` が自動で選びます）。
    RHEL 9 なら `sudo dnf install -y podman podman-compose`
- 初回ビルド時のみインターネット接続（`python:3.12-slim` と `public.ecr.aws/lambda/python:3.12` を取得）
- 検証コマンド用に、次のいずれか
  - Windows PowerShell（同梱の `.ps1` スクリプト）
  - bash + curl（Git Bash / WSL / macOS / Linux。同梱の `.sh` スクリプト）
- **ブラウザは不要です。** メンテナンス画面はターミナルへテキスト描画します。
  EC2 へ Session Manager で入っただけの GUI なし環境でも全機能が使えます
  （→ [9-3 章](#9-3-ブラウザが使えない環境での確認ec2--session-manager-など)）

- （任意）Python 3.8 以上 … `scripts/report.ps1` / `report.sh` をホストで直接動かす場合。
  **入っていなくても構いません**（自動で `inspector` コンテナにフォールバックします）。
  追加パッケージのインストールは不要です。

> ポート 8081-8084 / 9001 / 9002 / 9081-9084 が空いている必要があります。使用中の場合は
> `docker-compose.yml` の `ports:` を書き換えてください。

---

## 4. クイックスタート（3 コマンド）

```powershell
# 1) このディレクトリで起動（初回は数分かかります）
docker compose up -d --build

# 2) 起動確認（10 コンテナすべて Up になっていること）
docker compose ps

# 3) 全シナリオ自動検証（PASS=65 / FAIL=0 になれば成功）
powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1

# 4) 画面表示つきの検証 + Excel レポート出力（reports\ に xlsx が出ます）
.\scripts\report.ps1 report
```

bash 環境の場合:

```bash
docker compose up -d --build
docker compose ps
bash scripts/verify.sh          # PASS=44 / FAIL=0 になれば成功
./scripts/report.sh report      # 画面表示つき検証 + Excel レポート
```

やりたいことから引く早見表:

| やりたいこと | コマンド | 参照 |
|---|---|---|
| メンテ画面が実際どう見えるか確認したい | `.\scripts\report.ps1 check intraweb /dashboard` | [9 章](#9-呼び出し確認ツール-albcheck画面表示とレスポンス詳細) |
| 検証結果を Excel で残したい | `.\scripts\report.ps1 report` | [10 章](#10-excel-レポートを出力する) |
| 自作の Lambda で検証したい | `lambda-custom\app.py` を編集 → `.\scripts\mctl.ps1 lambda custom` | [11 章](#11-lambda-を自作のものに差し替えて検証する) |
| メンテナンスを ON/OFF したい | `.\scripts\mctl.ps1 on intraweb` | [6 章](#6-メンテナンスモードの切り替え方) |
| 接続エラーになる／ブラウザが使えない | `./scripts/report.sh doctor` | [9-3 章](#9-3-ブラウザが使えない環境での確認ec2--session-manager-など) |

---

## 5. ディレクトリ構成

```
Container_Compose_ALB_Lambda/
├─ docker-compose.yml            … 全コンテナ定義（ALB4台 + ECS4台 + Lambda2台 + ツール1台）
├─ docker-compose.selinux.yml    … SELinux が Enforcing のホスト用の追加設定（→ 9-3 章）
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
│  └─ app.py                     … handler(event, context)        ★ variant=builtin
├─ lambda-custom/                … 自作 Lambda（差し替え検証用）  ★ variant=custom
│  ├─ Dockerfile                 … builtin と同じ公式ベースイメージ
│  ├─ requirements.txt           … 追加ライブラリが必要ならここに
│  └─ app.py                     … ここを自分の実装に書き換える
├─ backend/                      … ECS サービスのモック（4 サービス共通イメージ）
│  ├─ Dockerfile
│  └─ app.py
├─ tools/                        … 検証ツール（pip install 不要・標準ライブラリのみ）
│  ├─ Dockerfile                 … inspector コンテナ（w3m / lynx 同梱）
│  ├─ albcheck.py                … CLI 本体（check / report / contract / variant / render / doctor）
│  ├─ envprobe.py                … 接続先の自動検出と環境診断（GUI 不要・→ 9-3 章）
│  ├─ textrender.py              … HTML・JSON をテキストブラウザ風に描画（全角幅対応）
│  ├─ report_excel.py            … 検証結果と画面表示を Excel シートへ
│  └─ xlsxlite.py                … 依存なしの最小 xlsx ライタ
├─ reports/                      … 出力された Excel レポートの置き場所
└─ scripts/
   ├─ mctl.ps1 / mctl.sh         … メンテナンスモード切替 + Lambda 差し替え
   ├─ verify.ps1 / verify.sh     … 全シナリオ自動検証（アサーションのみ）
   └─ report.ps1 / report.sh     … albcheck のラッパ（画面表示 + Excel レポート）
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
.\scripts\mctl.ps1 lambda                # 各 ALB が呼ぶ Lambda 実装を表示
.\scripts\mctl.ps1 lambda custom         # 全 ALB を自作 Lambda へ差し替え
.\scripts\mctl.ps1 lambda builtin sfapi  # sfapi だけ同梱実装へ戻す
```

bash:

```bash
./scripts/mctl.sh status
./scripts/mctl.sh on  intraweb
./scripts/mctl.sh off all
./scripts/mctl.sh rules sfapi
./scripts/mctl.sh lambda custom
```

`status` の出力例（`lambda=` が現在の Lambda 実装）:

```
intraweb   MAINTENANCE  lambda=builtin  updated=2026-07-22T13:33:45+0000 admin on
interapi   NORMAL       lambda=builtin  updated=2026-07-22T13:34:02+0000 admin off
intraapi   NORMAL       lambda=custom   updated=2026-07-22T13:34:02+0000 admin off
sfapi      NORMAL       lambda=builtin  updated=2026-07-22T13:34:02+0000 admin off
```

### 6-2. 管理 API を直接叩く

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/admin/state` | 現在のメンテナンス状態 |
| POST | `/admin/maintenance/on` | メンテナンス ON |
| POST | `/admin/maintenance/off` | メンテナンス OFF |
| POST | `/admin/maintenance` | `{"enabled":true,"note":"リリース作業"}` で理由付き設定 |
| GET | `/admin/rules` | 適用中のリスナールール一覧（`active_now` 付き） |
| GET | `/admin/lambda` | 現在の Lambda 実装（variant）と選択肢・invoke 先 URL |
| POST | `/admin/lambda` | `{"variant":"custom"}` で invoke 先 Lambda を差し替え |
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

# invoke 先 Lambda の確認と差し替え
curl -s http://localhost:9081/admin/lambda
curl -s -X POST http://localhost:9081/admin/lambda \
     -H 'Content-Type: application/json' -d '{"variant":"custom"}'
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
| 5 | Lambda 差し替え | `builtin` → `custom` へ切り替えると自作 Lambda が応答し（`X-Alb-Lambda-Variant`）、web は HTML・api は JSON のまま。`builtin` に戻せることも確認 |
| 6 | 復旧 | 全 ALB を OFF にして 200 に戻る |

終了コードは全 PASS で `0`、1 件でも失敗すると `1`（CI に組み込めます）。
アサーション件数は PowerShell 版が 65 件、bash 版が 44 件です。

### verify と report の使い分け

| | `verify.ps1` / `verify.sh` | `report.ps1 report`（albcheck） |
|---|---|---|
| 目的 | 合否だけを素早く確認（CI 向け） | 画面表示・レスポンス詳細まで確認し、記録を残す |
| 出力 | PASS/FAIL の一覧 | 端末表示 + **Excel レポート**（7 シート） |
| 必要なもの | PowerShell / curl | Python 3.8 以上（無ければ `inspector` コンテナ） |
| Lambda 差し替え検証 | あり（builtin ⇄ custom の切り替え） | あり（`--variant both` で両実装を通しで比較） |

どちらも実行後に全 ALB を通常モードへ戻します。

---

## 9. 呼び出し確認ツール albcheck（画面表示とレスポンス詳細）

`tools/albcheck.py` は「メンテナンス応答が実際どう見えるか」「レスポンスの中身がどうなって
いるか」をターミナルで確認するためのツールです。**pip install は不要**（Python 標準ライブラリ
のみ）で、`curl` の出力を読み解く代わりに次の 3 つを一度に表示します。

1. **経路** … どの ALB のどのリスナールールにマッチし、どこへ流れたか
2. **レスポンス詳細** … ステータス・全ヘッダ（ALB / Lambda / ECS / 標準に分類）・応答時間・サイズ
3. **画面表示** … HTML をテキストブラウザ風に整形して枠付きで描画（JSON は整形して色付け）

```powershell
# ラッパ経由（Python が無ければ自動で docker compose run にフォールバック）
.\scripts\report.ps1 check intraweb /dashboard

# Python を直接呼んでもよい
python tools\albcheck.py check intraweb /dashboard
```

bash 環境の場合:

```bash
./scripts/report.sh check intraweb /dashboard
```

出力例（intraweb をメンテナンスにした状態）:

```
════════════════════════════════════════════════════════════════════════════
 GET http://localhost:8081/dashboard
════════════════════════════════════════════════════════════════════════════
── リクエスト ───────────────────────────────────────────────────────────────
  GET http://localhost:8081/dashboard
── レスポンス ───────────────────────────────────────────────────────────────
  503 Service Unavailable      12.4 ms / 1,724 bytes
  経路: alb-intraweb  maintenance=true  rule=maintenance-catch-all(prio 100)
        → tg-intraweb-maintenance(lambda)[builtin]
── ALB が付与したヘッダ ─────────────────────────────────────────────────────
  X-Alb-Name            alb-intraweb
  X-Alb-Matched-Rule    maintenance-catch-all
  X-Alb-Target          tg-intraweb-maintenance(lambda)
  X-Alb-Lambda-Variant  builtin
  X-Alb-Duration-Ms     6.8
── Lambda / メンテナンス応答のヘッダ ────────────────────────────────────────
  X-Maintenance               true
  X-Maintenance-Backend-Kind  web
── 画面表示 (テキストブラウザ描画)  種別=html ───────────────────────────────
┌─ ただいまメンテナンス中です ─────────────────────────────────────────────┐
│ GET http://localhost:8081/dashboard  →  503                              │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│ ただいまメンテナンス中です                                               │
│ ━━━━━━━━━━━━━━━━━━━━━━━━━━                                               │
│                                                                          │
│ システムメンテナンスのため、一時的にサービスを停止しております。         │
│ メンテナンス時間帯: 2026-08-01 02:00 - 05:00 (JST)                       │
│                                                                          │
│ お問い合わせ: システム運用窓口 (内線 1234)                               │
└──────────────────────────────────────────────────────────────────────────┘
```

日本語（全角）を含む行でも枠がずれないよう、East Asian Width を見て表示幅を計算しています。

### 9-1. サブコマンド

| コマンド | 用途 |
|---|---|
| `check <service> [path]` | 1 リクエストのレスポンス詳細 + 画面表示 |
| `report` | 全シナリオを検証して Excel レポートを出力（→ 10 章） |
| `contract` | 自作 Lambda が ALB 統合の契約を守っているか検証（→ 11 章） |
| `variant [builtin\|custom]` | invoke 先 Lambda の確認・切り替え（→ 11 章） |
| `render <url\|file\|service>` | 画面のテキスト描画だけを見る |
| `doctor` | **繋がらないときの原因切り分け**（→ [9-3 章](#9-3-ブラウザが使えない環境での確認ec2--session-manager-など)） |

### 9-2. よく使うオプション

| オプション | 説明 |
|---|---|
| `--width 100` | 表示幅（既定 88 桁）。狭い端末では小さく |
| `--renderer w3m` | 描画に実物のテキストブラウザを使う（`w3m` / `lynx` / `links`） |
| `--renderer auto` | インストール済みのテキストブラウザがあれば使い、無ければ内蔵描画 |
| `-H "Name: value"` | ヘッダを追加（バイパス検証用） |
| `--xff 10.0.100.5` | `X-Forwarded-For` を指定（`source-ip` 条件の検証用） |
| `--raw` | 本文の生データも表示 |
| `--no-color` | ANSI 色を使わない（ログに残すとき） |
| `--save screen.html` | `check` のレスポンス本文をファイルに保存（後で手元へ持ち帰って開ける） |
| `--save-text screen.txt` | `check` の画面描画結果をテキストで保存（そのまま報告書に貼れる） |
| `--no-autodiscover` | 接続先の自動検出をやめ、`localhost` だけを使う |

ラッパ（`report.sh` / `report.ps1`）には、**実行場所**を選ぶオプションもあります。
サブコマンドより**前**に置いてください。

| オプション | 説明 |
|---|---|
| （無指定） | ホストから届くか確認し、届かなければコンテナ内実行へ自動で切り替え |
| `--host` | 必ずホストの Python で実行する |
| `--in-container` | 必ず検証用コンテナの中から実行する（ホストのポートに届かない環境向け） |

```powershell
# 運用者セグメントからのアクセスを再現（バイパスされて ECS の画面が出る）
.\scripts\report.ps1 check intraweb /dashboard --xff 10.0.100.5

# 運用ヘッダでのバイパスを確認
.\scripts\report.ps1 check intraapi /v1/orders -H "X-Maintenance-Bypass: ops-secret-token"

# 実物の w3m で描画（w3m がある環境のみ。無ければ内蔵描画にフォールバック）
.\scripts\report.ps1 check intraweb /dashboard --renderer w3m
```

> **ホストに Python が無い場合**
> `scripts/report.ps1` / `report.sh` は自動で `docker compose run --rm inspector ...` に
> フォールバックします。この `inspector` コンテナには **w3m / lynx が入っている**ので、
> `--renderer w3m` での確認もそのまま行えます。
> 直接呼ぶ場合: `docker compose run --rm inspector check intraweb /dashboard`

### 9-3. ブラウザが使えない環境での確認（EC2 + Session Manager など）

**このツールはブラウザを一切使いません。** `check` / `render` の画面表示は、HTML を自前で
解析してターミナルへ描画しているだけなので、GUI も X サーバも DISPLAY も不要です。
EC2（RHEL / Amazon Linux）へ Session Manager で入った CLI だけの環境でも、
ブラウザで見るのと同じ内容を確認できます。

```bash
./scripts/mctl.sh on intraweb                    # メンテナンスモードにする
./scripts/report.sh check intraweb /dashboard    # メンテ画面をテキストで描画
./scripts/report.sh report                       # 全シナリオ検証 + Excel レポート
```

画面を手元へ持ち帰りたい場合は保存できます（`scp` / `sftp` / S3 経由で回収してください）。

```bash
./scripts/report.sh check intraweb /dashboard \
    --save reports/screen.html \
    --save-text reports/screen.txt
```

#### 接続エラー `<urlopen error [Errno 111] Connection refused>` が出るとき

これは**ブラウザの有無とは無関係**で、`127.0.0.1:8081` で待ち受けているプロセスが無い、
という意味です。原因を切り分けるコマンドを用意しています。

```bash
./scripts/report.sh doctor
```

`doctor` は次をまとめて表示し、終了コードで結果を返します（到達可 = 0 / 到達不可 = 3）。

| 表示するもの | 分かること |
|---|---|
| 実行環境 | OS・Python・コンテナ内かどうか・SELinux の状態・GUI ブラウザの有無 |
| コンテナランタイム | `docker` / `podman` / `nerdctl` の有無と、使えない場合はその理由 |
| コンテナの状態 | 10 コンテナの起動状況。**落ちているものはログ末尾も表示** |
| 接続確認 | ALB 4 本 + 管理 API 4 本 + Lambda 2 本へ実際にリクエストし、失敗理由を日本語で表示 |
| 診断 | 原因の判定と、そのまま貼れる対処コマンド |
| export 行 | 他のスクリプト（`mctl.sh` / `verify.sh`）に同じ接続先を使わせるための `export` |

よくある原因と対処:

| 原因 | 対処 |
|---|---|
| そもそもコンテナが起動していない | `docker compose up -d --build`（`doctor` の「コンテナの状態」で分かります） |
| RHEL に docker が無い（podman だけ） | `podman compose up -d --build`。`report.sh` は `podman` / `nerdctl` も自動で使います |
| **SELinux が Enforcing** で ALB がマウントを読めず落ちている | `docker compose -f docker-compose.yml -f docker-compose.selinux.yml up -d --build`（`:z` 付きでマウントし直します） |
| ポートが公開されていない / rootless で届かない | `./scripts/report.sh --in-container check intraweb /dashboard` |

#### 接続先の自動検出

`albcheck` は接続先を次の順に試し、**最初に繋がったところを自動で採用**します。
どこを使ったかは `[接続先を自動検出] ...` の行に出ます。

1. 環境変数 `ALB_URL_<SERVICE>` / `ADMIN_URL_<SERVICE>` / `LAMBDA_URL_<VARIANT>`（明示指定が最優先）
2. ホストへ公開されたポート … `http://127.0.0.1:8081`
3. コンテナ名 … `http://alb-intraweb`（コンテナの中から実行したとき）
4. コンテナ IP … `http://172.x.x.x:80`（Linux + rootful ランタイム）

そのため、ホストから届かない環境でも `--in-container` を付ければそのまま動きます。
`mctl.sh` / `verify.sh` は環境変数のみを見るので、`doctor` が出力する `export` 行を
実行してから使ってください。

```bash
eval "$(./scripts/report.sh doctor | grep '^  export ' | sed 's/^  //')"
./scripts/verify.sh
```

> **どうしてもブラウザで見たい場合**（GUI のある手元 PC から）
> Session Manager のポートフォワードで EC2 の 8081 を手元へ転送できます。
> ```bash
> aws ssm start-session --target <instance-id> \
>     --document-name AWS-StartPortForwardingSession \
>     --parameters '{"portNumber":["8081"],"localPortNumber":["8081"]}'
> ```
> 転送後、手元のブラウザで `http://localhost:8081/dashboard` を開きます。
> ただし上記のテキスト描画で同じ内容が確認できるため、通常は不要です。

---

## 10. Excel レポートを出力する

`report` サブコマンドは、**全シナリオを自動実行 → 結果と画面表示をまとめて Excel に出力**します。
検証エビデンスとしてそのまま提出できる形式です。**openpyxl などのインストールは不要**で、
xlsx を標準ライブラリだけで生成します（`tools/xlsxlite.py`）。

```powershell
# 全シナリオ検証 + Excel 出力（既定の出力先は reports\alb-lambda-report_<日時>.xlsx）
.\scripts\report.ps1 report

# 出力先を指定
.\scripts\report.ps1 report --excel reports\2026-08-02_検証.xlsx

# 同梱 Lambda と自作 Lambda を続けて検証し、1 つのブックにまとめる
.\scripts\report.ps1 report --variant both

# Lambda の契約チェック（→ 11 章）も一緒に実施
.\scripts\report.ps1 report --contract
```

実行すると、メンテナンスの ON/OFF を自動で切り替えながら 21 ケース（約 87 チェック）を
検証し、**最後に必ず全 ALB を通常モードへ戻します**。全 PASS なら終了コード `0`、
1 件でも失敗すれば `1` を返すので CI に組み込めます。

### 10-1. 出力されるシート

| シート | 内容 |
|---|---|
| **サマリ** | 実行条件（日時・Lambda 実装・描画幅）と PASS/FAIL/WARN 集計、グループ別集計、凡例 |
| **検証結果** | 1 ケース = 1 行。ステータス・適用ルール・優先度・転送先・Lambda 実装・応答時間・サイズ |
| **チェック明細** | 「何を期待して実際どうだったか」を 1 項目 1 行で記録（期待値／実際値の列つき） |
| **レスポンスヘッダ** | 全レスポンスヘッダ。`ALB` / `Lambda` / `ECS` / `標準` に分類済み |
| **レスポンス本文** | 受信した本文の生データ（HTML / JSON そのまま） |
| **画面表示** | **テキストブラウザ描画をそのまま貼り付け**。等幅フォント指定なので枠線が崩れません |
| **Lambda契約チェック** | `--contract` 実行時のみ。ALB Lambda 統合の契約適合結果 |

すべてのシートに**ウィンドウ枠固定とオートフィルタ**が設定済みなので、
「FAIL だけ絞り込む」「intraweb の行だけ見る」といった確認がすぐできます。
判定セルは PASS=緑 / FAIL=赤 / WARN=黄で色分けされます。

### 10-2. 1 リクエストだけ Excel に出す

`check` にも `--excel` があります。「この 1 件の画面とレスポンスだけ記録したい」ときに使います。

```powershell
.\scripts\report.ps1 check intraweb /dashboard --excel reports\intraweb画面.xlsx
```

> **出力先について**
> 既定の出力先は `reports/` です（`REPORT_DIR` 環境変数で変更可）。
> `docker compose run --rm inspector report` で実行した場合も、`./reports` に
> バインドマウントされているのでホスト側に xlsx が残ります。

---

## 11. Lambda を自作のものに差し替えて検証する

同梱の `lambda/app.py` の代わりに、**自分で書いた Lambda 関数を同じ環境で検証**できます。
ALB の Lambda ターゲットグループには invoke 先を 2 つ登録してあり、**ターゲットグループ ARN
はそのまま**に invoke 先だけを切り替えます。つまり自作 Lambda にも、実 ALB からの呼び出しと
**まったく同じイベント**が渡ります。

| variant | コンテナ | ソース | 直接 invoke |
|---|---|---|---|
| `builtin`（既定） | `maintenance-lambda` | `lambda/app.py` | http://localhost:9001 |
| `custom` | `custom-lambda` | **`lambda-custom/app.py`** | http://localhost:9002 |

### 11-1. 手順

```powershell
# 1) lambda-custom\app.py を自分の実装に書き換える（ひな形と契約の説明が入っています）

# 2) 反映
docker compose up -d --build custom-lambda

# 3) 契約を満たしているか自動チェック（ALB を経由せず直接 invoke して検証）
.\scripts\report.ps1 contract --variant custom

# 4) invoke 先を自作 Lambda へ切り替え
.\scripts\mctl.ps1 lambda custom

# 5) メンテナンスにして画面とレスポンスを確認
.\scripts\mctl.ps1 on intraweb
.\scripts\report.ps1 check intraweb /dashboard

# 6) 同梱実装へ戻す
.\scripts\mctl.ps1 lambda builtin
.\scripts\mctl.ps1 off all
```

切り替えは ALB ごとに行えます（`.\scripts\mctl.ps1 lambda custom intraweb`）。
状態はボリューム `alb-state` に永続化され、`docker compose restart` しても維持されます。

現在どちらが使われているかは、**レスポンスヘッダ `X-Alb-Lambda-Variant`** か次のコマンドで分かります。

```powershell
.\scripts\mctl.ps1 lambda           # 全 ALB の現在値と選択肢を表示
.\scripts\mctl.ps1 status           # メンテ状態と併せて表示
```

### 11-2. 自作 Lambda が守るべき契約

ALB の Lambda ターゲットグループ統合には決まった入出力形式があり、**外れると ALB が 502 を返します**
（実 ALB と同じ挙動）。`lambda-custom/app.py` の冒頭にも同じ説明を書いてあります。

受け取るイベント:

| キー | 内容 |
|---|---|
| `requestContext.elb.targetGroupArn` | 呼び出し元ターゲットグループ ARN（**web/api の判定に使う**） |
| `httpMethod` / `path` / `queryStringParameters` | リクエストライン |
| `headers` | 小文字化されたヘッダ（`x-forwarded-for` などを含む） |
| `body` / `isBase64Encoded` | ボディ |

返す値:

| キー | 型 | 必須 |
|---|---|---|
| `statusCode` | int（100–599） | ○ |
| `statusDescription` | str（例 `"503 Service Unavailable"`） | |
| `headers` | dict[str, **str**]（値が文字列でないと壊れます） | |
| `multiValueHeaders` | dict[str, list[str]] | |
| `body` | str（bytes を返すなら base64 化して `isBase64Encoded: true`） | ○ |
| `isBase64Encoded` | bool | |

### 11-3. 契約チェックの実行

`contract` サブコマンドが、4 サービス分の ARN でそれぞれ直接 invoke し、上記を検証します。

```powershell
.\scripts\report.ps1 contract --variant custom            # 自作 Lambda
.\scripts\report.ps1 contract --variant custom --screen   # 返ってきた画面も描画する
.\scripts\report.ps1 contract --variant builtin           # 同梱実装（比較用）
.\scripts\report.ps1 contract --variant custom --excel reports\契約チェック.xlsx
```

検証項目は 2 段階に分かれます。

- **必須**（外すと ALB が 502 を返す）… `statusCode` が int、`headers` の値がすべて文字列、
  `body` が str、`isBase64Encoded` が bool、関数エラーが出ないこと
- **推奨**（動くが直したい）… web 向け ARN なら `text/html` を返す、api 向け ARN なら HTML を
  返さない、`Retry-After` を付ける

出力例:

```
  PASS intraweb  (web 向け)
     ✔ statusCode                 期待=int (100-599)
     ✔ headers の値                期待=すべて文字列
     ✔ body                       期待=str
     ✔ Content-Type (web 向け)     期待=text/html*
     ✔ 本文 (web 向け)             期待=HTML であること
```

### 11-4. 全シナリオを両方の実装で検証する

```powershell
.\scripts\report.ps1 report --variant both --contract
```

同梱実装と自作実装で同じ 21 ケースを続けて実行し、**1 つの Excel ブックに「実装」列つきで
まとめて出力**します。「自作 Lambda に差し替えてもリスナールールの挙動が変わらないこと」を
そのまま比較できます。

---

## 12. リスナールールの仕組みと書き方

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

## 13. Lambda がどうやって Web/API を判定しているか

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

## 14. Lambda を単体で叩く（ALB を経由しない）

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
ポートを `9002` にすれば自作 Lambda（`lambda-custom/app.py`）を同じ方法で叩けます。

この 4 パターン（web 1 本 + api 3 本）を自動で叩き、戻り値が ALB 統合の契約を満たしているか
まで検証するのが `contract` サブコマンドです（→ [11-3](#11-3-契約チェックの実行)）。

```powershell
.\scripts\report.ps1 contract --variant builtin --screen   # 同梱実装
.\scripts\report.ps1 contract --variant custom  --screen   # 自作実装
```

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

## 15. レスポンスヘッダで挙動をデバッグする

ALB シミュレータは、検証しやすいよう毎回次のヘッダを付与します。

| ヘッダ | 意味 |
|---|---|
| `X-Alb-Name` | 応答した ALB 名（例 `alb-intraweb`） |
| `X-Alb-Matched-Rule` | マッチしたリスナールール名（`default` はデフォルトアクション） |
| `X-Alb-Rule-Priority` | そのルールの優先度 |
| `X-Alb-Target` | 転送先（`tg-xxx-ecs(ecs)` / `tg-xxx-maintenance(lambda)` / `fixed-response`） |
| `X-Alb-Maintenance` | その ALB のメンテナンス状態（`true` / `false`） |
| `X-Alb-Lambda-Variant` | **どの Lambda 実装が呼ばれたか**（`builtin` / `custom`）。Lambda 経由のときだけ付与 |
| `X-Alb-Lambda-Endpoint` | 実際に invoke した URL（差し替えの確認用） |
| `X-Alb-Duration-Ms` | ALB 内での処理時間（ミリ秒） |
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

分類済みのヘッダ一覧と画面表示をまとめて見たいときは albcheck が便利です:

```powershell
.\scripts\report.ps1 check intraweb /dashboard
```

---

## 16. ログの見方

```powershell
docker compose logs -f                      # 全部
docker compose logs -f alb-intraweb         # 特定 ALB のアクセスログ
docker compose logs -f maintenance-lambda   # 同梱 Lambda の実行ログ（受信イベント全文つき）
docker compose logs -f custom-lambda        # 自作 Lambda の実行ログ
docker compose logs -f intraweb             # ECS 側のログ
```

ALB のアクセスログ例（どのルールでどの Lambda 実装へ流れたかが 1 行で分かります）:

```
ALB=alb-intraweb maint=True client=172.24.0.1 "GET /dashboard"
  rule=maintenance-catch-all(prio=100) target=tg-intraweb-maintenance(lambda)
  variant=builtin status=503 6.8ms
```

Lambda のログ例:

```
maintenance response: service=intraweb kind=web status=503 method=GET path=/dashboard
```

---

## 17. カスタマイズ方法

| やりたいこと | 変更箇所 |
|---|---|
| **Lambda を自作のものに丸ごと差し替える** | `lambda-custom/app.py` を編集 → `docker compose up -d --build custom-lambda` → `.\scripts\mctl.ps1 lambda custom`（→ [11 章](#11-lambda-を自作のものに差し替えて検証する)） |
| メンテ画面のデザイン・文言を変える | `lambda/app.py` の `html_body()` → `docker compose up -d --build maintenance-lambda` |
| API のエラー JSON 構造を変える | `lambda/app.py` の `json_body()` → 同上 |
| 検証シナリオ（ケース）を増やす | `tools/albcheck.py` の `PLAN` にケースを追加 |
| Excel レポートの列やシートを変える | `tools/report_excel.py` |
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

## 18. 本番 AWS への持っていき方

この環境の各要素は、AWS の実リソースと次のように対応します。

| ローカル | AWS |
|---|---|
| `lambda/app.py` の `handler` | Lambda 関数（そのまま使えます。ランタイム Python 3.12） |
| `lambda-custom/app.py` の `handler` | 同上（自作実装。ローカルで契約チェックを通してからデプロイ） |
| variant の切り替え（`builtin`／`custom`） | ターゲットグループの登録先 Lambda を差し替え（`aws elbv2 register-targets`）／エイリアス切り替え |
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

## 19. トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| **`<urlopen error [Errno 111] Connection refused>` が返る** | そのポートで待ち受けているプロセスがありません（ブラウザの有無とは無関係）。`./scripts/report.sh doctor` が原因と対処コマンドを出します（→ [9-3 章](#9-3-ブラウザが使えない環境での確認ec2--session-manager-など)） |
| RHEL / Amazon Linux で `docker: command not found` | podman が入っていることが多いです。`podman compose up -d --build`。`report.sh` は `podman` / `nerdctl` も自動検出します |
| RHEL で ALB コンテナがすぐ落ちる（→ 接続拒否） | SELinux が Enforcing でバインドマウントを読めていない可能性。`docker compose -f docker-compose.yml -f docker-compose.selinux.yml up -d --build` |
| コンテナは Up なのにホストから届かない | rootless 実行やポート未公開。`./scripts/report.sh --in-container check intraweb /dashboard` でコンテナの中から確認 |
| ブラウザが開けないので画面を確認できない | 確認できます。`./scripts/report.sh check intraweb /dashboard` がメンテ画面をテキスト描画します。ファイルに残すなら `--save` / `--save-text` |
| `docker compose up` でポート競合エラー | 8081-8084 / 9001 / 9081-9084 が使用中。`docker-compose.yml` の `ports` を変更 |
| `.ps1` が「Unexpected token」で落ちる | 実行ポリシー or 文字コード。`powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1` で実行してください（同梱スクリプトは UTF-8 BOM 付きで保存済み） |
| すべて 502 が返る | Lambda コンテナが起動途中。`docker compose logs maintenance-lambda` を確認し数秒待つ |
| メンテ ON にしたのに 200 が返る | 優先度の高いバイパスルールにマッチしている。`X-Alb-Matched-Rule` ヘッダで確認 |
| `X-Forwarded-For` を付けても source-ip バイパスが効かない | ヘッダ名のスペル、CIDR（既定 `10.0.100.0/24`）を確認。`.\scripts\mctl.ps1 rules intraweb` で条件を確認 |
| YAML を編集しても反映されない | `POST /admin/reload` するか `docker compose restart alb-xxx` |
| メンテ状態が意図せず残っている | 状態はボリューム `alb-state` に永続化。`.\scripts\mctl.ps1 off all` か `docker compose down -v` |
| Lambda のレスポンス形式を壊した | ALB は不正なレスポンスに対し 502 を返します（実 ALB と同じ）。`.\scripts\report.ps1 contract --variant custom` でどの項目が契約違反か特定できます |
| 自作 Lambda に差し替えたのに応答が変わらない | `X-Alb-Lambda-Variant` ヘッダを確認。`builtin` のままなら `.\scripts\mctl.ps1 lambda custom`。コード変更後は `docker compose up -d --build custom-lambda` が必要 |
| 自作 Lambda に切り替えたら 502 になる | `custom-lambda` コンテナが起動していない、または戻り値の形式が不正。`docker compose logs custom-lambda` と `.\scripts\report.ps1 contract --variant custom` を確認 |
| `report.ps1` が「Python が無い」と言う | 自動で `docker compose run --rm inspector` にフォールバックします（そのまま使えます）。ホストで実行したい場合は Python 3.8 以上を入れてください |
| 画面表示の枠がずれる / 文字化けする | 端末のフォントが等幅でないか、コードページが UTF-8 でない。`chcp 65001` を実行するか Windows Terminal を使ってください。`--width` を狭めるのも有効です |
| Excel の「画面表示」シートで枠がずれる | 該当列のフォントが `MS Gothic` になっているか確認してください（等幅でないと崩れます） |

---

## 20. 環境の停止・削除

```powershell
docker compose stop                 # 停止（状態は保持）
docker compose start                # 再開
docker compose down                 # コンテナ削除（メンテ状態ボリュームは残る）
docker compose down -v              # ボリューム含め完全削除
docker compose down -v --rmi local  # ビルドしたイメージも削除
```

---

## 21. この環境で再現できること／できないこと

**再現できること**

- リスナールールの優先度評価、条件（path/host/method/source-ip/header/query）の AND/OR 挙動
- ECS ターゲットグループ ↔ Lambda ターゲットグループの切り替え
- ALB → Lambda の **イベント形式**（`requestContext.elb.targetGroupArn`、`headers`、`body`、`isBase64Encoded`）
- Lambda → ALB の **レスポンス形式**（`statusCode` / `headers` / `multiValueHeaders` / `body` / `isBase64Encoded`）
- Lambda のレスポンス形式が不正な場合に ALB が 502 を返す挙動
- ALB による `X-Forwarded-For` / `X-Forwarded-Proto` / `X-Forwarded-Port` / `X-Amzn-Trace-Id` の付与
- fixed-response / redirect アクション
- ALB ごとに独立したメンテナンス切り替え
- **自作 Lambda への差し替え**（ターゲットグループ ARN を保ったまま invoke 先だけを切り替え）
- **自作 Lambda が ALB 統合の契約を満たしているかの検証**（`contract` サブコマンド）

**再現していないこと（必要なら別途 AWS で確認）**

- HTTPS/ACM、SNI、TLS ポリシー
- ヘルスチェックによるターゲットの自動 unhealthy 判定、Connection draining
- WAF、認証アクション（OIDC/Cognito）、スティッキーセッション
- Lambda の同時実行数制限・コールドスタート・課金
- ALB アクセスログの S3 出力形式、CloudWatch メトリクス
