param(
  [string]$ArtifactsRoot = (Join-Path $PSScriptRoot '..\browser-local\model-artifacts'),
  [string]$OutputPath = (Join-Path $PSScriptRoot '..\browser-local\model-manifest.json')
)

$resolvedArtifacts = Resolve-Path -LiteralPath $ArtifactsRoot -ErrorAction SilentlyContinue
$artifacts = @()
if ($resolvedArtifacts) {
  $artifacts = @(Get-ChildItem -LiteralPath $resolvedArtifacts.Path -File -Recurse | ForEach-Object {
    $hash = Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256
    [ordered]@{
      path = $_.FullName.Substring($resolvedArtifacts.Path.Length).TrimStart('\', '/')
      bytes = $_.Length
      sha256 = $hash.Hash.ToLowerInvariant()
    }
  })
}

$manifest = [ordered]@{
  schema = 1
  version = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK')
  model = 'onnx-community/Qwen2.5-0.5B-Instruct-ONNX'
  runtime = '@huggingface/transformers@3.7.2'
  artifacts = $artifacts
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutputPath -Encoding utf8
Write-Host "Manifesto escrito em $OutputPath com $($artifacts.Count) artefacto(s)."
