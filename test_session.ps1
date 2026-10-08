$ErrorActionPreference = "Stop"

function Invoke-JsonPost($Uri, $Payload) {
    $json = $Payload | ConvertTo-Json -Depth 10
    $utf8 = [System.Text.Encoding]::UTF8.GetBytes($json)
    Invoke-RestMethod -Method Post -Uri $Uri -ContentType "application/json; charset=utf-8" -Body $utf8
}

$body = @{
    persona = "Entrevistador de Talent Acquisition e ML Platform"
    instructions = "Aprofunda motivacao, ownership e profundidade tecnica. Responde em portugues de Portugal."
    questions = @("Fala-me de um pipeline de machine learning que tenhas construido.", "Como o monitorizarias em producao?")
}
$start = Invoke-JsonPost "http://127.0.0.1:8000/start" $body
Write-Host "Question: $($start.current_question)"
$askBody = @{ session_id = $start.session_id; text_answer = "Construi um pipeline de validacao de cheques com computer vision, OCR local, uma API REST, Docker e CI/CD." }
$result = Invoke-JsonPost "http://127.0.0.1:8000/ask" $askBody
$result | ConvertTo-Json -Depth 5
Invoke-JsonPost "http://127.0.0.1:8000/stop" @{ session_id = $start.session_id } | ConvertTo-Json -Depth 5
