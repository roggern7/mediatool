<#
.SYNOPSIS
    Salva, consulta ou remove a sessão local do Instagram do Media Tool.
.DESCRIPTION
    Sem parâmetros: pede o sessionid com entrada oculta (Read-Host -AsSecureString), envia ao CLI do backend
    somente pelo STDIN e grava o arquivo protegido com DPAPI (data\secrets\instagram-session.bin).
    O valor nunca é exibido, nunca vai para a linha de comando e a variável é descartada logo após o envio.
.PARAMETER Status
    Mostra se a sessão está configurada (sem exibir o valor).
.PARAMETER Clear
    Remove o arquivo protegido da sessão.
.EXAMPLE
    .\scripts\set-instagram-session.ps1
.EXAMPLE
    .\scripts\set-instagram-session.ps1 -Status
#>
[CmdletBinding(DefaultParameterSetName = 'Set')]
param(
    [Parameter(ParameterSetName = 'Status')][switch]$Status,
    [Parameter(ParameterSetName = 'Clear')][switch]$Clear
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$backend = Join-Path $root 'backend'
$module = 'app.integrations.instagram.session_cli'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Host "Python do projeto não encontrado em $python. Rode scripts\setup.ps1 primeiro." -ForegroundColor Red
    exit 1
}

function Invoke-SessionCli {
    param([string]$Command, [System.Security.SecureString]$Secret)

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $python
    $psi.Arguments = "-m $module $Command"
    $psi.WorkingDirectory = $backend
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = ($null -ne $Secret)

    $process = [System.Diagnostics.Process]::Start($psi)
    if ($null -ne $Secret) {
        $bstr = [IntPtr]::Zero
        $plain = $null
        try {
            $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secret)
            $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            $process.StandardInput.Write($plain)
            $process.StandardInput.Write("`n")
            $process.StandardInput.Flush()
        }
        finally {
            $plain = $null
            if ($bstr -ne [IntPtr]::Zero) {
                [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            }
            $process.StandardInput.Close()
        }
    }
    $process.WaitForExit()
    return $process.ExitCode
}

if ($Status) {
    exit (Invoke-SessionCli -Command 'status')
}

if ($Clear) {
    exit (Invoke-SessionCli -Command 'clear')
}

Write-Host 'Cole o valor do cookie sessionid do Instagram (a digitação não aparece na tela).'
$secure = Read-Host -Prompt 'sessionid' -AsSecureString
try {
    if ($null -eq $secure -or $secure.Length -eq 0) {
        Write-Host 'Nenhum valor informado. Nada foi alterado.' -ForegroundColor Yellow
        exit 2
    }
    $code = Invoke-SessionCli -Command 'set' -Secret $secure
}
finally {
    if ($null -ne $secure) { $secure.Dispose() }
    $secure = $null
    [System.GC]::Collect()
}
exit $code
