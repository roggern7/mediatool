# Funções comuns aos scripts do Media Tool (setup, start, stop).
# Compatível com Windows PowerShell 5.1 e PowerShell 7. Carregar com dot-source:
#   . (Join-Path $PSScriptRoot 'lib\common.ps1')

Set-StrictMode -Version Latest

$script:MtRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script:MtDefaultPort = 8765
$script:MtFrontendDevPort = 5173

function Get-MtRoot { return $script:MtRoot }

function Get-MtPath {
    param([Parameter(Mandatory)][string]$Relative)
    return (Join-Path $script:MtRoot $Relative)
}

function Get-MtVenvPython { return (Get-MtPath '.venv\Scripts\python.exe') }

function Get-MtRuntimeDir { return (Get-MtPath 'data\.runtime') }

function Write-MtStep { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-MtOk { param([string]$Message) Write-Host "  [ok] $Message" -ForegroundColor Green }
function Write-MtInfo { param([string]$Message) Write-Host "  $Message" }
function Write-MtWarn { param([string]$Message) Write-Host "  [aviso] $Message" -ForegroundColor Yellow }
function Write-MtFail { param([string]$Message) Write-Host "  [erro] $Message" -ForegroundColor Red }

# Executa um programa nativo capturando stdout+stderr como texto, sem que o stderr vire exceção
# no Windows PowerShell 5.1. Retorna ExitCode e Output (string[]).
function Invoke-MtCaptured {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = @(& $FilePath @Arguments 2>&1 | ForEach-Object { "$_" })
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prev
    }
    return [pscustomobject]@{ ExitCode = $code; Output = $out }
}

# Executa um programa nativo mostrando a saída no console; lança erro se o código de saída for != 0.
function Invoke-MtNative {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory)][string]$Description
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $FilePath @Arguments
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) {
        throw "$Description falhou (código de saída $code)."
    }
}

function Get-MtNpmCommand {
    # Usa npm.cmd para não depender da política de execução do npm.ps1.
    $cmd = Get-Command 'npm.cmd' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $cmd) { return $null }
    return $cmd.Source
}

function Get-MtProcessCommandLine {
    param([Parameter(Mandatory)][int]$ProcessId)
    $p = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $p) { return $null }
    return [string]$p.CommandLine
}

function Test-MtBackendCommandLine {
    param([string]$CommandLine)
    if ([string]::IsNullOrEmpty($CommandLine)) { return $false }
    return ($CommandLine -match 'uvicorn' -and $CommandLine -match 'app\.main:app')
}

function Test-MtFrontendDevCommandLine {
    param([string]$CommandLine)
    if ([string]::IsNullOrEmpty($CommandLine)) { return $false }
    return ($CommandLine -match 'npm' -and $CommandLine -match 'run\s+dev') -or ($CommandLine -match 'vite')
}

function Test-MtProcessAlive {
    param([Parameter(Mandatory)][int]$ProcessId)
    return ($null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue))
}

# PIDs que estão escutando na porta TCP local informada. Chamar como @(Get-MtListeningProcessIds -Port n).
function Get-MtListeningProcessIds {
    param([Parameter(Mandatory)][int]$Port)
    $conns = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    $ids = @()
    foreach ($c in $conns) {
        $owner = [int]$c.OwningProcess
        if ($owner -gt 0 -and $ids -notcontains $owner) { $ids += $owner }
    }
    return $ids
}

# Encerra o processo e todos os filhos (taskkill /T /F). Retorna $true se o processo não existe mais.
function Stop-MtProcessTree {
    param([Parameter(Mandatory)][int]$ProcessId)
    if (-not (Test-MtProcessAlive -ProcessId $ProcessId)) { return $true }
    $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
    $r = Invoke-MtCaptured -FilePath $taskkill -Arguments @('/PID', "$ProcessId", '/T', '/F')
    if ($r.ExitCode -ne 0 -and (Test-MtProcessAlive -ProcessId $ProcessId)) {
        # Último recurso para o processo principal.
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-MtProcessAlive -ProcessId $ProcessId)) { return $true }
        Start-Sleep -Milliseconds 200
    }
    return (-not (Test-MtProcessAlive -ProcessId $ProcessId))
}

