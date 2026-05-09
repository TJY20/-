param(
  [int]$BackendPort = 8765,
  [int]$FrontendPort = 3000,
  [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Join-Path $Root "backend"
$FrontendDir = Join-Path $Root "frontend"
$VenvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
$ApiBase = "http://127.0.0.1:$BackendPort"

function Resolve-Executable($Name, $Candidates, $InstallHint) {
  foreach ($candidate in $Candidates) {
    if ($candidate -and (Test-Path $candidate)) {
      return $candidate
    }
  }

  $command = Get-Command $Name -ErrorAction SilentlyContinue
  if ($command) {
    $path = $command.Source
    if ($path -and ($path -notlike "*\Microsoft\WindowsApps\*")) {
      return $path
    }
  }

  throw "$Name was not found. $InstallHint"
}

$PythonCmd = Resolve-Executable "python" @(
  "$env:LocalAppData\Programs\Python\Python311\python.exe",
  "$env:LocalAppData\Programs\Python\Python312\python.exe"
) "Install Python 3.11+ and add it to PATH."

$NpmCmd = Resolve-Executable "npm" @(
  "$env:ProgramFiles\nodejs\npm.cmd",
  "${env:ProgramFiles(x86)}\nodejs\npm.cmd"
) "Install Node.js 20+ and add npm to PATH."
$NodeCmd = Resolve-Executable "node" @(
  "$env:ProgramFiles\nodejs\node.exe",
  "${env:ProgramFiles(x86)}\nodejs\node.exe"
) "Install Node.js 20+ and add node to PATH."
$NodeDir = Split-Path -Parent $NpmCmd
$env:Path = "$NodeDir;$env:Path"

if (-not (Test-Path $VenvPython)) {
  Write-Host "Creating backend virtual environment..."
  Push-Location $BackendDir
  try {
    & $PythonCmd -m venv .venv
  } finally {
    Pop-Location
  }
}

if (-not $SkipInstall) {
  Write-Host "Installing backend dependencies..."
  Push-Location $BackendDir
  try {
    & $VenvPython -m pip install -e .
  } finally {
    Pop-Location
  }

  if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
    Write-Host "Installing frontend dependencies..."
    Push-Location $FrontendDir
    try {
      & $NpmCmd install
    } finally {
      Pop-Location
    }
  }
}

@"
NEXT_PUBLIC_API_BASE=$ApiBase
INTERNAL_API_BASE=$ApiBase
"@ | Set-Content -Path (Join-Path $FrontendDir ".env.local") -Encoding UTF8

Write-Host "Starting backend at $ApiBase"
$backendJob = Start-Job -Name "bike-share-backend" -ScriptBlock {
  param($BackendDir, $VenvPython, $BackendPort)
  Set-Location $BackendDir
  & $VenvPython -m uvicorn app.main:app --host 127.0.0.1 --port $BackendPort --reload
} -ArgumentList $BackendDir, $VenvPython, $BackendPort

Write-Host "Starting frontend at http://localhost:$FrontendPort"
$frontendJob = Start-Job -Name "bike-share-frontend" -ScriptBlock {
  param($FrontendDir, $FrontendPort, $ApiBase, $NodeCmd, $NodeDir)
  Set-Location $FrontendDir
  $env:Path = "$NodeDir;$env:Path"
  $env:NEXT_PUBLIC_API_BASE = $ApiBase
  $env:INTERNAL_API_BASE = $ApiBase
  & $NodeCmd ".\node_modules\next\dist\bin\next" dev --hostname 127.0.0.1 --port $FrontendPort
} -ArgumentList $FrontendDir, $FrontendPort, $ApiBase, $NodeCmd, $NodeDir

Write-Host ""
Write-Host "Local deployment is running:"
Write-Host "  Frontend: http://localhost:$FrontendPort"
Write-Host "  Backend:  $ApiBase/api/health"
Write-Host ""
Write-Host "Press Ctrl+C to stop both services."

try {
  while ($true) {
    Receive-Job $backendJob
    Receive-Job $frontendJob
    Start-Sleep -Seconds 2
  }
} finally {
  Stop-Job $backendJob, $frontendJob -ErrorAction SilentlyContinue
  Remove-Job $backendJob, $frontendJob -Force -ErrorAction SilentlyContinue
}
