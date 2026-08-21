param([string]$Python = "python")

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BuildRoot = Join-Path $ProjectRoot "build-output"
$Artifacts = Join-Path $ProjectRoot "artifacts"
$PackageName = "Camera-WiFi-Transfer-0.3.2-beta"
$Package = Join-Path $BuildRoot $PackageName

Push-Location $ProjectRoot
try {
    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "Tests failed" }

    & $Python -m PyInstaller --noconfirm --clean --distpath (Join-Path $BuildRoot "backend-dist") --workpath (Join-Path $BuildRoot "backend-work") packaging\airmtp-backend.spec
    if ($LASTEXITCODE -ne 0) { throw "Backend build failed" }

    & $Python -m PyInstaller --noconfirm --clean --distpath (Join-Path $BuildRoot "gui-dist") --workpath (Join-Path $BuildRoot "gui-work") packaging\camera-wifi-transfer.spec
    if ($LASTEXITCODE -ne 0) { throw "GUI build failed" }

    New-Item -ItemType Directory -Path $Package -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $BuildRoot "gui-dist\Camera WiFi Transfer.exe") -Destination $Package -Force
    Copy-Item -LiteralPath (Join-Path $BuildRoot "backend-dist\airmtp-backend.exe") -Destination $Package -Force
    Copy-Item -LiteralPath LICENSE, README.md, README_RU.md, THIRD_PARTY_NOTICES.md, CHANGELOG.md -Destination $Package -Force
    if (Test-Path -LiteralPath docs) {
        Copy-Item -LiteralPath docs -Destination $Package -Recurse -Force
    }

    $SourcePackage = Join-Path $Package "source"
    New-Item -ItemType Directory -Path $SourcePackage -Force | Out-Null
    Copy-Item -LiteralPath app.py, backend_output.py, localization.py, sony_soap.py, windows_pairing.py -Destination $SourcePackage -Force
    Copy-Item -LiteralPath tests, vendor, packaging, scripts -Destination $SourcePackage -Recurse -Force
    $PrivateBackendData = Join-Path $SourcePackage "vendor\airmtp\appdata"
    if (Test-Path -LiteralPath $PrivateBackendData) {
        Remove-Item -LiteralPath $PrivateBackendData -Recurse -Force
    }
    $ResolvedSourcePackage = (Resolve-Path -LiteralPath $SourcePackage).Path
    Get-ChildItem -LiteralPath $SourcePackage -Directory -Recurse -Filter "__pycache__" | ForEach-Object {
        if (-not $_.FullName.StartsWith($ResolvedSourcePackage + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove cache outside package: $($_.FullName)"
        }
        Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }

    $HashLines = foreach ($FileName in @("Camera WiFi Transfer.exe", "airmtp-backend.exe")) {
        $Hash = Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $Package $FileName)
        "{0}  {1}" -f $Hash.Hash, $FileName
    }
    Set-Content -LiteralPath (Join-Path $Package "SHA256SUMS.txt") -Value $HashLines -Encoding ascii

    New-Item -ItemType Directory -Path $Artifacts -Force | Out-Null
    $Archive = Join-Path $Artifacts ($PackageName + ".zip")
    Compress-Archive -LiteralPath $Package -DestinationPath $Archive -CompressionLevel Optimal -Force
    Write-Host "Built $Archive"
}
finally {
    Pop-Location
}
