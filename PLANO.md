# Karaokê em rede local

Documento vivo de requisitos e progresso. Estado atual: API, worker de download/prévias e legendas nativas divididas em blocos, `app-singer` com captura de microfone e `app-tv` com blocos letrados móveis, sincronismo e placar ao vivo; a precisão real ainda precisa ser conferida em `1pLrF_rGLyg` com microfone/aparelho. O repositório já está ligado ao Git; não criar branch nem commit automaticamente.

## Objetivo

Um PC Windows com Docker Desktop hospeda uma aplicação web acessível a celulares na mesma rede Wi-Fi. O navegador do host exibe o jogo numa TV 16:9. Celulares fazem pedidos e acompanham a fila, recordes e convites para cantar. Visual noturno compacto, com fundo escuro, neon verde/rosa e contraste legível à distância.

## Fluxos de interface

- **Entrada pela TV:** a tela roda no navegador do PC host, replicada na TV por HDMI, sem chave. `app-tv` exibe no canto inferior direito um QR que contém **somente** `http://IP_WIFI_DO_HOST:8000/cantor` (a interface `app-singer`); mostrar o endereço em texto também. O QR ajuda a encontrar o host, não é requisito para login nem contém ID/segredo da festa. Testar leitura a distância.
- **Entrada no celular:** informar nome; o servidor reutiliza o `singer_id` existente para o mesmo nome (ignorando maiúsculas e espaços repetidos) para recuperar os pedidos após reinstalar/reiniciar o navegador. Consolida perfis duplicados deixados por versões antigas e emite um token novo, invalidando o anterior. O nome é a chave de recuperação e, conforme a regra da festa, deve identificar uma única pessoa. Um reset apaga identidades e pedidos; depois dele o mesmo nome começa uma nova festa.
- **Fluxo do convidado:** conectar-se ao mesmo Wi-Fi, ler o QR **ou abrir diretamente a URL LAN**, informar o nome e usar o celular para escolher vídeos; quando chegar sua vez, aceitar e cantar com o microfone, se já estiver habilitado e autorizado pelo navegador. No PC host também é possível abrir `http://localhost:8000/cantor`; no celular, `localhost` apontaria para o próprio celular. Após reset, pedir nome novamente no mesmo endereço.
- **Tela do celular:** mostrar toda a fila em ordem, autor e título, duas posições protegidas, melhor pontuação pessoal e melhor pontuação geral (autor e música). Receber alterações por WebSocket, com snapshot integral na reconexão; sem polling de 10 segundos.
- **Pedido:** celular aceita link ou código do vídeo; extrai o ID de 11 caracteres e envia apenas `youtubeCode` com sua sessão. Host valida o código e atribui posição imediatamente. Mostrar `○ Em fila` enquanto aguarda preparo, indicador animado `Processando` durante download/legenda e `✓ Pronto` quando o vídeo estiver utilizável; as barras são exibidas quando a legenda automática nativa está disponível.
- **Remoção pelo cantor:** cada pessoa pode tirar uma música que pediu antes de aceitá-la, inclusive se estiver em preparo ou falhou. Mostrar confirmação reutilizável `ConfirmDialog` com título e imagem de prévia quando disponível; cancelar mantém o pedido. Só o dono pode confirmar; o servidor remove da fila e atualiza as posições para todos, sem apagar o cache compartilhado de vídeo usado por outros pedidos. Um download já em andamento pode terminar, mas não pode recriar o pedido removido.
- **Convite:** o dono da primeira música pronta entre as duas do pódio vê aviso com botão OK, sem prazo automático de aceite. Tentar vibrar quando suportado; notificações/vibração em segundo plano não são garantidas. Só o servidor valida o aceite.
- **Cantar com outros:** ao lado de "OK, vou cantar", mostrar botão "Cantar com outros" em outra cor. Ele abre uma lista vertical das pessoas da festa; tocar num nome envia convite e exibe espera, V verde quando a pessoa aceita e X vermelho quando recusa. O convidado recebe `ConfirmDialog` com "Aceito" e "Recusar". "Cantar em equipe" inicia uma única apresentação para o `singer` e os `backvocals` aceitos. Um convite ainda sem resposta pode ser aceito depois do início, permitindo cantar junto, mas essa pessoa fica marcada como não elegível para a pontuação daquela música. A medição e pontuação conjunta de voz continuam futuras.
- **Pular música:** qualquer cliente pode pedir para pular a música em uso (aguardando aceite ou já em reprodução), se `KARAOKE_ALLOW_SKIP` estiver habilitado (padrão `true`). Após `ConfirmDialog`, publicar "Pulando música" e contar 5 s para todos; então avançar para a próxima pronta do pódio. Antes do aceite, adiar a atual para a terceira posição sem apagá-la; após o aceite, encerrá-la sem nova contagem ou retorno à fila. Nenhum pulo ocorre automaticamente por demora do cantor.
- **TV durante a música:** vídeo e áudio originais usam toda a área acima do rodapé fixo. Reprodução com `object-fit: contain`: preservar 100% do quadro e suas legendas, admitindo faixas vazias no eixo que sobrar; jamais cortar para preencher a tela. Cada bloco contém apenas o texto novo do cue, usando timestamps internos do VTT quando disponíveis; cada bloco colorido entra pela direita e sua borda esquerda chega ao marcador central em `start_ms`, sincronizado com `video.currentTime` mesmo após pausa ou seek. Todos os blocos pendentes compartilham a cor-base escolhida na TV; o acerto fica azulado e a janela de acerto encerrada sem hit fica avermelhada. O início vocal dentro de +/- 100 ms do começo do bloco credita esse bloco uma vez. Microfone precisa de gesto explícito e contexto HTTPS; celular envia onset com posição estimada, e a TV exibe o placar. Usar fullscreen do navegador (F11), pois fullscreen nativo do player pode ocultar overlays. Se o navegador bloquear autoplay com áudio, o apresentador usa o botão Reproduzir.
- **TV entre músicas e rodapé:** a prévia principal ocupa o palco acima do rodapé. O rodapé fixo, em toda a largura, tem uma faixa horizontal "Próximas músicas" com até quatro vídeos (overflow oculto conforme a largura disponível), uma coluna de ações rolável com "Alterne as cores dos blocos" e, à direita, "Entre na festa" com QR e URL empilhados. O QR define a altura estável de 172px do rodapé. Uma bolinha flutuante no canto superior direito indica conexão: amarelo ao conectar/reconectar, verde quando conectado e vermelho se a conexão falhar; tooltip e nome acessível identificam o estado. Ações de cor (`C`) e nova festa (`Ctrl+Alt+N`) podem ser clicadas; iniciar nova festa pede confirmação.
- **Teclado do host:** setas cima/baixo percorrem ciclicamente topo, meio e base da área dos blocos (base + baixo = topo; topo + cima = base). Sem fundo de barra, os blocos passam sobre o vídeo e um marcador vertical leve indica o instante de cantar. `C` alterna cinco cores-base de contraste para todos os blocos pendentes; acertos ficam azulados e erros avermelhados. Celulares não recebem esses atalhos.
- **Nova festa na TV:** `Ctrl+Alt+N` no teclado do `app-tv` inicia imediatamente um karaokê do zero, sem preservar fila, pontuações ou identidades. A página intercepta o atalho; o endpoint de reset existe apenas no serviço da TV, ligado ao loopback do host (`127.0.0.1:8001`), não na porta LAN dos celulares. Testar o atalho no navegador real do host.

