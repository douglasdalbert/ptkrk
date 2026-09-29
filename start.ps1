$wifi = Get-NetIPConfiguration | Where-Object {
    $_.NetAdapter.Status -eq 'Up' -and $_.NetAdapter.InterfaceDescription -match 'Wi-Fi|Wireless|802\.11' -and
    $_.IPv4Address -and $_.IPv4DefaultGateway
} | Select-Object -First 1
if (-not $wifi) { throw 'Nenhuma interface Wi-Fi ativa com IPv4 e gateway foi encontrada.' }
$env:KARAOKE_LAN_IP = $wifi.IPv4Address.IPAddress
if (-not $env:KARAOKE_LAN_IP) { throw 'Não foi possível detectar o IPv4 da rede Wi-Fi.' }
Write-Host "Karaoke na rede: http://$($env:KARAOKE_LAN_IP):8000/cantor"
Write-Host 'TV no computador host: http://localhost:8001/tv'
Write-Host "Enviando KARAOKE_LAN_IP=$($env:KARAOKE_LAN_IP) para os containers."
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw 'Falha ao iniciar os containers.' }