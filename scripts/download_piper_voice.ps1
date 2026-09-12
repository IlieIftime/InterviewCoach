$ErrorActionPreference = "Stop"

$voiceDirectory = Join-Path $PSScriptRoot "..\models\piper"
New-Item -ItemType Directory -Force -Path $voiceDirectory | Out-Null

$baseUrl = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/pt/pt_PT/tug%C3%A3o/medium"
$files = @(
    @{ Name = "pt_PT-tugao-medium.onnx"; Remote = "pt_PT-tug%C3%A3o-medium.onnx?download=true" },
    @{ Name = "pt_PT-tugao-medium.onnx.json"; Remote = "pt_PT-tug%C3%A3o-medium.onnx.json?download=true" }
)

foreach ($file in $files) {
    $target = Join-Path $voiceDirectory $file.Name
    if (-not (Test-Path -LiteralPath $target)) {
        Invoke-WebRequest -Uri "$baseUrl/$($file.Remote)" -OutFile $target
    }
}

Get-ChildItem -LiteralPath $voiceDirectory | Select-Object Name, Length