## Reset completo da festa

- O servidor executa uma única operação de reset para `Ctrl+Alt+N`: interromper reprodução/contagem de aceite, bloquear novos pedidos, cancelar ou impedir a conclusão de downloads e análises em andamento, limpar pedidos, clientes, sessões, contadores, resultados, recordes, análises e estado da festa no SQLite; apagar também **todos** os vídeos baixados, áudio extraído, prévias e caches do volume de mídia em ambos os modos. Manter somente esquema do banco e configuração da aplicação para que a festa recomece imediatamente.
- `NODE_ENV` vem do `.env` local (não versionado); manter `.env.example` versionado. O padrão de produção é `production`; para desenvolvimento, usar `development`. Em produção, apagar o vídeo e a prévia quando o pedido terminar, falhar, for cancelado ou pulado, mas somente se nenhum outro pedido ativo usar o mesmo vídeo. Em desenvolvimento, manter a mídia nesses términos para facilitar o debug; limpar tudo apenas ao iniciar uma nova festa.
- Invalidar todas as conexões e sessões: encerrar WebSockets, apagar `singer_id`, token e `party` do navegador e voltar à tela de nome. Celulares offline devem receber sessão inválida ao voltar; em qualquer resposta HTTP ou mensagem WebSocket, se a geração diferir da sessão, deslogar. Nenhum ID antigo pode autorizar ações depois do reset. A TV volta ao estado inicial sem música.
- O QR continua apontando para a mesma URL após o reset: é apenas endereço, não convite descartável. A geração muda **no servidor** e no cadastro seguinte; clientes antigos precisam informar nome novamente.
- Tornar o reset serializado e idempotente, persistindo uma nova geração da festa para impedir que trabalhos antigos ou mensagens atrasadas recriem dados/arquivos após a limpeza. Não usar `docker compose down -v` para esse atalho: o reset ocorre com os containers ativos e preserva os volumes para o próximo uso.

