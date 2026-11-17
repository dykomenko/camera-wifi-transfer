param(
    [string]$Task = "assembleRelease",
    [string]$Tools = "C:\Users\dykomenko\AndroidBuild"
)

# Builds the Android app with the portable toolchain in $Tools (JDK 17,
# Android SDK, Gradle 8.2.1) and writes the full log next to this script.
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$env:JAVA_HOME = Join-Path $Tools "jdk-17.0.21.0.2+1"
$env:ANDROID_HOME = Join-Path $Tools "sdk"
$env:GRADLE_USER_HOME = Join-Path $Tools "gradle-home"
$Gradle = Join-Path $Tools "gradle-8.2.1\bin\gradle.bat"
$Log = Join-Path $Root "build-log.txt"

Push-Location $Root
try {
    "sdk.dir=$($env:ANDROID_HOME -replace '\\', '/')" | Set-Content -Encoding ascii local.properties
    & $Gradle --no-daemon --console=plain $Task *>&1 | Tee-Object -FilePath $Log
    $code = $LASTEXITCODE
    "BUILD EXIT CODE: $code" | Tee-Object -FilePath $Log -Append
    if ($code -eq 0 -and $Task -like "assemble*") {
        Get-ChildItem -Recurse -Filter *.apk (Join-Path $Root "app\build\outputs\apk") |
            Select-Object FullName, Length | Format-Table -AutoSize | Out-String |
            Tee-Object -FilePath $Log -Append
    }
}
finally {
    Pop-Location
}
