# Karaokê em rede local

Documento vivo de requisitos e progresso. Estado atual: base Docker executável com endpoint de saúde; interface, fila, download e pontuação ainda não implementados. O repositório já está ligado ao Git; não criar branch nem commit automaticamente.

## Objetivo

Um PC Windows com Docker Desktop hospeda uma aplicação web acessível a celulares na mesma rede Wi-Fi. O navegador do host exibe o jogo numa TV 16:9. Celulares fazem pedidos e acompanham a fila, recordes e convites para cantar. Visual noturno compacto, com fundo escuro, neon verde/rosa e contraste legível à distância.

## Fluxos de interface

- **Entrada no celular:** informar nome; o servidor cria um `client_id` persistido no navegador. Não confiar no ID sozinho como autenticação: emitir também um segredo de sessão para validar aceite e eventos do microfone.
- **Tela do celular:** mostrar toda a fila em ordem, autor e título, quatro posições protegidas, melhor pontuação pessoal e melhor pontuação geral (autor e música). Receber alterações por WebSocket, com snapshot integral na reconexão; sem polling de 10 segundos.
- **Pedido:** celular envia link do YouTube e sua identidade. Host valida o link, baixa o vídeo, prepara mídia e análise; pedidos ainda em preparo não entram na fila pronta. Mostrar estados de preparo, falha e pronto.
- **Convite:** dono do primeiro pedido vê aviso com botão OK e prazo de 20 s (configurável). Tentar vibrar, quando suportado; notificações/vibração em segundo plano não são garantidas. Relógio e aceite são validados no servidor.
- **TV durante a música:** vídeo e áudio originais ocupam a tela com barra animada e pontuação por cima. Usar fullscreen do navegador (F11), pois fullscreen nativo do player pode ocultar overlays.
- **TV entre músicas:** prévia do próximo vídeo ocupa cerca de 50% da tela com nome, título e contagem regressiva; abaixo, três próximas prévias lado a lado, cada uma com cerca de 15% da área, nome e título. Adaptar para filas com menos de quatro pedidos.
- **Teclado do host:** setas cima/baixo percorrem ciclicamente topo, meio e base da barra (base + baixo = topo; topo + cima = base). A barra cobre toda a largura do vídeo e fica sobre legendas existentes conforme a posição escolhida. `C` alterna cinco paletas de contraste: padrão amarelo escuro, verde para acerto e vermelho para erro, além de quatro alternativas a definir/testar em vídeos claros e escuros. Celulares não recebem esses atalhos.

## Fila e aceite

- Cada pedido tem ID próprio, cliente, título, prévia, estado, ordem, tentativas perdidas e resultado. As primeiras **quatro** posições da fila pronta não são reordenadas por pedidos novos. Vaga liberada por música cantada/removida é ocupada pelo primeiro da espera.
- Na espera, manter a ordem das músicas de cada pessoa, intercalar pessoas e favorecer quem **aceitou menos músicas**; incrementar o contador no aceite válido, não no pedido. Considerar também quantas posições da fila a pessoa já ocupa para impedir sequência longa de uma mesma pessoa. Empates respeitam a chegada. Antes de codificar, fechar e testar regras de desempate que satisfaçam os exemplos abaixo; um sort simples por contador não basta.
- Cenário A/B: A1, B1, A2, B2 estão protegidos; espera A3, B3, A4, B4, A5, B5, A6, A7... Se B pedir outra, B6 fica entre A6 e A7.
- Cenário C: com A1, B1, A2, B2 protegidos, C1 (nunca cantou) entra antes de A3; C2 entra depois de A3 e B3, antes de A4. Novos pedidos nunca ultrapassam as quatro posições protegidas.
- O primeiro entra em `aguardando_aceite` por 20 s. Só seu dono pode aceitá-lo, uma vez, antes do prazo. Aceite inicia a apresentação e conta como música aceita, mesmo se ocorrer desconexão posterior (revisar política se necessário).
- Sem aceite, mover o primeiro para a **terceira posição** (índice 2, ou final se a fila for menor); o antigo segundo assume o primeiro lugar e recebe novo prazo completo de 20 s. Essa movimentação por recusa é exceção à proteção das quatro posições. Na terceira perda de prazo **do mesmo pedido**, removê-lo. Com um pedido apenas, repetir o prazo até aceitar ou atingir três perdas.
- Publicar estado da fila com versão a cada transição; após reinício/reconexão, recompor estados e prazos a partir de dados persistidos. Manter um único coordenador da fila no MVP.

