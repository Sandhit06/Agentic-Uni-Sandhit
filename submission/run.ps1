$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$env:PYTHONPATH = "$ScriptDir;$RepoRoot;$env:PYTHONPATH"
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
& $python -m submission.controller @args
exit $LASTEXITCODE
