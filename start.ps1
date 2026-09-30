$wifi = Get-NetIPConfiguration | Where-Object {
    $_.NetAdapter.Status -eq 'Up' -and $_.NetAdapter.InterfaceDescription -match 'Wi-Fi|Wireless|802\.11' -and
    $_.IPv4Address -and $_.IPv4DefaultGateway
} | Select-Object -First 1
if (-not $wifi) { throw 'Nenhuma interface Wi-Fi ativa com IPv4 e gateway foi encontrada.' }
$env:KARAOKE_LAN_IP = $wifi.IPv4Address.IPAddress
if (-not $env:KARAOKE_LAN_IP) { throw 'Não foi possível detectar o IPv4 da rede Wi-Fi.' }

$mkcertCommand = Get-Command mkcert -ErrorAction SilentlyContinue
$mkcertPath = if ($mkcertCommand) {
    $mkcertCommand.Source
} else {
    Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\mkcert.exe'
}
if (-not (Test-Path $mkcertPath)) {
    throw 'mkcert não encontrado. Instale com: winget install --id FiloSottile.mkcert'
}

$certificateDirectory = Join-Path $PSScriptRoot '.certs'
$certificatePath = Join-Path $certificateDirectory 'singer.pem'
$certificateKeyPath = Join-Path $certificateDirectory 'singer-key.pem'
$certificateIpPath = Join-Path $certificateDirectory 'singer-ip.txt'
$rootCertificatePath = Join-Path $certificateDirectory 'rootCA.pem'
New-Item -ItemType Directory -Force -Path $certificateDirectory | Out-Null

& $mkcertPath -install
if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar a CA local do mkcert no Windows.' }

$certificateIp = if (Test-Path $certificateIpPath) { (Get-Content $certificateIpPath -Raw).Trim() } else { '' }
if (-not (Test-Path $certificatePath) -or -not (Test-Path $certificateKeyPath) -or
    $certificateIp -ne $env:KARAOKE_LAN_IP) {
    & $mkcertPath -cert-file $certificatePath -key-file $certificateKeyPath `
        $env:KARAOKE_LAN_IP localhost 127.0.0.1 ::1
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao gerar o certificado HTTPS do karaokê.' }
    Set-Content -Path $certificateIpPath -Value $env:KARAOKE_LAN_IP
}

$mkcertRoot = & $mkcertPath -CAROOT
if ($LASTEXITCODE -ne 0) { throw 'Não foi possível localizar a CA local do mkcert.' }
Copy-Item (Join-Path $mkcertRoot 'rootCA.pem') $rootCertificatePath -Force

Write-Host "Karaoke na rede: https://$($env:KARAOKE_LAN_IP):8000/cantor"
Write-Host 'TV no computador host: http://localhost:8001/tv'
Write-Host "CA para instalar e confiar nos celulares: $rootCertificatePath"
Write-Host "Enviando KARAOKE_LAN_IP=$($env:KARAOKE_LAN_IP) para os containers."
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw 'Falha ao iniciar os containers.' }
docker compose watch
if ($LASTEXITCODE -ne 0) { throw 'Falha ao monitorar alterações.' }