$ErrorActionPreference = "Stop"
$body = @{
    persona = "Mollie Talent Acquisition interviewer"
    instructions = "Probe motivation and technical depth."
    questions = @("Tell me about a machine learning pipeline you built.", "How would you monitor it in production?")
} | ConvertTo-Json
$start = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/start -ContentType "application/json" -Body $body
Write-Host "Question: $($start.current_question)"
$askBody = @{ session_id = $start.session_id; text_answer = "I built a cheque validation pipeline with computer vision, local LLM OCR, a REST API, Docker and CI/CD." } | ConvertTo-Json
$result = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/ask -ContentType "application/json" -Body $askBody
$result | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/stop -ContentType "application/json" -Body (@{ session_id = $start.session_id } | ConvertTo-Json) | ConvertTo-Json -Depth 5