$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 app.py @args
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python app.py @args
} else {
    Write-Error 'Python 3.10 or newer is required.'
}
