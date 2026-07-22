<#
.SYNOPSIS
  メンテナンスモード操作ツール (Windows / PowerShell)

.EXAMPLE
  .\scripts\mctl.ps1 status
  .\scripts\mctl.ps1 on  intraweb
  .\scripts\mctl.ps1 off intraweb
  .\scripts\mctl.ps1 on  all
  .\scripts\mctl.ps1 rules intraweb
#>
param(
  [Parameter(Position = 0)][ValidateSet('status', 'on', 'off', 'rules')][string]$Action = 'status',
  [Parameter(Position = 1)][string]$Service = 'all'
)

$AdminPorts = @{
  intraweb = 9081
  interapi = 9082
  intraapi = 9083
  sfapi    = 9084
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
        "{0,-10} {1}  updated={2} {3}" -f $r.service, $flag, $r.updated_at, $r.note
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
    }
  }
  catch {
    Write-Warning "$s ($base): $($_.Exception.Message)"
  }
}