## Fila e aceite

- Cada pedido tem ID próprio, cliente, título, prévia, estado, ordem e resultado. Recebe posição mesmo antes do preparo. As primeiras **duas** posições ficam protegidas contra novos pedidos; a posição só muda ao cantar, remover ou pular. Vaga liberada é ocupada pelo primeiro da espera.
- O pódio contém as duas próximas músicas na ordem. Escolher a primeira **pronta** dentre elas, sem mudar as posições: se a #1 ainda processa e a #2 está pronta, executar a #2. Se nenhuma das duas está pronta, esperar; a #3 não fura o pódio.
- Na espera, manter a ordem das músicas de cada pessoa, intercalar pessoas e favorecer quem **aceitou menos músicas**; incrementar o contador no aceite válido, não no pedido. Considerar também quantas posições da fila a pessoa já ocupa para impedir sequência longa de uma mesma pessoa. Empates respeitam a chegada. Antes de codificar, fechar e testar regras de desempate que satisfaçam os exemplos abaixo; um sort simples por contador não basta.
- Cenário A/B: A1 e B1 estão protegidos; espera A2, B2, A3, B3, A4, B4, A5, A6... Se B pedir outra, B6 fica entre A6 e A7. Novos pedidos nunca ultrapassam as duas posições protegidas.
- Cenário C: com A1 e B1 protegidos, C1 e C2 (nunca cantou) entram na espera conforme a regra de balanceamento, sem ultrapassar as duas posições protegidas.
- Só o dono do convite pode aceitar uma vez. Aceite inicia a apresentação e conta como música aceita, mesmo se o cliente desconectar depois (revisar política se necessário). Não há mais prazo de 20 s nem remoção após três ausências.
- Pulo manual antes do aceite: após a confirmação de um cliente, contar 5 s no servidor e colocar a música em uso na terceira posição (ou no fim se houver menos de três), preservando a ordem das demais; escolher outra pronta no pódio. Pulo durante reprodução encerra essa apresentação e avança após os mesmos 5 s. Se não houver outra pronta, a TV aguarda; não reinvitar imediatamente a música pulada.
- Publicar estado da fila com versão a cada transição; após reinício/reconexão, recompor estados e prazos a partir de dados persistidos. Manter um único coordenador da fila no MVP.

## Mídia e limite legal