## Mídia e limite legal

- **Requisito obrigatório desde o MVP:** receber link do YouTube, baixar o vídeo no host, analisar áudio antes de tocar, guardar intervalos e reproduzir o arquivo local com vídeo e áudio originais na TV. Usar `yt-dlp` para obtenção de vídeos acessíveis e `FFmpeg` para normalização/extração de áudio e prévia; tratar falhas e mudanças do YouTube sem travar a fila. Não contornar DRM nem controles de acesso.
- Aceitar apenas URLs de vídeo do YouTube (normalizar e validar ID, não permitir download de URLs arbitrárias), limitar duração/tamanho e evitar duplicar arquivo já baixado. **Pendente operacional:** confirmar que os vídeos escolhidos podem ser baixados, armazenados e exibidos conforme direitos e termos aplicáveis. Link público não garante isso; não prometer suporte a todo vídeo.
- Processar mídia antes do show: separar vocais opcionalmente, detectar regiões com voz do cantor e intensidade relativa, armazenar `[inicio_ms, fim_ms]` com confiança por arquivo. Instrumentos e vozes do público podem produzir falsos positivos; testar com vídeos representativos. Cachear análise, limitar tamanho/duração/armazenamento e tratar codec/falhas. Nunca executar modelo pesado durante a música.

## Jogo e sincronização

- A posição real do player na TV é a referência, inclusive durante pausas, buffering e seeks. TV comunica posição/estado durante a execução; servidor associa amostras com tempo do vídeo após compensar latência e deriva da rede. Evitar stream de áudio bruto constante como primeira solução.
- Celular, com permissão explícita, calcula localmente atividade vocal e volume em janelas de 20 a 40 ms (Web Audio/AudioWorklet quando disponível) e envia amostras compactas com timestamps via WebSocket. Descartar dados atrasados; avaliar WebRTC apenas se a medição da rede mostrar necessidade.
- Barra anda da direita para a esquerda nos momentos de voz da referência. Acerto fica verde, erro vermelho e trecho neutro amarelo escuro na paleta padrão; placar ao vivo na TV. Começar com tolerância configurável de **+/- 300 ms** após calibração. Medir acerto em ms, sem arredondar a segundos; `1 ponto` por segundo de canto correto da referência. Cantar fora de região vocal ou deixar de cantar nela não soma.
- **Risco principal:** microfone do celular perto da TV escuta o áudio original, que pode produzir falso acerto. Testar com TV ligada, distância real, fones/microfone próximo da boca, cancelamento de eco e/ou comparação com áudio de referência. Se insuficiente, oferecer microfone no host ou deixar o placar explicitamente recreativo. Detecção de presença vocal não prova que o cliente esteja cantando as palavras corretas (fora de escopo).
- **Bônus futuro:** acumular 60 s de canto correto, mesmo não contínuos; se cantor original e cliente elevarem intensidade relativa juntos, acionar até 15 s com `1,5 ponto/s` correto. Calibrar volumes individualmente. Limitar o placar total aos segundos de voz da referência já transcorridos: 50 pontos após 100 s + 15 s perfeitos com bônus = 72,5, com teto 115 naquele instante.
- Guardar pontuação por apresentação, recorde pessoal e geral por pontos absolutos; opcionalmente exibir percentual para comparar músicas de durações diferentes.

## Arquitetura inicial proposta

