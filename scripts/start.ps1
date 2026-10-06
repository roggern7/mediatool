<#
.SYNOPSIS
    Inicia o Media Tool (backend FastAPI/uvicorn e, opcionalmente, o Vite de desenvolvimento).

.DESCRIPTION
    Valida o ambiente (.venv, pacotes importáveis, frontend\dist), verifica a porta, inicia o backend em
    segundo plano, grava o PID em data\.runtime\backend.pid e espera /api/health responder.
    O log da aplicação fica só em logs\media-tool.log (rotativo); em segundo plano ele não é repetido no
    console (MEDIA_TOOL_LOG_CONSOLE=0) e logs\backend.out.log / logs\backend.err.log guardam só mensagens do
    servidor (início, erros de inicialização). O access log do uvicorn fica desligado (--no-access-log).

.PARAMETER Port
    Porta do backend (padrão 8765). O backend recebe MEDIA_TOOL_HOST=127.0.0.1 e MEDIA_TOOL_PORT=<Port>.

.PARAMETER Dev
    Também inicia o Vite (npm run dev) em http://127.0.0.1:5173 com PID em data\.runtime\frontend.pid.
    O backend iniciado com -Dev recebe MEDIA_TOOL_DEV=1 (só no processo dele), que libera a origem do Vite.
    Os processos filhos não herdam variáveis INSTAGRAM_* (o backend mantém só INSTAGRAM_SESSIONID, se definida).

.PARAMETER Foreground
    Roda o backend no console atual (Ctrl+C para parar). O log da aplicação aparece no console e em
    logs\media-tool.log.

.PARAMETER Open
    Abre o painel no navegador padrão (com -Dev, abre o Vite).

.PARAMETER NoBuild
    Não gera o build do frontend se frontend\dist estiver ausente (apenas avisa; a API funciona mesmo assim).
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [switch]$Dev,
    [switch]$Foreground,
    [switch]$Open,
    [switch]$NoBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'lib\common.ps1')

$root = Get-MtRoot
$python = Get-MtVenvPython
$runtimeDir = Get-MtRuntimeDir
$logDir = Get-MtPath 'logs'
$backendPidFile = Join-Path $runtimeDir 'backend.pid'
$frontendPidFile = Join-Path $runtimeDir 'frontend.pid'
$panelUrl = "http://127.0.0.1:$Port/"
$docsUrl = "http://127.0.0.1:$Port/docs"
$viteUrl = "http://127.0.0.1:$($script:MtFrontendDevPort)/"
$healthTimeoutSec = 30

function Show-Urls {
    Write-Host ''
    Write-Host "  Painel:    $panelUrl" -ForegroundColor White
    Write-Host "  API docs:  $docsUrl" -ForegroundColor White
}

function Fail {
    param([string]$Message)
    Write-MtFail $Message
    exit 1
}

if ($Foreground -and $Dev) {
    Fail '-Foreground e -Dev não podem ser usados juntos. Inicie o Vite em outro terminal com -Dev, ou use só -Dev.'
}

Write-Host ''
Write-Host "Media Tool: iniciando em $root" -ForegroundColor White
Write-Host ''

# ---------------------------------------------------------------- Ambiente
Write-MtStep 'Validando o ambiente'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Fail "Python do projeto não encontrado ($python). Rode primeiro: .\scripts\setup.ps1"
}
# Sem aspas duplas: o Windows PowerShell 5.1 as remove ao passar argumentos para programas nativos.
$checkCode = @'
import importlib, sys
sys.path.insert(0, 'backend')
missing = []
for name in ('fastapi', 'uvicorn', 'app', 'media_processor', 'gallery_dl'):
    try:
        importlib.import_module(name)
    except Exception as exc:
        missing.append('%s (%s: %s)' % (name, type(exc).__name__, exc))
if missing:
    print('\n'.join(missing))
    sys.exit(1)