- **Requisito obrigatório desde o MVP:** usar vídeos do YouTube escolhidos na festa, aceitar o link no celular, transmitir só seu `youtubeCode` ao host, baixar o vídeo e reproduzir o arquivo local com vídeo e áudio originais na TV. Usar `yt-dlp` para vídeos acessíveis e `FFmpeg` para prévia; obter a legenda automática nativa para sincronizar barras letradas. Se a legenda falhar, manter o vídeo utilizável sem barras. Não contornar DRM nem controles de acesso.
- Validar no servidor apenas IDs de vídeo do YouTube com 11 caracteres permitidos, montar a URL canônica no worker (não permitir URLs arbitrárias), limitar duração/tamanho e evitar duplicar arquivo já baixado. Vídeo público não implica autorização automática de download, armazenamento ou reprodução; verificar as condições aplicáveis aos vídeos usados. Alguns vídeos públicos podem falhar por restrições ou mudanças da plataforma; teste real ainda pendente.
- Gerar blocos somente a partir da legenda automática nativa, ignorando conteúdos entre colchetes e marcadores de locutor `>>`. Ler cada cue pela linha de timestamp, inclusive quando houver linhas contendo apenas espaços. Usar o timestamp interno de cada segmento VTT quando disponível; sem ele, emitir o texto novo do cue uma única vez, sem inventar tempos por palavra. Descontar sobreposição com o bloco anterior quando os inícios estiverem separados por até `KARAOKE_SCORE_BLOCK_MS` (padrão 1000 ms; no `.env` local, 3000 ms). Manter também histórico de sequências brutas e sem repetição por `KARAOKE_CAPTION_HISTORY_MS` (padrão 60000 ms): descartar sequências de três ou mais palavras já contidas no histórico recente. Cada bloco termina antes do próximo início, no fim do cue ou no limite máximo, o que vier primeiro. No VTT `1pLrF_rGLyg`, a simulação atual produz 169 blocos para 105 cues; tempos de legenda ainda são aproximações, não ground truth fonético. Sidecar é servido somente pela rota local da TV; caches antigos das dez primeiras músicas prontas são atualizados pelo worker quando há capacidade. Legenda indisponível não bloqueia o vídeo.
- Limitar download e preparação de legendas às **dez primeiras posições** da fila, priorizando a ordem atual de uso. Até dois trabalhos podem ser preparados simultaneamente; se alguém sobe para as dez primeiras por prioridade, começa assim que houver capacidade sem cancelar trabalho já iniciado que caiu para a 11ª posição.

## Jogo e sincronização

- A posição real do player na TV é a referência, inclusive durante pausas, buffering e seeks. TV comunica posição/estado durante a execução; servidor associa amostras com tempo do vídeo após compensar latência e deriva da rede. Evitar stream de áudio bruto constante como primeira solução.
- Celular, com permissão explícita, deverá detectar o início do canto e enviar eventos compactos com timestamps via WebSocket. Descartar dados atrasados; avaliar WebRTC apenas se a medição da rede mostrar necessidade.
- Cada bloco vocal detectado dentro da tolerância de `KARAOKE_SCORE_TOLERANCE_MS` (padrão 100 ms) rende 1 hit; repetir onset no mesmo bloco não conta outra vez. Som detectado fora da letra gera `KARAOKE_SCORE_OFF_CUE_PENALTY` (padrão 1), limitado a uma penalidade por janela `KARAOKE_SCORE_OFF_CUE_REARM_MS` (padrão 500 ms). Ranking: `clamp(hits - penalidades, 0, total_blocos) / total_blocos * KARAOKE_SCORE_MAX` (padrão 1000), exibido com uma casa decimal. São pontos experimentais; ruído, eco e falso onset precisam ser calibrados em dispositivo real.
- **Risco principal:** microfone do celular perto da TV escuta o áudio original, que pode produzir falso acerto. Testar com TV ligada, distância real, fones/microfone próximo da boca, cancelamento de eco e/ou comparação com áudio de referência. Se insuficiente, oferecer microfone no host ou deixar o placar explicitamente recreativo. Detecção de presença vocal não prova que o cliente esteja cantando as palavras corretas (fora de escopo).
- **Bônus futuro:** acumular 60 s de canto correto, mesmo não contínuos; se cantor original e cliente elevarem intensidade relativa juntos, acionar até 15 s com `1,5 ponto/s` correto. Calibrar volumes individualmente. Limitar o placar total aos segundos de voz da referência já transcorridos: 50 pontos após 100 s + 15 s perfeitos com bônus = 72,5, com teto 115 naquele instante.
- Guardar pontuação por apresentação, recorde pessoal e geral por pontos absolutos; opcionalmente exibir percentual para comparar músicas de durações diferentes.

