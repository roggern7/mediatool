# POC do provider web do Instagram sobre o app real: cria UM lote (limite 1, sem processar), baixa a mídia para
# storage\originals e registra no SQLite de data\. Com -Process, processa a mídia com o preset Anti-duplicidade.
# Para no primeiro erro (sem nova tentativa) e imprime um relatório JSON sem segredos.
# Feche o Media Tool antes (scripts\stop.ps1): a POC usa a mesma fila.
# Uso: .\scripts\poc-instagram-web.ps1 -Username instagram [-Content all] [-Process]
param(
    [Parameter(Mandatory = $true)][string]$Username,
    [ValidateSet("reels", "posts", "videos", "images", "all")][string]$Content = "all",
    [switch]$Process
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$script = Join-Path $root "backend\tools\poc_instagram_web.py"

if (-not (Test-Path $python)) {
    Write-Error "Python do projeto não encontrado em $python. Rode scripts\setup.ps1 antes."
}

$arguments = @($script, "--profile", $Username, "--content", $Content)
if ($Process) { $arguments += "--process" }
& $python @arguments
exit $LASTEXITCODE
