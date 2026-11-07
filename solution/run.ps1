param(
  [Parameter(Mandatory=$true, Position=0)][string]$SimUrl,
  [string]$StateDir = "$PSScriptRoot\state",
  [int]$ViewPort = 8081
)
$repoRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = "$repoRoot;$env:PYTHONPATH"
$env:CONTROLLER_STATE_DIR = $StateDir
$env:CONTROLLER_VIEW_PORT = "$ViewPort"
python -m solution.controller $SimUrl
exit $LASTEXITCODE