print('ok')
'@
Push-Location -LiteralPath $root
try {
    $check = Invoke-MtCaptured -FilePath $python -Arguments @('-c', $checkCode)
}
finally {
    Pop-Location
}
if ($check.ExitCode -ne 0) {
    Write-MtFail 'Pacotes Python não importáveis no .venv:'
    $check.Output | ForEach-Object { Write-MtInfo "  $_" }
    Fail 'Rode .\scripts\setup.ps1 e confira as mensagens acima.'
}
Write-MtOk 'pacotes importáveis: fastapi, uvicorn, app, media_processor, gallery_dl'

$distIndex = Get-MtPath 'frontend\dist\index.html'
if (Test-Path -LiteralPath $distIndex -PathType Leaf) {
    Write-MtOk 'frontend\dist encontrado'
}
elseif ($NoBuild) {
    Write-MtWarn 'frontend\dist não existe (-NoBuild): a API funciona, mas o painel em / não estará disponível.'
}
else {
    Write-MtStep 'frontend\dist não existe; gerando o build (npm run build)'
    $npm = Get-MtNpmCommand
    if ($null -eq $npm) { Fail 'npm não encontrado no PATH. Instale o Node.js e rode .\scripts\setup.ps1, ou use -NoBuild.' }
    $frontendDir = Get-MtPath 'frontend'
    if (-not (Test-Path -LiteralPath (Join-Path $frontendDir 'node_modules'))) {
        Fail 'frontend\node_modules não existe. Rode .\scripts\setup.ps1 (ou use -NoBuild).'
    }
    Push-Location -LiteralPath $frontendDir
    try { Invoke-MtNative -FilePath $npm -Arguments @('run', 'build') -Description 'npm run build' }
    finally { Pop-Location }
    if (-not (Test-Path -LiteralPath $distIndex -PathType Leaf)) { Fail 'O build terminou mas frontend\dist\index.html não existe.' }
    Write-MtOk 'frontend\dist gerado'
}

foreach ($d in @($runtimeDir, $logDir)) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

# ---------------------------------------------------------------- Porta
Write-MtStep "Verificando a porta $Port"
$backendAlreadyRunning = $false
$listeners = @(Get-MtListeningProcessIds -Port $Port)
if ($listeners.Count -gt 0) {
    $isOurs = $false
    foreach ($procId in $listeners) {
        if (Test-MtBackendCommandLine (Get-MtProcessCommandLine -ProcessId $procId)) { $isOurs = $true }
    }
    if (-not $isOurs -and (Test-MtBackendHealth -Port $Port)) { $isOurs = $true }
    if ($isOurs) {
        Write-MtOk "o Media Tool já está rodando na porta $Port (PID $($listeners -join ', '))."
        $backendAlreadyRunning = $true
    }
    else {
        $desc = foreach ($procId in $listeners) {
            $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($null -ne $p) { "$($p.ProcessName) (PID $procId)" } else { "PID $procId" }
        }
        Fail "A porta $Port está ocupada por outro processo: $($desc -join ', '). Encerre-o ou use -Port <outra porta>."
    }
}
else {
    Write-MtOk "porta $Port livre"
}

# ---------------------------------------------------------------- Backend
$uvicornArgs = @('-m', 'uvicorn', 'app.main:app', '--app-dir', 'backend', '--host', '127.0.0.1', '--port', "$Port", '--no-access-log')
# Ambiente do processo do backend (só do filho; o deste console é restaurado logo após o início):
# HOST/PORT coerentes com o uvicorn; MEDIA_TOOL_DEV=1 só com -Dev (libera a origem do Vite), removido sem -Dev;
# variáveis INSTAGRAM_* removidas, exceto INSTAGRAM_SESSIONID (fallback documentado da sessão).
$backendEnvSet = @{ MEDIA_TOOL_HOST = '127.0.0.1'; MEDIA_TOOL_PORT = "$Port" }
$backendEnvRemove = @()
if ($Dev) { $backendEnvSet['MEDIA_TOOL_DEV'] = '1' } else { $backendEnvRemove = @('MEDIA_TOOL_DEV') }