- **Python + FastAPI** para API, WebSocket, fila, sessões e pontuação. Frontend web responsivo em TypeScript (framework a decidir após protótipo). Um worker Python separado executa FFmpeg e análise; nunca bloquear o processo da TV com análise pesada.
- **SQLite** em volume Docker nomeado, modo WAL, para fila, clientes, análises e recordes; volume separado para mídia. Primeiro MVP: tarefas persistidas no banco, reivindicadas transacionalmente por um worker. Redis ou outro broker só se medições exigirem. Sem múltiplas réplicas de app até coordenar relógios/fila.
- Compose `name: karaoke` já configura o serviço `app`, volumes persistentes separados (`db_data` e `media_data`) e porta `${KARAOKE_PORT:-8000}`. O Docker Desktop agrupa os containers sob `karaoke`, independentemente de `saope`; não criar pasta dentro desse outro projeto nem tocar nos seus containers. Adicionar o `worker` quando houver processamento a executar. Expor apenas o app para a LAN. Verificar firewall do Windows, IP da máquina e isolamento Wi-Fi entre dispositivos.
- **HTTPS confiável no celular é necessário para captura do microfone** (`getUserMedia` exige contexto seguro, salvo exceções como localhost). HTTP serve ao MVP de fila/TV; para microfone, planejar hostname/certificado confiável instalado nos aparelhos ou solução equivalente, com `wss`. Certificado autoassinado sem confiança instalada não resolve. Testar permissão, autoplay e suspensão da aba.
- Validar URL/origem e tamanho dos uploads, escapar títulos, limitar pedidos e negar acesso do servidor a endereços arbitrários internos. Celulares não controlam a TV.

## Checklist incremental

### 0. Viabilidade e decisões

- [x] Confirmar YouTube obrigatório desde o MVP e download no host.
- [x] Confirmar `saope` como outro projeto no Docker Desktop; usar projeto Compose `karaoke` independente.
- [ ] Confirmar direitos/fontes de vídeo permitidas e recursos do PC host e rede Wi-Fi.
- [ ] Medir em aparelhos reais: análise de voz, eco da TV, latência e HTTPS/microfone.
- [ ] Especificar desempates da fila e testes para os cenários A/B/C acima.

### 1. MVP: festa e fila

- [x] Criar projeto Compose `karaoke`, serviço web mínimo, volumes e validar subida no Docker Desktop (`GET /health`).
- [ ] Implementar banco SQLite, worker de download/análise e testar acesso real pela LAN.
- [ ] Implementar entrada por nome, sessão e interface mobile compacta.
- [ ] Implementar download de links do YouTube permitidos, estados de preparo e prévias.
- [ ] Implementar fila justa, quatro posições protegidas, três perdas, aceite e WebSocket.
- [ ] Implementar interface da TV, reprodução, intervalo e atalhos de barra/cores.
- [ ] Testar reinício, reconexão e múltiplos celulares.

### 2. Sincronismo e placar

- [ ] Pré-analisar intervalos de voz e medir precisão/custo de processamento.
- [ ] Configurar HTTPS confiável e captura de microfone com alternativa no host.
- [ ] Calibrar latência/eco e testar pausa, buffering, seek e perda de pacotes.
- [ ] Implementar barra animada, pontuação ao vivo e recordes.

### 3. Evolução

- [ ] Testar separação vocal/modelos melhores se análise simples for insuficiente.
- [ ] Implementar bônus de intensidade, duração e teto após validação real.
- [ ] Definir recuperação de identidade, limpeza de cache e avisos em segundo plano.
- [ ] Revisar resiliência do downloader a mudanças do YouTube e estratégias de cache/limpeza.

## Perguntas pendentes

1. Quais vídeos do YouTube estão autorizados para baixar/processar/exibir neste uso?
2. Podemos testar um celular cantando perto da TV (com e sem fones) e qual é o hardware do host?
3. A primeira entrega pode ser a festa com fila/vídeo, deixando pontuação automática para a etapa seguinte?

## Execução atual

- `docker compose up -d --build` inicia a base. `docker compose ps` mostra o projeto `karaoke`; `http://localhost:8000/health` retorna `{"status":"ok"}`. Esta URL ainda não é a interface do jogo.
- Para acesso de outro aparelho na mesma rede, usar `http://IP_DO_HOST:8000/health` após confirmar firewall e Wi-Fi; a porta pode ser alterada com `KARAOKE_PORT` no ambiente. O microfone do celular exigirá HTTPS confiável em uma etapa posterior.