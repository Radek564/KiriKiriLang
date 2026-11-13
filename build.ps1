# Thin wrapper around langpatch.py.
#
#   .\build.ps1                       build the default profile
#   .\build.ps1 -Config <profile>     build one profile
#   .\build.ps1 -All                  build every profile in configs/
#   .\build.ps1 -Fresh                refresh the kit first, then build
#   .\build.ps1 -Pack                 only pack the kit into patch.xp3
param(
    [string]$Config = '',     # profile to build (default: config.json)
    [switch]$All,             # build every profile in configs/
    [switch]$Fresh,           # refresh the kit before building
    [switch]$Pack,            # pack the kit instead of building in place
    [switch]$Deploy           # install the patch into the game folder
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
Set-Location $here

function Invoke-Step([string]$Title, [string[]]$Arguments) {
    Write-Host ''
    Write-Host "=== $Title ==="
    & py -X utf8 @Arguments
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Get-Profiles {
    if ($All) {
        return (Get-ChildItem (Join-Path $here 'configs') -Filter '*.json' |
            Sort-Object Name | ForEach-Object { $_.FullName })
    }
    if ($Config) {
        return @($Config)
    }
    return @('config.json')
}

foreach ($profile in Get-Profiles) {
    if (-not (Test-Path $profile)) {
        Write-Host "profile not found: $profile"
        exit 2
    }
    $name = [System.IO.Path]::GetFileNameWithoutExtension($profile)

    if ($Fresh) {
        # The reproducible chain: the scenarios get the new slot, the kit is
        # refreshed (the captions are collected, the workbook is re-exported)
        # and the kit is written back.
        Invoke-Step "[$name] Adding the language slot to the scenarios" @(
            'langpatch.py', '--config', $profile, 'slot')
        Invoke-Step "[$name] Refreshing the kit" @(
            'langpatch.py', '--config', $profile, 'kit', 'create', '--force')
    }

    if ($Pack) {
        Invoke-Step "[$name] Packing the kit" @(
            'langpatch.py', '--config', $profile, 'pack')
    } else {
        Invoke-Step "[$name] Building the patch" @(
            'langpatch.py', '--config', $profile, 'build')
    }

    if ($Deploy) {
        Invoke-Step "[$name] Installing the patch" @(
            'langpatch.py', '--config', $profile, 'deploy', '-y')
    }
}

Write-Host ''
Write-Host 'Done.  The patch is in the profile''s build folder;'
Write-Host 'install it with:  langpatch.py --config <profile> deploy -y'
