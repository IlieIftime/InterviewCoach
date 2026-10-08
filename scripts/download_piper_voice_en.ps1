$ErrorActionPreference = "Stop"

$voiceDirectory = Join-Path $PSScriptRoot "..\models\piper"
New-Item -ItemType Directory -Force -Path $voiceDirectory | Out-Null

$baseUrl = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium"
$files = @(
    @{ Name = "en_US-lessac-medium.onnx"; Remote = "en_US-lessac-medium.onnx?download=true" },
    @{ Name = "en_US-lessac-medium.onnx.json"; Remote = "en_US-lessac-medium.onnx.json?download=true" }
)

foreach ($file in $files) {
    $target = Join-Path $voiceDirectory $file.Name
    if (-not (Test-Path -LiteralPath $target)) {
        Invoke-WebRequest -Uri "$baseUrl/$($file.Remote)" -OutFile $target
    }
}

Get-ChildItem -LiteralPath $voiceDirectory | Select-Object Name, Length