## Arquitetura inicial proposta

- **Python + FastAPI** para API, WebSocket, fila, sessões e pontuação. As interfaces se chamam **`app-singer`** (celular) e **`app-tv`** (host/TV); compartilham API e WebSocket, sem criar dois backends nem dois projetos Docker Compose. Um worker Python separado executa FFmpeg e análise; nunca bloquear o processo da TV com análise pesada.
- **SQLite** em volume Docker nomeado, modo WAL, para fila, cantores, análises e recordes; volume separado para mídia. A tabela `singers`, as chaves `singer_id` e as contagens substituem `clients`/`client_id` sem perder a festa existente; sessões antigas do navegador são convertidas no próximo acesso. Primeiro MVP: tarefas persistidas no banco, reivindicadas transacionalmente por um worker. Redis ou outro broker só se medições exigirem. Sem múltiplas réplicas de app até coordenar relógios/fila.
- Compose `name: karaoke` agrupa `app-singer` (LAN, porta 8000), `app-tv` (somente host, `127.0.0.1:8001`) e `worker`, com volumes persistentes separados. `start.ps1` detecta automaticamente o IPv4 Wi-Fi do Windows e fornece esse IP ao QR; não configurar URL manualmente. `app-singer` e `app-tv` são interfaces do mesmo sistema, não projetos Compose adicionais. Não tocar em `saope`; verificar firewall e isolamento Wi-Fi. HTTPS confiável continua pendente para o microfone.
- **HTTPS confiável no celular é necessário para captura do microfone** (`getUserMedia` exige contexto seguro, salvo exceções como localhost). O QR deve apontar para um endereço LAN que os celulares consigam abrir e, quando houver microfone, para a origem HTTPS usada pelo `app-singer` com `wss`; `localhost` na TV não aponta para o host no celular. Uma opção sem custos é CA local (`mkcert`), certificado para IP/hostname estável e proxy HTTPS no Compose, mas cada celular deve instalar e confiar na CA antes de usar o microfone; ler o QR não instala essa confiança. Alternativa a avaliar: certificado público gratuito por desafio DNS, se houver domínio/hostname e DNS local disponíveis. Testar permissão, autoplay e suspensão da aba.
- Validar URL/origem e tamanho dos uploads, escapar títulos, limitar pedidos e negar acesso do servidor a endereços arbitrários internos. Celulares não controlam a TV.

## Checklist incremental

### 0. Viabilidade e decisões

- [x] Confirmar YouTube obrigatório desde o MVP e download no host.
- [x] Confirmar `saope` como outro projeto no Docker Desktop; usar projeto Compose `karaoke` independente.
- [x] Confirmar vídeos públicos do YouTube como fonte desejada.
- [ ] Verificar condições de uso dos vídeos escolhidos e recursos do PC host/rede Wi-Fi.
- [ ] Medir em aparelhos reais: análise de voz, eco da TV, latência e HTTPS/microfone.
- [ ] Especificar desempates da fila e testes para os cenários A/B/C acima.

### 1. MVP: festa e fila

