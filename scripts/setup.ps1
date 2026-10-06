<#
.SYNOPSIS
    Prepara o ambiente local do Media Tool (Python, frontend, pastas, FFmpeg).

.DESCRIPTION
    - Verifica Python 3.11+; cria .venv se não existir (se existir, reusa).
    - Instala backend/requirements.txt e o processor (editable) no .venv.
    - Garante o gallery-dl instalado, sem reinstalar se já estiver: da cópia local (./gallery-dl, editable) se
      existir; senão, a versão fixada do PyPI.
    - Verifica Node/npm, instala dependências do frontend e gera frontend/dist.
    - Cria data, data/secrets, logs e storage/{originals,processed,temp,thumbs}.
    - Verifica FFmpeg/ffprobe (apenas avisa; não instala nada).
    Nada é instalado globalmente e nenhuma configuração do sistema é alterada.

.PARAMETER SkipPython
    Pula as etapas de Python (venv e pacotes).

.PARAMETER SkipFrontend
    Pula as etapas de Node/npm e o build do frontend.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipPython,
    [switch]$SkipFrontend
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'lib\common.ps1')

$root = Get-MtRoot
$summary = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]

function Get-PythonVersion {
    param([Parameter(Mandatory)][string]$Exe, [string[]]$PrefixArgs = @())
    $r = Invoke-MtCaptured -FilePath $Exe -Arguments ($PrefixArgs + @('-c', 'import sys; print(''%d.%d.%d'' % sys.version_info[:3])'))
    if ($r.ExitCode -ne 0 -or $r.Output.Count -eq 0) { return $null }
    $text = ($r.Output | Select-Object -Last 1).Trim()
    if ($text -match '^\d+\.\d+\.\d+$') { return [version]$text }
    return $null
}

function Find-BasePython {
    # Procura um Python 3.11+ para criar o .venv. Retorna @{ Exe; PrefixArgs; Version } ou $null.
    $candidates = @()
    $py = Get-Command 'py.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $py) {
        $candidates += , @{ Exe = $py.Source; PrefixArgs = @('-3.13') }
        $candidates += , @{ Exe = $py.Source; PrefixArgs = @('-3') }
    }
    foreach ($name in @('python.exe', 'python3.exe')) {
        foreach ($cmd in @(Get-Command $name -CommandType Application -ErrorAction SilentlyContinue)) {
            $candidates += , @{ Exe = $cmd.Source; PrefixArgs = @() }
        }
    }
    foreach ($c in $candidates) {
        $v = Get-PythonVersion -Exe $c.Exe -PrefixArgs $c.PrefixArgs
        if ($null -ne $v -and $v -ge [version]'3.11.0') {
            return @{ Exe = $c.Exe; PrefixArgs = $c.PrefixArgs; Version = $v }
        }
    }
    return $null
}

Write-Host ''
Write-Host "Media Tool: setup em $root" -ForegroundColor White
Write-Host ''

# ---------------------------------------------------------------- Pastas
Write-MtStep 'Criando pastas locais'
foreach ($rel in @('data', 'data\secrets', 'data\.runtime', 'logs', 'storage\originals', 'storage\processed', 'storage\temp', 'storage\thumbs')) {
    $p = Get-MtPath $rel
    if (-not (Test-Path -LiteralPath $p)) {
        New-Item -ItemType Directory -Path $p -Force | Out-Null
        Write-MtOk "criada: $rel"
    }
    else {
        Write-MtInfo "já existe: $rel"
    }
}
$summary.Add('Pastas data, data\secrets, logs e storage\* prontas.')

