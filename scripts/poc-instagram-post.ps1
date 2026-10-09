# POC da consulta de post isolado do provider web do Instagram: SÓ resolução (sem banco, sem download).
# ATENÇÃO: a consulta de post NÃO foi verificada no Instagram real; rode só com autorização do dono.
# Tráfego: 1 GET do documento + 1 POST GraphQL. Usa a sessão salva, para no primeiro erro (sem nova tentativa) e
# imprime um relatório JSON sem segredos (requisições, forma da resposta, quantidade de mídias, itens).
# Feche o Media Tool antes (scripts\stop.ps1): a POC recusa rodar com o app escutando na porta.
# Uso: .\scripts\poc-instagram-post.ps1 -Url https://www.instagram.com/p/<codigo>/
param(
    [Parameter(Mandatory = $true)][string]$Url
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$script = Join-Path $root "backend\tools\poc_instagram_post.py"

if (-not (Test-Path $python)) {
    Write-Error "Python do projeto não encontrado em $python. Rode scripts\setup.ps1 antes."
}

& $python $script --url $Url
exit $LASTEXITCODE
