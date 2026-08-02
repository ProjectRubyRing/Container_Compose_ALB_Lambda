<#
.SYNOPSIS
  ALB リスナールール + Lambda メンテナンス応答の自動検証 (Windows / PowerShell)

.DESCRIPTION
  1) 通常時   : 4 ALB すべてが ECS サービスへフォワードし 200 を返すこと
  2) メンテ中 : web (intraweb) は Lambda が HTML のメンテナンス画面 (503) を返すこと
                api (interapi/intraapi/sfapi) は Lambda が JSON + HTTP 503 を返すこと
  3) バイパスルール / ヘルスチェックルールが優先度どおりに効くこと
  終了コード 0 = 全件 PASS

.EXAMPLE
  .\scripts\verify.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$script:pass = 0
$script:fail = 0

$Alb = @{ intraweb = 8081; interapi = 8082; intraapi = 8083; sfapi = 8084 }
$Adm = @{ intraweb = 9081; interapi = 9082; intraapi = 9083; sfapi = 9084 }

# Windows PowerShell 5.1 には -SkipHttpErrorCheck が無いため、
# 4xx/5xx でも例外にせず本文を読める HttpWebRequest ベースの実装を使う
function Get-Res {
  param([string]$Url, [hashtable]$Headers = @{}, [string]$Method = 'GET')
  $req = [System.Net.HttpWebRequest]::Create($Url)
  $req.Method = $Method
  $req.AllowAutoRedirect = $false
  $req.Timeout = 15000
  foreach ($k in $Headers.Keys) {
    if ($k -ieq 'Host') { $req.Host = $Headers[$k] } else { $req.Headers.Add($k, $Headers[$k]) }
  }
  try { $resp = $req.GetResponse() } catch [System.Net.WebException] { $resp = $_.Exception.Response }
  $status = [int]$resp.StatusCode
  $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
  $body = $sr.ReadToEnd(); $sr.Close()
  $hdr = @{}
  foreach ($h in $resp.Headers.AllKeys) { $hdr[$h] = $resp.Headers[$h] }
  $resp.Close()
  return [pscustomobject]@{ Status = $status; Body = $body; Headers = $hdr }
}

function Assert {
  param([string]$Name, [bool]$Condition, [string]$Detail = '')
  if ($Condition) { $script:pass++; Write-Host ("  [PASS] " + $Name) -ForegroundColor Green }
  else { $script:fail++; Write-Host ("  [FAIL] " + $Name + "  " + $Detail) -ForegroundColor Red }
}

function Set-Maint {
  param([string]$Service, [bool]$On)
  $verb = if ($On) { 'on' } else { 'off' }
  foreach ($s in @($Service)) {
    Invoke-RestMethod "http://localhost:$($Adm[$s])/admin/maintenance/$verb" -Method Post | Out-Null
  }
}

