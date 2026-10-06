# POC de paginação do provider web do Instagram: SÓ listagem (sem banco, sem download, sem processamento).
# Padrão: -PageSize 1 -Limit 2 (exatamente 2 páginas: 1 GET do documento + 2 POSTs GraphQL). Usa a sessão salva,
# para no primeiro erro (sem nova tentativa) e imprime um relatório JSON sem segredos (requisições, páginas, itens).
# Feche o Media Tool antes (scripts\stop.ps1): a POC recusa rodar com o app escutando na porta.
# Uso: .\scripts\poc-instagram-pagination.ps1 -Username instagram [-PageSize 1] [-Limit 2]
param(
    [Parameter(Mandatory = $true)][string]$Username,
    [int]$PageSize = 1,
    [int]$Limit = 2
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$script = Join-Path $root "backend\tools\poc_instagram_pagination.py"

if (-not (Test-Path $python)) {
    Write-Error "Python do projeto não encontrado em $python. Rode scripts\setup.ps1 antes."
}

& $python $script --profile $Username --page-size $PageSize --limit $Limit
exit $LASTEXITCODE
