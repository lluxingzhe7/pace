param(
  [int]$Port = 9333,
  [string]$Url = "http://127.0.0.1:8765/#/activities",
  [string]$Steps = ".ui/steps.json",
  [string]$Out = ".ui"
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$node = Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
$profile = Join-Path $root ".edge-profile"

if (Test-Path $profile) { Remove-Item $profile -Recurse -Force -ErrorAction SilentlyContinue }

$proc = Start-Process -FilePath $edge -ArgumentList @(
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-crash-reporter", "--remote-debugging-port=$Port",
  "--user-data-dir=$profile", "about:blank"
) -PassThru

$ready = $false
for ($i = 0; $i -lt 40; $i++) {
  try { Invoke-RestMethod "http://127.0.0.1:$Port/json/version" -TimeoutSec 1 | Out-Null; $ready = $true; break }
  catch { Start-Sleep -Milliseconds 400 }
}
if (-not $ready) { Write-Output "EDGE_NOT_READY"; exit 3 }

Push-Location $root
try {
  & $node tools/ui_probe.mjs --port $Port --url $Url --steps $Steps --out $Out
  $code = $LASTEXITCODE
} finally {
  Pop-Location
  Start-Sleep -Milliseconds 400
  $own = [int](($proc.Id))
  Stop-Process -Id $own -Force -ErrorAction SilentlyContinue
  $line = netstat -ano | Select-String ":$Port" | Select-String "LISTENING" | Select-Object -First 1
  if ($line) {
    $owner = [int](($line.Line -split '\s+')[-1])
    Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
  }
}
exit $code