- [x] Criar projeto Compose `karaoke`, serviço web mínimo, volumes e validar subida no Docker Desktop (`GET /health`).
- [x] Implementar banco SQLite, cadastro por nome com sessão e API de pedidos com validação do código de vídeo do YouTube.
- [x] Adicionar worker independente de download com `yt-dlp` e FFmpeg, limite de 12 minutos/500 MB e testes sem mídia real.
- [x] Gerar prévia JPEG com FFmpeg antes de marcar pedido como pronto; entregar prévia via API autenticada e testar com vídeo sintético.
- [x] Testar download real de um vídeo público do YouTube (`VV1XWJN3nJo`): MP4 local com resposta parcial `206` e prévia JPEG `200` na TV.
- [ ] Testar extração/análise vocal e acesso pelo QR em um celular físico na LAN. Um vídeo público funcionar não garante suporte a todos.
- [x] Implementar `app-singer` inicial: entrada por nome, sessão persistida, pedidos pelo celular e layout compacto.
- [x] Recuperar identidade, pedidos e contagens ao entrar de novo com o mesmo nome; consolidar perfis duplicados antigos e renovar o token de sessão.
- [x] Fixar barra de ação de 48px no rodapé do `app-singer`: pulo como botão e aceite exclusivo quando for a vez do cantor; conteúdo restante rolável.
- [x] Detectar automaticamente IP Wi-Fi no host pelo `start.ps1`; exibir QR/URL de acesso ao cantor no canto inferior direito da TV, sem dados da festa. Teste do PNG concluído.
- [x] Invalidar sessões antigas no reset sem mudar a URL/QR; `POST /api/singers` retorna o novo `party`, e respostas da API e WebSocket permitem detectar troca de festa. Falta testar leitura do QR por um celular real na TV.
- [x] Exibir no celular os estados de download e prévias já disponíveis na API; confirmar push em navegador sem polling dos celulares.
- [x] Adicionar WebSocket autenticado para publicar alterações de pedidos; observador único do SQLite no servidor enquanto o worker é outro processo.
- [x] Posicionar pedidos desde a entrada, intercalar cantores e proteger as duas primeiras posições; testar A/B/C e selecionar a primeira pronta do pódio sem reordená-lo.
- [x] Limitar download/prévia às dez primeiras posições com até dois preparos simultâneos; testar promoção à top-10 sem cancelar trabalho já iniciado.
- [x] Aplicar o limite top-10 e concorrência máxima de dois também à obtenção da legenda e geração das barras letradas.
- [x] Persistir aceite autenticado e único e contagem das apresentações aceitas, sem timeout automático; exibir aviso/OK ao dono com vibração opcional.
- [x] Convidar pessoas da festa para a mesma música com resposta individual, V verde/X vermelho e inclusão após início sem elegibilidade para pontuação naquela música.
- [x] Persistir pedido manual de pulo por qualquer cliente com `ConfirmDialog`, configuração `KARAOKE_ALLOW_SKIP` (padrão `true`) e contagem de 5 s no servidor; testar adiamento antes do aceite e encerramento depois do aceite.
- [x] Permitir remover um pedido próprio não aceito após `ConfirmDialog` reutilizável com título e prévia opcional; cancelar conserva o pedido, confirmar compacta a fila e impede ressurgimento ao terminar download.
- [x] Ativar convites com a TV local conectada, reproduzir MP4 após aceite e avançar ao fim ou ao concluir pulo; validar que a porta LAN não expõe controle da TV e testar streaming parcial.
- [x] Implementar `app-tv` com vídeo sem corte (`contain`) no palco máximo e rodapé fixo: até quatro próximos, ações roláveis clicáveis com atalhos, QR/URL à direita, conexão flutuante e confirmação para nova festa.
- [ ] Testar com vídeo real e celulares na LAN: autoplay, áudio, reconexão da TV durante vídeo e leitura do QR a distância.
- [x] Implementar `Ctrl+Alt+N` somente no `app-tv`: nova geração, limpeza do SQLite/mídia e invalidação das sessões, mantendo o mesmo endereço no QR; teste isolado do endpoint concluído.
- [ ] Testar reset durante vídeo, aceite e download em celulares reais, incluindo cliente offline e repetição do atalho.
- [ ] Testar reinício, reconexão e múltiplos celulares.

