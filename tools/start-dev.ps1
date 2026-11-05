$ErrorActionPreference='Stop'
$repoRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location -LiteralPath $repoRoot
try {
  if(-not(Test-Path -LiteralPath '.env.local')){Copy-Item -LiteralPath '.env.example' -Destination '.env.local'}
  & docker compose up -d --wait db
  if($LASTEXITCODE -ne 0){throw 'Start Docker Desktop before starting the API.'}
  $taskPython=Join-Path $repoRoot '.venv/Scripts/python.exe'
  if(-not(Test-Path -LiteralPath $taskPython)){throw 'Create .venv and install api/requirements-dev.txt first. See README.md.'}
  & $taskPython -m api.migrate
  if($LASTEXITCODE -ne 0){throw 'Database migration failed.'}
  & $taskPython -m uvicorn api.main:app --host 127.0.0.1 --port 8808 --reload --no-access-log
} finally {Pop-Location}