if ($Foreground) {
    if ($backendAlreadyRunning) {
        Show-Urls
        Write-Host ''
        exit 0
    }
    Write-MtStep 'Iniciando o backend em primeiro plano (Ctrl+C para parar)'
    $proc = $null
    $savedEnv = $null
    try {
        $savedEnv = Set-MtChildEnvironment -Set $backendEnvSet -Remove ($backendEnvRemove + @('MEDIA_TOOL_LOG_CONSOLE')) -RemovePrefix @('INSTAGRAM_') -KeepNames @('INSTAGRAM_SESSIONID')
        $proc = Start-Process -FilePath $python -ArgumentList $uvicornArgs -WorkingDirectory $root -NoNewWindow -PassThru
        $null = $proc.Handle
        Write-MtPidFile -Path $backendPidFile -ProcessId $proc.Id
        $deadline = (Get-Date).AddSeconds($healthTimeoutSec)
        $healthy = $false
        while ((Get-Date) -lt $deadline -and -not $proc.HasExited) {
            if (Test-MtBackendHealth -Port $Port) { $healthy = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if ($healthy) {
            Show-Urls
            Write-Host ''
            if ($Open) { Start-Process $panelUrl }
        }
        elseif (-not $proc.HasExited) {
            Write-MtWarn "o backend não respondeu em /api/health em $healthTimeoutSec s; confira as mensagens acima."
        }
        $proc.WaitForExit()
        $code = $proc.ExitCode
        if ($null -eq $code) { $code = 1 }
        exit $code
    }
    finally {
        if ($null -ne $proc -and -not $proc.HasExited) { Stop-MtProcessTree -ProcessId $proc.Id | Out-Null }
        Remove-MtPidFile -Path $backendPidFile
        Restore-MtChildEnvironment -Saved $savedEnv
    }
}

if (-not $backendAlreadyRunning) {
    Write-MtStep 'Iniciando o backend em segundo plano'
    $outLog = Join-Path $logDir 'backend.out.log'
    $errLog = Join-Path $logDir 'backend.err.log'
    $savedEnv = $null
    try {
        # Em segundo plano o log da aplicação vai só para logs\media-tool.log (sem cópia no stderr redirecionado).
        $bgEnvSet = $backendEnvSet.Clone()
        $bgEnvSet['MEDIA_TOOL_LOG_CONSOLE'] = '0'
        $savedEnv = Set-MtChildEnvironment -Set $bgEnvSet -Remove $backendEnvRemove -RemovePrefix @('INSTAGRAM_') -KeepNames @('INSTAGRAM_SESSIONID')
        $proc = Start-Process -FilePath $python -ArgumentList $uvicornArgs -WorkingDirectory $root `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog -WindowStyle Hidden -PassThru
    }
    finally {
        Restore-MtChildEnvironment -Saved $savedEnv
    }
    if ($Dev) { Write-MtInfo 'modo de desenvolvimento: MEDIA_TOOL_DEV=1 só no processo do backend' }
    $null = $proc.Handle
    Write-MtPidFile -Path $backendPidFile -ProcessId $proc.Id
    Write-MtInfo "PID $($proc.Id); log da aplicação em logs\media-tool.log (servidor: logs\backend.err.log)"

    Write-MtStep "Aguardando /api/health (até $healthTimeoutSec s)"
    $deadline = (Get-Date).AddSeconds($healthTimeoutSec)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) { break }
        if (Test-MtBackendHealth -Port $Port) { $healthy = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $healthy) {
        if ($proc.HasExited) {
            Write-MtFail "o backend encerrou durante a inicialização (código $($proc.ExitCode))."
        }
        else {
            Write-MtFail "o backend não respondeu em /api/health em $healthTimeoutSec s; encerrando o processo."
            Stop-MtProcessTree -ProcessId $proc.Id | Out-Null
        }
        Remove-MtPidFile -Path $backendPidFile
        Show-MtLogTail -Path $errLog -Lines 40
        exit 1
    }
    Write-MtOk 'backend respondendo'
}

# ---------------------------------------------------------------- Frontend dev (opcional)
if ($Dev) {
    Write-MtStep "Iniciando o frontend de desenvolvimento (Vite em $viteUrl)"
    if ($backendAlreadyRunning) {
        Write-MtWarn 'o backend já estava rodando: se ele foi iniciado sem -Dev, o painel do Vite não consegue fazer alterações (origem recusada). Nesse caso rode .\scripts\stop.ps1 e depois .\scripts\start.ps1 -Dev.'
    }
    $existing = Read-MtPidFile -Path $frontendPidFile
    if ($null -ne $existing -and (Test-MtProcessAlive -ProcessId $existing) -and
        (Test-MtFrontendDevCommandLine (Get-MtProcessCommandLine -ProcessId $existing))) {
        Write-MtOk "o Vite já está rodando (PID $existing)."
    }
    else {
        if ($null -ne $existing) { Remove-MtPidFile -Path $frontendPidFile }
        $viteListeners = @(Get-MtListeningProcessIds -Port $script:MtFrontendDevPort)
        if ($viteListeners.Count -gt 0) {
            Fail "A porta $($script:MtFrontendDevPort) está ocupada (PID $($viteListeners -join ', ')). O backend continua rodando; encerre o processo da porta ou rode .\scripts\stop.ps1."
        }
        $npm = Get-MtNpmCommand
        $frontendDir = Get-MtPath 'frontend'
        if ($null -eq $npm) { Fail 'npm não encontrado no PATH. O backend continua rodando.' }
        if (-not (Test-Path -LiteralPath (Join-Path $frontendDir 'node_modules'))) {
            Fail 'frontend\node_modules não existe. Rode .\scripts\setup.ps1. O backend continua rodando.'
        }
        $feOut = Join-Path $logDir 'frontend.out.log'
        $feErr = Join-Path $logDir 'frontend.err.log'
        $cmdArgs = @('/d', '/c', "npm.cmd run dev -- --host 127.0.0.1 --port $($script:MtFrontendDevPort) --strictPort")
        # O Vite recebe MEDIA_TOOL_PORT (alvo do proxy /api) e nenhuma variável INSTAGRAM_*.
        $savedFeEnv = $null
        try {
            $savedFeEnv = Set-MtChildEnvironment -Set @{ MEDIA_TOOL_PORT = "$Port" } -RemovePrefix @('INSTAGRAM_')
            $fe = Start-Process -FilePath $env:ComSpec -ArgumentList $cmdArgs -WorkingDirectory $frontendDir `
                -RedirectStandardOutput $feOut -RedirectStandardError $feErr -WindowStyle Hidden -PassThru
        }
        finally {
            Restore-MtChildEnvironment -Saved $savedFeEnv
        }
        $null = $fe.Handle
        Write-MtPidFile -Path $frontendPidFile -ProcessId $fe.Id
        Write-MtInfo "PID $($fe.Id); logs em logs\frontend.out.log e logs\frontend.err.log"
        $deadline = (Get-Date).AddSeconds($healthTimeoutSec)
        $viteOk = $false
        while ((Get-Date) -lt $deadline) {
            if ($fe.HasExited) { break }
            if ($null -ne (Invoke-MtHttpGet -Url $viteUrl)) { $viteOk = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $viteOk) {
            Write-MtFail "o Vite não respondeu em $viteUrl em $healthTimeoutSec s. O backend continua rodando."
            if (-not $fe.HasExited) { Stop-MtProcessTree -ProcessId $fe.Id | Out-Null }
            Remove-MtPidFile -Path $frontendPidFile
            Show-MtLogTail -Path $feOut -Lines 20
            Show-MtLogTail -Path $feErr -Lines 20
            exit 1
        }
        Write-MtOk 'Vite respondendo'
    }
}

# ---------------------------------------------------------------- URLs
Show-Urls
if ($Dev) { Write-Host "  Vite (dev): $viteUrl" -ForegroundColor White }
Write-Host ''
Write-MtInfo 'Para parar: powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1'
Write-Host ''
if ($Open) {
    if ($Dev) { Start-Process $viteUrl } else { Start-Process $panelUrl }
}
exit 0
