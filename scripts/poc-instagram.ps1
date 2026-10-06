# Prova de conceito do adapter Instagram: testa a sessão, lê o perfil, lista 1 mídia e baixa só ela
# para storage\temp\poc-<timestamp>. Não imprime cookies.
# Uso: .\scripts\poc-instagram.ps1 -Username instagram -Content reels
param(
    [Parameter(Mandatory = $true)][string]$Username,
    [ValidateSet("reels", "posts", "videos", "images", "all")][string]$Content = "reels"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$script = Join-Path $root "backend\tools\poc_instagram.py"

if (-not (Test-Path $python)) {
    Write-Error "Python do projeto não encontrado em $python. Rode scripts\setup.ps1 antes."
}

& $python $script --profile $Username --content $Content
exit $LASTEXITCODE