function Set-LambdaVariant {
  param([string[]]$Service, [string]$Variant)
  $body = @{ variant = $Variant; note = 'verify' } | ConvertTo-Json -Compress
  foreach ($s in @($Service)) {
    Invoke-RestMethod "http://localhost:$($Adm[$s])/admin/lambda" -Method Post `
      -Body $body -ContentType 'application/json' | Out-Null
  }
}

Write-Host "`n=== 0. 事前準備: 全 ALB を通常モードへ ===" -ForegroundColor Cyan
foreach ($s in $Alb.Keys) { Set-Maint $s $false }
Start-Sleep -Milliseconds 300

# ---------------------------------------------------------------------------
Write-Host "`n=== 1. 通常時: ECS サービスへフォワードされる ===" -ForegroundColor Cyan
foreach ($s in @('intraweb', 'interapi', 'intraapi', 'sfapi')) {
  $r = Get-Res "http://localhost:$($Alb[$s])/v1/orders"
  Assert "$s : HTTP 200" ($r.Status -eq 200) "actual=$($r.Status)"
  Assert "$s : ECS コンテナが応答 (X-Backend-Service)" ($r.Headers['X-Backend-Service'] -eq $s) "actual=$($r.Headers['X-Backend-Service'])"
  Assert "$s : 適用ルール = default" ($r.Headers['X-Alb-Matched-Rule'] -eq 'default') "actual=$($r.Headers['X-Alb-Matched-Rule'])"
}

# ---------------------------------------------------------------------------
Write-Host "`n=== 2. メンテナンス中: web は Lambda が HTML 画面を返す ===" -ForegroundColor Cyan
Set-Maint 'intraweb' $true
Start-Sleep -Milliseconds 200
$r = Get-Res "http://localhost:8081/dashboard"
Assert "intraweb : HTTP 503" ($r.Status -eq 503) "actual=$($r.Status)"
Assert "intraweb : Content-Type = text/html" ($r.Headers['Content-Type'] -like 'text/html*') "actual=$($r.Headers['Content-Type'])"
Assert "intraweb : Lambda 経由 (X-Alb-Target)" ($r.Headers['X-Alb-Target'] -like '*lambda*') "actual=$($r.Headers['X-Alb-Target'])"
Assert "intraweb : メンテ画面の文言を含む" ($r.Body -match 'メンテナンス中') ""
Assert "intraweb : Retry-After ヘッダあり" ($null -ne $r.Headers['Retry-After']) ""
Assert "intraweb : 適用ルール = maintenance-catch-all" ($r.Headers['X-Alb-Matched-Rule'] -eq 'maintenance-catch-all') "actual=$($r.Headers['X-Alb-Matched-Rule'])"

Write-Host "`n--- 2-1. ヘルスチェックはメンテ中も ECS へ通る (priority 1) ---"
$r = Get-Res "http://localhost:8081/healthz"
Assert "intraweb : /healthz は 200" ($r.Status -eq 200) "actual=$($r.Status)"
Assert "intraweb : /healthz は ECS が応答" ($r.Headers['X-Alb-Target'] -like '*ecs*') "actual=$($r.Headers['X-Alb-Target'])"

Write-Host "`n--- 2-2. 運用者 IP (10.0.100.0/24) はメンテ中もバイパス ---"
$r = Get-Res "http://localhost:8081/dashboard" @{ 'X-Forwarded-For' = '10.0.100.5' }
Assert "intraweb : 許可 IP は 200" ($r.Status -eq 200) "actual=$($r.Status)"
Assert "intraweb : 許可 IP は maintenance-bypass-source-ip にマッチ" ($r.Headers['X-Alb-Matched-Rule'] -eq 'maintenance-bypass-source-ip') "actual=$($r.Headers['X-Alb-Matched-Rule'])"
$r = Get-Res "http://localhost:8081/dashboard" @{ 'X-Forwarded-For' = '192.168.10.5' }
Assert "intraweb : 対象外 IP は 503" ($r.Status -eq 503) "actual=$($r.Status)"

# ---------------------------------------------------------------------------
Write-Host "`n=== 3. メンテナンス中: api は Lambda が HTTP ステータスコードを返す ===" -ForegroundColor Cyan
foreach ($s in @('interapi', 'intraapi', 'sfapi')) {
  Set-Maint $s $true
}
Start-Sleep -Milliseconds 200
foreach ($s in @('interapi', 'intraapi', 'sfapi')) {
  $r = Get-Res "http://localhost:$($Alb[$s])/v1/orders"
  Assert "$s : HTTP 503" ($r.Status -eq 503) "actual=$($r.Status)"
  Assert "$s : Content-Type = application/json" ($r.Headers['Content-Type'] -like 'application/json*') "actual=$($r.Headers['Content-Type'])"
  Assert "$s : Lambda 経由" ($r.Headers['X-Alb-Target'] -like '*lambda*') "actual=$($r.Headers['X-Alb-Target'])"
  Assert "$s : HTML ではない (機械向け応答)" ($r.Body -notmatch '<html') ""
  $json = $r.Body | ConvertFrom-Json
  Assert "$s : code = SERVICE_UNDER_MAINTENANCE" ($json.error.code -eq 'SERVICE_UNDER_MAINTENANCE') "actual=$($json.error.code)"
  Assert "$s : Lambda がサービスを正しく識別" ($json.error.service -eq $s) "actual=$($json.error.service)"
}

Write-Host "`n--- 3-1. interapi: /v1/ping はメンテ中も ECS へ (priority 10) ---"
$r = Get-Res "http://localhost:8082/v1/ping"
Assert "interapi : /v1/ping は 200" ($r.Status -eq 200) "actual=$($r.Status)"
Assert "interapi : maintenance-bypass-ping にマッチ" ($r.Headers['X-Alb-Matched-Rule'] -eq 'maintenance-bypass-ping') "actual=$($r.Headers['X-Alb-Matched-Rule'])"

Write-Host "`n--- 3-2. intraapi: 運用ヘッダでバイパス ---"
$r = Get-Res "http://localhost:8083/v1/orders" @{ 'X-Maintenance-Bypass' = 'ops-secret-token' }
Assert "intraapi : 正しいトークンは 200" ($r.Status -eq 200) "actual=$($r.Status)"
$r = Get-Res "http://localhost:8083/v1/orders" @{ 'X-Maintenance-Bypass' = 'wrong-token' }
Assert "intraapi : 誤ったトークンは 503" ($r.Status -eq 503) "actual=$($r.Status)"

Write-Host "`n--- 3-3. sfapi: /internal/* は ALB の fixed-response (Lambda 非経由) ---"
$r = Get-Res "http://localhost:8084/internal/sync"
Assert "sfapi : /internal/* は 503" ($r.Status -eq 503) "actual=$($r.Status)"
Assert "sfapi : fixed-response が応答" ($r.Headers['X-Alb-Target'] -eq 'fixed-response') "actual=$($r.Headers['X-Alb-Target'])"

# ---------------------------------------------------------------------------
Write-Host "`n=== 4. ALB ごとに独立して切り替わること ===" -ForegroundColor Cyan
Set-Maint 'interapi' $false
Start-Sleep -Milliseconds 200
$r1 = Get-Res "http://localhost:8082/v1/orders"
$r2 = Get-Res "http://localhost:8083/v1/orders"
Assert "interapi のみ復旧 -> 200" ($r1.Status -eq 200) "actual=$($r1.Status)"
Assert "intraapi はメンテ継続 -> 503" ($r2.Status -eq 503) "actual=$($r2.Status)"

# ---------------------------------------------------------------------------
Write-Host "`n=== 5. Lambda 実装の差し替え (builtin <-> custom) ===" -ForegroundColor Cyan
Set-Maint 'intraweb' $true
Set-Maint 'sfapi' $true
Start-Sleep -Milliseconds 200

Write-Host "`n--- 5-1. 既定は同梱実装 (builtin) ---"
Set-LambdaVariant @('intraweb', 'sfapi') 'builtin'
$r = Get-Res "http://localhost:8081/dashboard"
Assert "intraweb : X-Alb-Lambda-Variant = builtin" ($r.Headers['X-Alb-Lambda-Variant'] -eq 'builtin') "actual=$($r.Headers['X-Alb-Lambda-Variant'])"

Write-Host "`n--- 5-2. 自作 Lambda (custom) へ差し替え ---"
Set-LambdaVariant @('intraweb', 'sfapi') 'custom'
Start-Sleep -Milliseconds 200
$r = Get-Res "http://localhost:8081/dashboard"
Assert "intraweb : 差し替え後も 503" ($r.Status -eq 503) "actual=$($r.Status)"
Assert "intraweb : 自作 Lambda が応答" ($r.Headers['X-Alb-Lambda-Variant'] -eq 'custom') "actual=$($r.Headers['X-Alb-Lambda-Variant'])"
Assert "intraweb : 自作実装の目印ヘッダ" ($r.Headers['X-Maintenance-Impl'] -eq 'custom') "actual=$($r.Headers['X-Maintenance-Impl'])"
Assert "intraweb : web なので HTML" ($r.Headers['Content-Type'] -like 'text/html*') "actual=$($r.Headers['Content-Type'])"
Assert "intraweb : 適用ルールは変わらない" ($r.Headers['X-Alb-Matched-Rule'] -eq 'maintenance-catch-all') "actual=$($r.Headers['X-Alb-Matched-Rule'])"

$r = Get-Res "http://localhost:8084/v1/orders"
Assert "sfapi : 自作 Lambda が応答" ($r.Headers['X-Alb-Lambda-Variant'] -eq 'custom') "actual=$($r.Headers['X-Alb-Lambda-Variant'])"
Assert "sfapi : api なので JSON" ($r.Headers['Content-Type'] -like 'application/json*') "actual=$($r.Headers['Content-Type'])"
Assert "sfapi : HTML ではない" ($r.Body -notmatch '<html') ""
$json = $r.Body | ConvertFrom-Json
Assert "sfapi : 自作 Lambda もサービスを識別" ($json.error.service -eq 'sfapi') "actual=$($json.error.service)"

Write-Host "`n--- 5-3. 同梱実装 (builtin) へ戻す ---"
Set-LambdaVariant @('intraweb', 'sfapi') 'builtin'
Start-Sleep -Milliseconds 200
$r = Get-Res "http://localhost:8081/dashboard"
Assert "intraweb : builtin に戻る" ($r.Headers['X-Alb-Lambda-Variant'] -eq 'builtin') "actual=$($r.Headers['X-Alb-Lambda-Variant'])"
Assert "intraweb : 目印ヘッダが消える" ($null -eq $r.Headers['X-Maintenance-Impl']) "actual=$($r.Headers['X-Maintenance-Impl'])"

# ---------------------------------------------------------------------------
Write-Host "`n=== 6. 後片付け: 全 ALB を通常モードへ ===" -ForegroundColor Cyan
foreach ($s in $Alb.Keys) { Set-Maint $s $false }
Start-Sleep -Milliseconds 200
foreach ($s in @('intraweb', 'interapi', 'intraapi', 'sfapi')) {
  $r = Get-Res "http://localhost:$($Alb[$s])/"
  Assert "$s : 復旧後 200" ($r.Status -eq 200) "actual=$($r.Status)"
}

Write-Host ""
Write-Host ("=" * 60)
Write-Host ("結果: PASS=$script:pass  FAIL=$script:fail") -ForegroundColor $(if ($script:fail -eq 0) { 'Green' } else { 'Red' })
Write-Host ("=" * 60)
if ($script:fail -gt 0) { exit 1 }
exit 0