### 2. Sincronismo e placar

- [ ] Validar visualmente se a legenda de `1pLrF_rGLyg` acompanha suficientemente o início e fim do canto; cue de legenda é aproximação, não alinhamento fonético.
- [ ] Configurar HTTPS confiável e alternativa de microfone no host.
- [ ] Calibrar limiar de RMS, latência/eco e testar pausas, buffering, seek e perda de pacotes em `1pLrF_rGLyg`.
- [x] Implementar captura de onset, blocos de legenda, penalidade off-cue e ranking configurável; falta validação humana/aparelho real e persistência de recordes de longo prazo.

### 3. Evolução

- [ ] Testar separação vocal/modelos melhores se análise simples for insuficiente.
- [ ] Implementar bônus de intensidade, duração e teto após validação real.
- [ ] Definir recuperação de identidade, limpeza de cache e avisos em segundo plano.
- [ ] Revisar resiliência do downloader a mudanças do YouTube e estratégias de cache/limpeza.

## Perguntas pendentes

1. Verificar condições de download, armazenamento e exibição dos vídeos públicos efetivamente usados nesta festa.
2. Podemos testar um celular cantando perto da TV (com e sem fones) e qual é o hardware do host?
3. A primeira entrega pode ser a festa com fila/vídeo, deixando pontuação automática para a etapa seguinte?

## Execução atual

- No Windows host, executar `./start.ps1` no PowerShell: ele detecta o IP Wi-Fi, inicia `app-singer`, `app-tv` e `worker`, mostra os endereços e mantém `docker compose watch` ativo para sincronizar alterações. Deixar essa janela aberta enquanto desenvolve. Abrir **`http://localhost:8001/tv`** no PC conectado à TV por HDMI, sem senha; cantores entram pelo QR ou digitando `http://IP_WIFI_DO_HOST:8000/cantor`. No host, `http://localhost:8000/cantor` também funciona. A porta 8001 é ligada a `127.0.0.1`, não à LAN. `Ctrl+Alt+N` apaga a festa atual: usar apenas quando quiser recomeçar.
- `compose.yaml` sincroniza alterações em `app`, `app-singer` e `app-tv`; Python recarrega nos serviços automaticamente, e mudanças de interface ficam disponíveis ao atualizar a página. Alterações em `Dockerfile` ou `requirements.txt` acionam rebuild pelo watcher. Não é necessário executar `docker compose up -d` a cada edição.
- API inicial: `POST /api/singers` recebe `{"name":"Nome"}` e retorna `singer_id`, token e `party`; `POST /api/requests` recebe `{"youtubeCode":"glvVYIhdWlU"}` com `Authorization: Bearer TOKEN` (não recebe URL inteira); respostas `/api/` expõem a geração em `X-Karaoke-Party`. `GET /api/requests` lista `pending`, `processing`, `ready` e `failed`. Pedidos prontos têm JPEG em `GET /api/requests/{id}/preview`. O worker prepara pedidos da festa atual; se faltar MP4 em um pedido antigo `ready`, ele volta para `pending` sem perder posição. Rodar testes sem baixar vídeos externos com `docker compose run --rm --no-deps app-singer python -m unittest discover -s tests -v`.
- `KARAOKE_ALLOW_SKIP=false` desliga o pulo. O celular usa `ws` nesta fase; o microfone exigirá HTTPS confiável e `wss` depois. Pedidos em fila/processamento/prontos têm posição; pedidos com falha não. O dono remove antes do aceite com `DELETE /api/requests/{id}`; durante convite ativo, só o dono aceita, e qualquer cliente autenticado pode pedir pulo com cinco segundos de contagem. Recordes permanecem vazios até existir pontuação.