$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $python) {
    & $python.Source -3 -c "import sys, tkinter; assert sys.version_info >= (3, 10), 'Python 3.10+ required'"
    if ($LASTEXITCODE -ne 0) { throw "Python 3.10+ with Tkinter is required." }
    & $python.Source -3 (Join-Path $scriptDir "main.py")
    exit $LASTEXITCODE
}
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) {
    Write-Error "未找到 Python 3，请先安装 Python 3.10 或更高版本。"
}
& $python.Source -c "import sys, tkinter; assert sys.version_info >= (3, 10), 'Python 3.10+ required'"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10+ with Tkinter is required." }
& $python.Source (Join-Path $scriptDir "main.py")
exit $LASTEXITCODE
