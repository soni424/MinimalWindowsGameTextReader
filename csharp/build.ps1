param([switch]$SkipTests)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$localSdk = Join-Path $env:USERPROFILE '.dotnet\dotnet.exe'
$sdk = if (Test-Path -LiteralPath $localSdk) { $localSdk } else { (Get-Command dotnet -ErrorAction Stop).Source }
Push-Location $projectRoot
try {
    & $sdk restore GameTextReader.slnx --locked-mode
    if ($LASTEXITCODE -ne 0) { throw 'Dependency restore failed.' }
    if (!$SkipTests) {
        & $sdk test GameTextReader.Tests/GameTextReader.Tests.csproj -c Release --no-restore --logger 'trx;LogFileName=csharp-release.trx' --results-directory "$projectRoot\TestResults"
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed; no package was published.' }
    }
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    [xml]$buildProperties = Get-Content -LiteralPath (Join-Path $projectRoot 'Directory.Build.props')
    $version = $buildProperties.Project.PropertyGroup.Version
    $revision = (& git rev-parse --short HEAD).Trim()
    if (& git status --porcelain) { $revision += '-dirty' }
    $build = "$stamp-$revision"
    # Keep native WPF/WebView library probing below legacy Windows path limits.
    # The archive and frozen metadata still carry the complete version/build.
    $releaseRoot = Join-Path $projectRoot "artifacts\release\$stamp"
    $publish = Join-Path $releaseRoot 'GameTextReader'
    & $sdk publish GameTextReader.App/GameTextReader.App.csproj -c Release -r win-x64 --self-contained true -p:RestoreLockedMode=true -p:PublishSingleFile=false -p:DebugType=None -p:DebugSymbols=false -o $publish
    if ($LASTEXITCODE -ne 0) { throw 'Publishing failed.' }
    [IO.File]::WriteAllText((Join-Path $publish 'build_info.json'), (@{version=$version;build=$build;framework='.NET 10 / WPF';runtime='win-x64';selfContained=$true} | ConvertTo-Json))
    Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination (Join-Path $publish 'README-CSharp.md')
    Copy-Item -LiteralPath (Join-Path $projectRoot 'VALIDATION.md') -Destination (Join-Path $publish 'VALIDATION.md')
    Copy-Item -LiteralPath (Join-Path $projectRoot 'MANUAL_TEST_CHECKLIST.md') -Destination (Join-Path $publish 'MANUAL_TEST_CHECKLIST.md')
    Copy-Item -LiteralPath (Join-Path $projectRoot '..\README.md') -Destination (Join-Path $publish 'README.md')
    Copy-Item -LiteralPath (Join-Path $projectRoot '..\RELEASE_NOTES.md') -Destination (Join-Path $publish 'RELEASE_NOTES.md')
    $forbidden = Get-ChildItem -LiteralPath $publish -Recurse -File | Where-Object { $_.Name -eq 'config.json' -or $_.Extension -in '.py','.pyc','.pdb' -or $_.Name -like 'python*.dll' }
    if ($forbidden) { throw 'The publish directory contains personal settings, Python files or debug symbols.' }
    if (!(Test-Path -LiteralPath (Join-Path $publish 'coreclr.dll'))) { throw 'The self-contained runtime was not published.' }
    $zip = Join-Path $projectRoot "artifacts\GameTextReader-$version-$build-win-x64.zip"
    Compress-Archive -LiteralPath $publish -DestinationPath $zip -CompressionLevel Optimal
    Get-FileHash -LiteralPath $zip -Algorithm SHA256 | Format-List
    Write-Output "Version: $version"
    Write-Output "Build: $build"
    Write-Output "Executable: $publish\GameTextReader.exe"
    Write-Output "ZIP: $zip"
} finally { Pop-Location }
