#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

fail() {
    printf 'Erro: %s\n' "$1" >&2
    exit 1
}

command -v powershell.exe >/dev/null 2>&1 ||
    fail 'Este iniciador requer WSL com acesso ao PowerShell do Windows. No Windows, use ./start-windows.ps1 no PowerShell.'
command -v wslpath >/dev/null 2>&1 ||
    fail 'wslpath nao encontrado. Execute este script dentro do WSL.'
command -v docker >/dev/null 2>&1 ||
    fail 'Docker nao encontrado. Habilite a integracao WSL desta distribuicao no Docker Desktop.'
docker compose version >/dev/null ||
    fail 'Docker Compose indisponivel. Atualize o Docker Desktop.'
docker info >/dev/null ||
    fail 'Docker indisponivel. Inicie o Docker Desktop e habilite a integracao WSL.'

script_path="$(wslpath -w "$PWD/start-windows.ps1")"
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass \
    -File "$script_path" -PrepareOnly ||
    fail 'Falha ao preparar o IP Wi-Fi e os certificados pelo Windows.'

[[ -s .certs/singer-ip.txt ]] ||
    fail 'A preparacao nao gerou .certs/singer-ip.txt.'
KARAOKE_LAN_IP="$(tr -d '\r\n' < .certs/singer-ip.txt)"
[[ -n "$KARAOKE_LAN_IP" ]] ||
    fail 'O IPv4 Wi-Fi preparado pelo Windows esta vazio.'
export KARAOKE_LAN_IP

printf 'Enviando KARAOKE_LAN_IP=%s para os containers pelo WSL.\n' "$KARAOKE_LAN_IP"
docker compose up -d --build ||
    fail 'Falha ao iniciar os containers.'
docker compose watch ||
    fail 'Falha ao monitorar alteracoes.'
