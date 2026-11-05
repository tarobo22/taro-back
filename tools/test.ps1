$ErrorActionPreference='Stop'
$repoRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location -LiteralPath $repoRoot
try {
  & docker compose --profile test up -d --wait db-test
  if($LASTEXITCODE -ne 0){throw 'Test database did not start.'}
  $env:YEOUN_TEST_DATABASE_URL='postgresql://taro_test:local_test_only@127.0.0.1:55441/taro_journal_test'
  & (Join-Path $repoRoot '.venv/Scripts/python.exe') -m pytest -q
  exit $LASTEXITCODE
} finally {Pop-Location}