# ---------------------------------------------------------------- Python
$venvPython = Get-MtVenvPython
if ($SkipPython) {
    Write-MtStep 'Python: etapa ignorada (-SkipPython)'
    $summary.Add('Python: ignorado (-SkipPython).')
}
else {
    Write-MtStep 'Verificando Python e o ambiente virtual (.venv)'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $v = Get-PythonVersion -Exe $venvPython
        if ($null -eq $v) {
            throw "O .venv existe mas o Python dele não executa: $venvPython. Apague a pasta .venv manualmente e rode o setup de novo."
        }
        if ($v -lt [version]'3.11.0') {
            throw "O .venv usa Python $v; o Media Tool exige 3.11+. Apague a pasta .venv manualmente e rode o setup de novo com Python 3.13 instalado."
        }
        Write-MtOk "reusando .venv existente (Python $v)"
    }
    else {
        $base = Find-BasePython
        if ($null -eq $base) {
            throw 'Python 3.11+ não encontrado. Instale o Python 3.13 (https://www.python.org/downloads/ ou "winget install Python.Python.3.13") e rode o setup de novo.'
        }
        Write-MtInfo "criando .venv com Python $($base.Version) ($($base.Exe) $($base.PrefixArgs -join ' '))"
        Invoke-MtNative -FilePath $base.Exe -Arguments ($base.PrefixArgs + @('-m', 'venv', (Get-MtPath '.venv'))) -Description 'Criação do .venv'
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            throw "O .venv foi criado mas $venvPython não existe."
        }
        $v = Get-PythonVersion -Exe $venvPython
        Write-MtOk ".venv criado (Python $v)"
    }

    Write-MtStep 'Instalando dependências do backend (backend\requirements.txt)'
    $req = Get-MtPath 'backend\requirements.txt'
    if (-not (Test-Path -LiteralPath $req -PathType Leaf)) {
        throw "Arquivo não encontrado: $req"
    }
    Invoke-MtNative -FilePath $venvPython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $req) -Description 'pip install -r backend\requirements.txt'
    Write-MtOk 'dependências do backend instaladas'

    Write-MtStep 'Instalando o processor (media_processor) em modo editable'
    $procProject = Get-MtPath 'processor\pyproject.toml'
    if (Test-Path -LiteralPath $procProject -PathType Leaf) {
        Invoke-MtNative -FilePath $venvPython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '-e', (Get-MtPath 'processor')) -Description 'pip install -e processor'
        Write-MtOk 'processor instalado (editable)'
    }
    else {
        Write-MtWarn 'processor\pyproject.toml não encontrado; instalação do processor ignorada.'
        $warnings.Add('processor\pyproject.toml ausente: media_processor não foi instalado.')
    }

    Write-MtStep 'Verificando o gallery-dl (método legado de importação)'
    $gdl = Invoke-MtCaptured -FilePath $venvPython -Arguments @('-c', 'import gallery_dl, gallery_dl.version as v; print(v.__version__)')
    if ($gdl.ExitCode -eq 0) {
        Write-MtOk "gallery-dl já instalado no .venv (versão $(($gdl.Output | Select-Object -Last 1).Trim())); não será reinstalado"
    }
    else {
        $gdlDir = Get-MtPath 'gallery-dl'
        if (-not (Test-Path -LiteralPath (Join-Path $gdlDir 'setup.py')) -and -not (Test-Path -LiteralPath (Join-Path $gdlDir 'pyproject.toml'))) {
            # Máquina nova, sem a cópia local do upstream: instala a mesma versão a partir do PyPI.
            Invoke-MtNative -FilePath $venvPython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', 'gallery-dl==1.32.14') -Description 'pip install gallery-dl==1.32.14'
            Write-MtOk 'gallery-dl 1.32.14 instalado a partir do PyPI'
        }
        else {
            Invoke-MtNative -FilePath $venvPython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '-e', $gdlDir) -Description 'pip install -e gallery-dl'
            Write-MtOk 'gallery-dl instalado (editable) a partir de ./gallery-dl'
        }
    }
    $summary.Add("Python: .venv pronto ($venvPython).")
}

