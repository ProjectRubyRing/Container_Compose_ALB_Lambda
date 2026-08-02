<#
.SYNOPSIS
  レスポンス確認ツール albcheck のラッパ (Windows / PowerShell)

.DESCRIPTION
  ホストに Python があればそれで、無ければ docker compose run --rm inspector で実行します。
  引数はそのまま tools/albcheck.py へ渡ります。

  先頭に --host / --in-container を付けると実行場所を指定できます。
    --host          ホストの Python で実行 (自動フォールバックしない)
    --in-container  検証用コンテナの中から実行 (ホストのポートに届かない環境向け)
  無指定のときは、ホストから届かなければコンテナ内実行へ自動で切り替えます。

.EXAMPLE
  .\scripts\report.ps1                              # 全シナリオ検証 + Excel レポート出力
  .\scripts\report.ps1 report --variant both        # builtin と custom を続けて検証
  .\scripts\report.ps1 report --contract            # Lambda 契約チェックも実施
  .\scripts\report.ps1 check intraweb /dashboard    # 1 リクエストの詳細 + 画面表示
  .\scripts\report.ps1 contract --variant custom    # 自作 Lambda の契約チェック
  .\scripts\report.ps1 variant custom               # invoke 先 Lambda を差し替える
  .\scripts\report.ps1 doctor                       # 繋がらないときの原因診断
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
  [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$tool = Join-Path $root 'tools\albcheck.py'

if (-not $Arguments) { $Arguments = @() }

# 先頭の実行場所オプションを取り出す
$mode = 'auto'
while ($Arguments.Count -gt 0 -and @('--host', '--in-container', '--container') -contains $Arguments[0]) {
  if ($Arguments[0] -eq '--host') { $mode = 'host' } else { $mode = 'container' }
  $Arguments = @($Arguments | Select-Object -Skip 1)
}
if ($Arguments.Count -eq 0) { $Arguments = @('report') }

# 枠線や日本語が化けないよう、コンソールの出力エンコーディングを UTF-8 にする
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }

function Invoke-InContainer {
  Write-Host 'コンテナの中で実行します (docker compose run --rm inspector)' -ForegroundColor Yellow
  Push-Location $root
  try {
    docker compose run --rm inspector @Arguments
    exit $LASTEXITCODE
  }
  finally { Pop-Location }
}

if ($mode -eq 'container') { Invoke-InContainer }

if (-not $python) {
  Write-Host 'ホストに Python が無いためコンテナの中で実行します' -ForegroundColor Yellow
  Invoke-InContainer
}

$env:PYTHONIOENCODING = 'utf-8'

# auto のときは、ホストから検証環境へ届くかを先に確認する
# (doctor 自体は「届かない理由」を出すのが仕事なので、常にホストで実行する)
if ($mode -eq 'auto' -and -not ($Arguments -contains 'doctor')) {
  & $python.Source $tool doctor --quiet 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Write-Host 'ホストから検証環境へ届かないため、コンテナの中から実行します' -ForegroundColor Yellow
    Write-Host '  (ホストで実行したい場合は --host、原因を見るには .\scripts\report.ps1 doctor)' -ForegroundColor DarkGray
    Invoke-InContainer
  }
}

& $python.Source $tool @Arguments
exit $LASTEXITCODE