function Read-MtPidFile {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $raw = (Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue)
    if ($null -eq $raw) { return $null }
    $value = 0
    if ([int]::TryParse($raw.Trim(), [ref]$value) -and $value -gt 0) { return $value }
    return $null
}

function Write-MtPidFile {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][int]$ProcessId)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    [System.IO.File]::WriteAllText($Path, "$ProcessId")
}

function Remove-MtPidFile {
    param([Parameter(Mandatory)][string]$Path)
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force }
}

# GET simples sem proxy. Retorna o corpo (string) se HTTP 2xx, senão $null.
function Invoke-MtHttpGet {
    param([Parameter(Mandatory)][string]$Url, [int]$TimeoutMs = 2000)
    try {
        $req = [System.Net.HttpWebRequest][System.Net.WebRequest]::Create($Url)
        $req.Method = 'GET'
        $req.Proxy = $null
        $req.Timeout = $TimeoutMs
        $req.ReadWriteTimeout = $TimeoutMs
        $resp = $req.GetResponse()
        try {
            $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
            try { return $reader.ReadToEnd() } finally { $reader.Dispose() }
        }
        finally { $resp.Close() }
    }
    catch {
        return $null
    }
}

# Verdadeiro se /api/health responde com status "ok".
function Test-MtBackendHealth {
    param([Parameter(Mandatory)][int]$Port)
    $body = Invoke-MtHttpGet -Url "http://127.0.0.1:$Port/api/health"
    if ($null -eq $body) { return $false }
    try {
        $json = $body | ConvertFrom-Json
        return ($json.PSObject.Properties.Name -contains 'status' -and $json.status -eq 'ok')
    }
    catch {
        return $false
    }
}

# Ajusta temporariamente o ambiente deste processo para que um filho iniciado em seguida o herde.
# Set: variáveis a definir; Remove: nomes exatos a remover; RemovePrefix: prefixos a remover (exceto KeepNames).
# Retorna os valores originais para Restore-MtChildEnvironment (sempre chamar em finally).
function Set-MtChildEnvironment {
    param(
        [hashtable]$Set = @{},
        [string[]]$Remove = @(),
        [string[]]$RemovePrefix = @(),
        [string[]]$KeepNames = @()
    )
    $saved = @{}
    $toRemove = New-Object System.Collections.Generic.List[string]
    foreach ($name in $Remove) { $toRemove.Add($name) }
    foreach ($item in @(Get-ChildItem Env:)) {
        foreach ($prefix in $RemovePrefix) {
            if ($item.Name.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) -and $KeepNames -notcontains $item.Name) {
                $toRemove.Add($item.Name)
            }
        }
    }
    foreach ($name in $toRemove) {
        if (-not $saved.ContainsKey($name)) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    foreach ($name in $Set.Keys) {
        if (-not $saved.ContainsKey($name)) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
        [Environment]::SetEnvironmentVariable($name, [string]$Set[$name], 'Process')
    }
    return $saved
}

function Restore-MtChildEnvironment {
    param([hashtable]$Saved)
    if ($null -eq $Saved) { return }
    foreach ($name in $Saved.Keys) {
        [Environment]::SetEnvironmentVariable($name, $Saved[$name], 'Process')
    }
}

function Show-MtLogTail {
    param([Parameter(Mandatory)][string]$Path, [int]$Lines = 40)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Write-Host "---- últimas $Lines linhas de $Path ----" -ForegroundColor DarkGray
        Get-Content -LiteralPath $Path -Tail $Lines | ForEach-Object { Write-Host "  $_" }
        Write-Host '---- fim do log ----' -ForegroundColor DarkGray
    }
    else {
        Write-MtInfo "(log $Path não existe)"
    }
}