# ---------------------------------------------------------------- Frontend
if ($SkipFrontend) {
    Write-MtStep 'Frontend: etapa ignorada (-SkipFrontend)'
    $summary.Add('Frontend: ignorado (-SkipFrontend).')
}
else {
    Write-MtStep 'Verificando Node.js e npm'
    $node = Get-Command 'node.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $npm = Get-MtNpmCommand
    if ($null -eq $node -or $null -eq $npm) {
        throw 'Node.js/npm não encontrados no PATH. Instale o Node.js 24 LTS (https://nodejs.org/ ou "winget install OpenJS.NodeJS.LTS") e rode o setup de novo.'
    }
    $nodeVer = (Invoke-MtCaptured -FilePath $node.Source -Arguments @('--version')).Output -join ''
    $npmVer = (Invoke-MtCaptured -FilePath $npm -Arguments @('--version')).Output -join ''
    Write-MtOk "node $nodeVer, npm $npmVer"

    $frontendDir = Get-MtPath 'frontend'
    if (-not (Test-Path -LiteralPath (Join-Path $frontendDir 'package.json') -PathType Leaf)) {
        throw "frontend\package.json não encontrado em $frontendDir."
    }
    Push-Location -LiteralPath $frontendDir
    try {
        if (Test-Path -LiteralPath (Join-Path $frontendDir 'package-lock.json') -PathType Leaf) {
            Write-MtStep 'Instalando dependências do frontend (npm ci)'
            Invoke-MtNative -FilePath $npm -Arguments @('ci') -Description 'npm ci'
        }
        else {
            Write-MtStep 'Instalando dependências do frontend (npm install)'
            Invoke-MtNative -FilePath $npm -Arguments @('install') -Description 'npm install'
        }
        Write-MtStep 'Gerando o build do frontend (npm run build)'
        Invoke-MtNative -FilePath $npm -Arguments @('run', 'build') -Description 'npm run build'
    }
    finally {
        Pop-Location
    }
    if (-not (Test-Path -LiteralPath (Join-Path $frontendDir 'dist\index.html') -PathType Leaf)) {
        throw 'O build terminou mas frontend\dist\index.html não existe.'
    }
    Write-MtOk 'frontend\dist gerado'
    $summary.Add('Frontend: dependências instaladas e frontend\dist gerado.')
}

# ---------------------------------------------------------------- FFmpeg
Write-MtStep 'Verificando FFmpeg e ffprobe'
foreach ($tool in @(@{ Name = 'ffmpeg'; Env = 'MEDIA_TOOL_FFMPEG' }, @{ Name = 'ffprobe'; Env = 'MEDIA_TOOL_FFPROBE' })) {
    $path = $null
    $fromEnv = [Environment]::GetEnvironmentVariable($tool.Env)
    if (-not [string]::IsNullOrWhiteSpace($fromEnv)) {
        if (Test-Path -LiteralPath $fromEnv -PathType Leaf) { $path = $fromEnv }
        else { Write-MtWarn "$($tool.Env) aponta para um arquivo inexistente: $fromEnv" }
    }
    if ($null -eq $path) {
        $cmd = Get-Command "$($tool.Name).exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $cmd) { $path = $cmd.Source }
    }
    if ($null -ne $path) {
        $first = ((Invoke-MtCaptured -FilePath $path -Arguments @('-version')).Output | Select-Object -First 1)
        Write-MtOk "$($tool.Name): $path"
        if ($first) { Write-MtInfo "$first" }
        $summary.Add("$($tool.Name): $path")
    }
    else {
        Write-MtWarn "$($tool.Name) não encontrado. O processamento de mídia não funcionará sem ele."
        Write-MtInfo 'Para instalar (comando sugerido, rode você mesmo): winget install Gyan.FFmpeg'
        Write-MtInfo "Depois abra um novo terminal, ou defina $($tool.Env) no .env com o caminho completo do executável."
        $warnings.Add("$($tool.Name) ausente: instale com 'winget install Gyan.FFmpeg' ou defina $($tool.Env).")
    }
}

# ---------------------------------------------------------------- Resumo
Write-Host ''
Write-Host 'Resumo do setup' -ForegroundColor White
foreach ($s in $summary) { Write-MtInfo "- $s" }
if ($warnings.Count -gt 0) {
    Write-Host ''
    Write-Host 'Avisos' -ForegroundColor Yellow
    foreach ($w in $warnings) { Write-MtWarn $w }
}
Write-Host ''
Write-Host 'Próximos passos' -ForegroundColor White
Write-MtInfo '1. Inicie:  powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1 -Open'
Write-MtInfo '2. Abra o painel em http://127.0.0.1:8765/ e configure a sessão do Instagram em Configurações'
Write-MtInfo '   (ou rode .\scripts\set-instagram-session.ps1).'
Write-MtInfo '3. Para parar: powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1'
Write-Host ''
exit 0
