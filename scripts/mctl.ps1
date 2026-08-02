<#
.SYNOPSIS
  メンテナンスモード / Lambda 差し替え操作ツール (Windows / PowerShell)

.EXAMPLE
  .\scripts\mctl.ps1 status
  .\scripts\mctl.ps1 on  intraweb
  .\scripts\mctl.ps1 off intraweb
  .\scripts\mctl.ps1 on  all
  .\scripts\mctl.ps1 rules intraweb
  .\scripts\mctl.ps1 lambda                  # 現在の Lambda 実装 (variant) を表示
  .\scripts\mctl.ps1 lambda custom           # 全 ALB を自作 Lambda へ差し替え
  .\scripts\mctl.ps1 lambda builtin intraweb # intraweb だけ同梱実装へ戻す
#>
param(
  [Parameter(Position = 0)][ValidateSet('status', 'on', 'off', 'rules', 'lambda')][string]$Action = 'status',
  [Parameter(Position = 1)][string]$Service = 'all',
  [Parameter(Position = 2)][string]$Target = 'all'
)

$AdminPorts = @{
  intraweb = 9081
  interapi = 9082
  intraapi = 9083
  sfapi    = 9084
}

# `lambda` のときは 2 番目の引数を variant 名として解釈する
#   mctl.ps1 lambda custom            -> variant=custom / 対象=all
#   mctl.ps1 lambda custom intraweb   -> variant=custom / 対象=intraweb
#   mctl.ps1 lambda                   -> 表示のみ
$Variant = ''
if ($Action -eq 'lambda' -and -not $AdminPorts.ContainsKey($Service) -and $Service -ne 'all') {
  $Variant = $Service
  $Service = $Target
}

function Get-Targets([string]$svc) {
  if ($svc -eq 'all') { return $AdminPorts.Keys | Sort-Object }
  if (-not $AdminPorts.ContainsKey($svc)) {
    Write-Error "unknown service: $svc (intraweb|interapi|intraapi|sfapi|all)"; exit 1
  }
  return @($svc)
}

foreach ($s in Get-Targets $Service) {
  $port = $AdminPorts[$s]
  $base = "http://localhost:$port"
  try {
    switch ($Action) {
      'status' {
        $r = Invoke-RestMethod "$base/admin/state"
        $flag = if ($r.maintenance) { 'MAINTENANCE' } else { 'NORMAL     ' }
        "{0,-10} {1}  lambda={2,-8} updated={3} {4}" -f $r.service, $flag, $r.lambda_variant, $r.updated_at, $r.note
      }
      'on' {
        $r = Invoke-RestMethod "$base/admin/maintenance/on" -Method Post
        "{0,-10} -> maintenance = {1}" -f $s, $r.maintenance
      }
      'off' {
        $r = Invoke-RestMethod "$base/admin/maintenance/off" -Method Post
        "{0,-10} -> maintenance = {1}" -f $s, $r.maintenance
      }
      'rules' {
        Invoke-RestMethod "$base/admin/rules" | ConvertTo-Json -Depth 8
      }
      'lambda' {
        if ($Variant) {
          $body = @{ variant = $Variant; note = 'mctl' } | ConvertTo-Json -Compress
          $r = Invoke-RestMethod "$base/admin/lambda" -Method Post -Body $body -ContentType 'application/json'
          "{0,-10} -> lambda variant = {1}" -f $s, $r.variant
        }
        else {
          $r = Invoke-RestMethod "$base/admin/lambda"
          "{0,-10} lambda variant = {1,-8} (選択肢: {2})" -f $r.service, $r.variant, ($r.available -join ', ')
        }
      }
    }
  }
  catch {
    Write-Warning "$s ($base): $($_.Exception.Message)"
  }
}
