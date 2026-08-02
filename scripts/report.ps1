<#
.SYNOPSIS
  レスポンス確認ツール albcheck のラッパ (Windows / PowerShell)

.DESCRIPTION
  ホストに Python があればそれで、無ければ docker compose run --rm inspector で実行します。
  引数はそのまま tools/albcheck.py へ渡ります。

.EXAMPLE
  .\scripts\report.ps1                              # 全シナリオ検証 + Excel レポート出力
  .\scripts\report.ps1 report --variant both        # builtin と custom を続けて検証
  .\scripts\report.ps1 report --contract            # Lambda 契約チェックも実施
  .\scripts\report.ps1 check intraweb /dashboard    # 1 リクエストの詳細 + 画面表示
  .\scripts\report.ps1 contract --variant custom    # 自作 Lambda の契約チェック
  .\scripts\report.ps1 variant custom               # invoke 先 Lambda を差し替える
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
  [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$tool = Join-Path $root 'tools\albcheck.py'

if (-not $Arguments -or $Arguments.Count -eq 0) { $Arguments = @('report') }

# 枠線や日本語が化けないよう、コンソールの出力エンコーディングを UTF-8 にする
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }

if ($python) {
  $env:PYTHONIOENCODING = 'utf-8'
  & $python.Source $tool @Arguments
  exit $LASTEXITCODE
}

Write-Host 'ホストに Python が無いため docker compose run --rm inspector で実行します' -ForegroundColor Yellow
Push-Location $root
try {
  docker compose run --rm inspector @Arguments
  exit $LASTEXITCODE
}
finally { Pop-Location }
