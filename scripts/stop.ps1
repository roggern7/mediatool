<#
.SYNOPSIS
    Para o Media Tool (backend e frontend de desenvolvimento).

.DESCRIPTION
    Encerra os processos registrados em data\.runtime\backend.pid e data\.runtime\frontend.pid, incluindo
    processos filhos (taskkill /T /F), depois de confirmar pela linha de comando que são do Media Tool.
    Fallback: procura o processo que escuta na porta do backend e só o encerra se a linha de comando for
    "uvicorn app.main:app". Remove os PID files. Rodar sem nada em execução não é erro.

.PARAMETER Port
    Porta do backend usada no fallback (padrão 8765).
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 8765
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'lib\common.ps1')

$runtimeDir = Get-MtRuntimeDir
$failures = 0

function Stop-FromPidFile {
    param([string]$Name, [string]$PidFile, [scriptblock]$Matcher)
    $procId = Read-MtPidFile -Path $PidFile
    if ($null -eq $procId) {
        if (Test-Path -LiteralPath $PidFile) {
            Write-MtWarn "$Name`: PID file inválido; removendo."
            Remove-MtPidFile -Path $PidFile
        }
        else {
            Write-MtInfo "$Name`: nenhum PID file."
        }
        return $true
    }
    if (-not (Test-MtProcessAlive -ProcessId $procId)) {
        Write-MtInfo "$Name`: PID $procId não está mais em execução; removendo PID file."
        Remove-MtPidFile -Path $PidFile
        return $true
    }
    $cmdLine = Get-MtProcessCommandLine -ProcessId $procId
    if (-not (& $Matcher $cmdLine)) {
        Write-MtWarn "$Name`: o PID $procId pertence a outro processo (PID file obsoleto); ele NÃO será encerrado. Removendo PID file."
        Remove-MtPidFile -Path $PidFile
        return $true
    }
    if (Stop-MtProcessTree -ProcessId $procId) {
        Write-MtOk "$Name`: processo $procId e filhos encerrados."
        Remove-MtPidFile -Path $PidFile
        return $true
    }
    Write-MtFail "$Name`: não foi possível encerrar o PID $procId."
    return $false
}

Write-Host ''
Write-Host 'Media Tool: parando' -ForegroundColor White
Write-Host ''

Write-MtStep 'Frontend de desenvolvimento (Vite)'
if (-not (Stop-FromPidFile -Name 'frontend' -PidFile (Join-Path $runtimeDir 'frontend.pid') -Matcher { param($c) Test-MtFrontendDevCommandLine $c })) {
    $failures++
}

Write-MtStep 'Backend'
if (-not (Stop-FromPidFile -Name 'backend' -PidFile (Join-Path $runtimeDir 'backend.pid') -Matcher { param($c) Test-MtBackendCommandLine $c })) {
    $failures++
}

Write-MtStep "Verificando a porta $Port"
$listeners = @(Get-MtListeningProcessIds -Port $Port)
if ($listeners.Count -eq 0) {
    Write-MtOk "porta $Port livre"
}
else {
    foreach ($procId in $listeners) {
        $cmdLine = Get-MtProcessCommandLine -ProcessId $procId
        if (Test-MtBackendCommandLine $cmdLine) {
            if (Stop-MtProcessTree -ProcessId $procId) {
                Write-MtOk "backend sem PID file (PID $procId, uvicorn app.main:app) encerrado."
            }
            else {
                Write-MtFail "não foi possível encerrar o PID $procId."
                $failures++
            }
        }
        else {
            $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
            $name = if ($null -ne $p) { $p.ProcessName } else { '?' }
            Write-MtWarn "a porta $Port está em uso por outro processo ($name, PID $procId), que não é o Media Tool; ele NÃO foi encerrado."
        }
    }
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        $still = @(Get-MtListeningProcessIds -Port $Port | Where-Object { Test-MtBackendCommandLine (Get-MtProcessCommandLine -ProcessId $_) })
        if ($still.Count -eq 0) { break }
        Start-Sleep -Milliseconds 300
    }
}

Write-Host ''
if ($failures -gt 0) {
    Write-MtFail "$failures processo(s) não puderam ser encerrados."
    exit 1
}
Write-MtOk 'Media Tool parado.'
Write-Host ''
exit 0
