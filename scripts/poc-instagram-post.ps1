# POC do documento público de UMA publicação do Instagram: SÓ leitura (sem banco, sem download, SEM sessão).
# ATENÇÃO: verificado no Instagram real só para um Reel público (2026-10-10); rode só com autorização do dono.
# Tráfego: 1 GET anônimo do documento da publicação (sem cookies, sem seguir redirect). Para no primeiro erro (sem
# nova tentativa) e imprime um relatório JSON sem segredos (requisição, status, leitura do documento, mídias).
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
