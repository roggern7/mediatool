# Legendas CapCut — núcleo técnico

Este documento descreve o pacote `backend/app/integrations/capcut/` (o "núcleo"): como um vídeo vira um rascunho do
CapCut Desktop com legendas, como o Media Tool exporta esse rascunho pela interface do CapCut e quais são os limites.
A fila, a API e a tela ficam em outros módulos (ver `docs/CONTRACTS.md`, seção "Legendas CapCut").

Verificado nesta máquina com o **CapCut Desktop 9.7.1.4203 (Windows 11, interface em português)** em 2026-11-07.
Outras versões podem mudar o formato do rascunho ou os nomes da interface.

## Visão geral

```
process_video(spec)
  transcribing   faster-whisper local (CPU, int8)          transcribe.py
  (blocos)       chunk_words: 3 palavras / 24 caracteres    transcribe.py
  drafting       rascunho MT_<job>_m<mídia> + índice global draft.py + style.py
  waiting_capcut CapCut aberto por outra pessoa => CapCutBusy (o runner espera, sem gastar tentativa)
  launching      acessibilidade + abrir o CapCut            controller.py
  opening        clique no cartão do rascunho
  exporting      janela Exportar, opções, Exportar, espera o arquivo
  finalizing     ffprobe + áudio original sem recodificar   finalize.py
  cleaning       apaga o rascunho (se keep_draft = false)
```

| Módulo | Papel |
|---|---|
| `__init__.py` | Interface pública (contrato com o runner): `process_video`, `status`, `list_caption_drafts`, `extract_style`, `default_style`, `style_summary`, `CapCutJobSpec`, `CapCutResult`, `STAGES`, erros. |
| `errors.py` | `CapCutError(code, message, transient)` e subclasses `CapCutBusy`, `CapCutCancelled`, `CapCutNotInstalled`, `AccessibilityUnavailable`, `ForegroundLost`, `UiTimeout`, `ExportFailed`. |
| `install.py` | Detecção (só leitura), `is_running()` (PIDs de `CapCut.exe`; falha da consulta = `process_query_failed` transitório, nunca "fechado") e árvore de processos (`process_table`, `track_tree`). |
| `skill.py` | Carrega o capcut-agent-skill por caminho (importlib). |
| `transcribe.py` | Áudio via FFmpeg, faster-whisper, cache de modelos, `chunk_words`. |
| `style.py` | `CaptionStyle`: extrair de um rascunho, estilo padrão, aplicar a um bloco, destaque por palavra. |
| `draft.py` | Motor de rascunho 9.x, validação, `register_draft`/`unregister_draft`/`delete_draft`. |
| `controller.py` | `CapCutController` (UI Automation + cliques com trava), `ExportOptions`, patch do `globalSetting`. |
| `finalize.py` | Conferência do exportado, remux com o áudio original, nome seguro, limpeza do intermediário. |
| `pipeline.py` | A sequência acima, com injeção de instalação/controlador/transcritor (testes). |
| `media.py` | ffprobe/FFmpeg (`MEDIA_TOOL_FFMPEG`/`MEDIA_TOOL_FFPROBE` ou PATH) e gravação atômica. |
| `assets/base_draft/` | Rascunho-base mínimo (ver abaixo). |

Importar o pacote não carrega `faster_whisper`, `numpy`, `uiautomation` nem `pywin32` (importados dentro das funções);
sem eles o app sobe e `status()` mostra o que falta. Dependências opcionais em `backend/requirements-capcut.txt`.

## Instalação do CapCut (o que é lido)

| Item | Onde |
|---|---|
| Lançador | `%LOCALAPPDATA%\CapCut\Apps\CapCut.exe` |
| Versão | `Apps\ProductInfo.xml` -> `<full_appver value="9.7.1.4203"/>` |
| Rascunhos | `root_path` de `root_meta_info.json`; senão `currentCustomDraftPath=` do `User Data\Config\globalSetting` (barras invertidas escapadas `\\`); senão `User Data\Projects\com.lveditor.draft` |
| Índice global | `<rascunhos>\root_meta_info.json` (`all_draft_store[]`, `draft_ids`, `root_path`) |
| Exportação padrão | `%LOCALAPPDATA%\CapCut\Videos` |
| Fontes do sistema | `Apps\<versão>\Resources\Font\SystemFont\` |

`MEDIA_TOOL_CAPCUT_ROOT` (para testes) substitui `%LOCALAPPDATA%\CapCut`.

## O que vem do capcut-agent-skill e por que existe um motor próprio

Cópia sem modificação em `third_party/capcut-agent-skill/` (commit `bd8b88f`, MIT; detalhes em `UPSTREAM.md`).
Reaproveitado:

- `create_native_text_material` — material de texto nativo (branco, contorno preto 0,06, `check_flag` 15): é o
  **estilo padrão** quando o dono não escolheu modelo; posição y -0,70 e escala 1,919 do skill.
- `parse_time_us`.
- Invariantes do `draft_schema.md` §8: tempos inteiros em µs; espelho idêntico; fim[i] <= início[i+1] - 20 ms;
  pausa >= 180 ms limpa a legenda (fim = última palavra + 50 ms); uma trilha de texto; gravação atômica + backup.

O skill foi escrito para o macOS (`draft_info.json`, `project.json` com `current_timeline_id`, `~/Movies`, `pkill`).
No Windows 9.7 a linha do tempo é `draft_content.json`, com espelho em `Timelines/<id>/draft_content.json`, e o
`project.json` usa `main_timeline_id`. Por isso o motor de rascunho é próprio (`draft.py`).

## Formato do rascunho 9.7 (Windows)

Pasta de um rascunho: `draft_content.json` (linha do tempo; `id` == id da linha do tempo), `draft_meta_info.json`,
`timeline_layout.json` (`dockItems[0].timelineIds`), `Timelines/project.json` (`id`, `main_timeline_id`,
`timelines[0].id`), `Timelines/<id>/draft_content.json` (espelho), `draft_settings`, anexos e pastas de cache. O
CapCut cria `.locked` enquanto o rascunho está aberto. JSON aberto (não cifrado) nesta versão.

`build_draft` (só com o CapCut fechado; nome obrigatoriamente `MT_...`):

1. Copia o rascunho-base para uma pasta temporária `.<nome>.<x>.building` dentro da pasta de rascunhos, sem os
   transitórios (`draft_content.json.bak`, `template-2.tmp`, `template.tmp`, `.locked`, `project.json.bak`,
   `draft_cover.jpg`). O rascunho-base é o rascunho de onde o estilo veio; sem estilo extraído, a fixture.
2. Mantém só o 1º segmento da trilha de vídeo principal; remove as outras trilhas e os materiais referenciados só por
   elas (ex.: vídeos, áudios e textos antigos do dono). Ids novos: linha do tempo, projeto, trilha, segmento e
   material de vídeo, materiais extras do vídeo (speeds, canvases, ...), `draft_id`.
3. Troca o vídeo (ffprobe: duração, largura/altura com rotação, áudio, fps): `path` absoluto com `/`, `duration`,
   `width`, `height`, `has_audio`, `material_name`, recorte inteiro, velocidade 1, sem keyframes.
4. `duration`, `fps` = fps escolhido para a exportação (padrão 60; `original` = fps da origem; o fps do rascunho é o padrão da exportação no CapCut), `canvas_config` = dimensões da
   origem com `ratio: "original"`.
5. Uma trilha de texto com os segmentos de `apply_style` (render_index 14000+).
6. Grava o mesmo JSON na raiz e no espelho (temporário + `os.replace`), atualiza `project.json`,
   `timeline_layout.json` e `draft_meta_info.json` (`draft_id`, `draft_name`, `draft_fold_path` com `/`, `tm_*`,
   `tm_duration`, `draft_materials` tipo 0 com o vídeo novo).
7. `validate_draft`: espelho idêntico, `project.json`/layout coerentes, todo `material_id` e
   `extra_material_refs` existe, sem sobreposição na trilha de texto, nada além da duração, uma trilha de texto.
8. Renomeia a pasta temporária para o nome final. Uma pasta `MT_` com o mesmo nome é sobra de uma tentativa anterior
   do mesmo job e é substituída; nomes sem `MT_` são recusados.

`register_draft` põe a entrada no topo de `all_draft_store` (copiando os campos da entrada do rascunho-modelo, ou os
campos mínimos observados), atualiza `draft_ids` e grava de forma atômica. Campos de nuvem/compartilhamento (chaves com
`cloud`, `pippit_*`, `draft_enterprise*`, `draft_is_pippit_draft`) NUNCA vêm do modelo: ficam com os valores neutros
dos rascunhos locais (`False`, `""`, `-1` nos ids, `0` nos demais números, `draft_enterprise_info` vazio), na entrada do
índice e no `draft_meta_info.json` do MT_; antes da 1ª alteração guarda
`root_meta_info.json.bak-media-tool` (backup único). `unregister_draft`/`delete_draft` desfazem (só `MT_`). Tudo isso
recusa com `CapCutBusy` se houver `CapCut.exe` rodando: o CapCut mantém o índice em memória e o regravaria.

### Fixture `assets/base_draft/`

Gerada a partir de um rascunho real que o CapCut 9.7.1 regravou, com: textos e trilhas extras removidos, material de
vídeo esvaziado (sem caminho/nome), `device_id`/`mac_address` apagados, `draft_materials`, `draft_virtual_store` e
`attachment_id_mapping` esvaziados, ids fixos de exemplo, sem capa, sem `crypto_key_store.dat`, sem `draft.extra`,
sem `Resources/` e sem `subdraft/`.

## Texto, estilo e destaque

Material de texto (`materials.texts[]`, ~127 chaves no 9.7): `content` é JSON em string
`{"styles":[{fill, range:[ini,fim], size, strokes, bold, font?, useLetterColor}], "text": "..."}`. Segmento:
`material_id`, `target_timerange`, `clip` (`transform.y` negativo = abaixo do centro, `scale`), `uniform_scale`,
`extra_material_refs` (animações em `material_animations`, efeitos/bolhas em `effects`, ... referenciados por id).

- `extract_style` (só leitura) copia o material inteiro do segmento escolhido (segmentos de texto ordenados pelo
  início), o segmento (clip/escala/posição), `uniform_scale`, e cada material de `extra_material_refs` procurado em
  todos os arrays de `materials` (guardado como `{array, object}`), além de `caption_template_info`, `font_path`,
  `fonts`. Cor de destaque: com trechos de cores diferentes em `styles`, a cor do trecho com menos caracteres.
  Convenção de `words`: inferida do modelo quando ele traz `words` preenchidos (ms/µs, relativo/absoluto); padrão ms
  relativos ao início do segmento.
- `apply_style` clona tudo com ids novos, troca `content.text` e os `range` (contados em **unidades UTF-16**: igual a
  caracteres para acentos; emoji fora do BMP conta 2), preserva o 1º trecho de estilo (o que não é o destaque),
  atualiza `text`/`recognize_text` só quando o modelo os preenchia, preenche `words`.
- Destaque por palavra (`HighlightOptions(color)`): um bloco de N palavras vira N segmentos consecutivos sem buracos
  nem sobreposição (o 1º começa no início do bloco, cada um seguinte no início da sua palavra, o último termina no
  fim do bloco), todos com o texto inteiro e a palavra atual num trecho de `styles` com a cor de destaque (se o modelo
  tiver um trecho dessa cor, ele é usado, com tamanho/fonte próprios). Animações `in` só no 1º pedaço, `out` só no
  último, `loop` em todos; tempos das animações ajustados à duração de cada pedaço. É texto rico nativo do CapCut,
  não FFmpeg.

## Transcrição e blocos

FFmpeg decodifica para PCM float32 16 kHz mono (o decodificador PyAV do faster-whisper falha com o av 19:
`open() got an unexpected keyword argument 'metadata_errors'`). faster-whisper com `language='pt'`,
`word_timestamps=True`, `vad_filter=True`, `condition_on_previous_text=False`, `beam_size=5`, CPU `int8`. Modelos
`small`, `medium`, `large-v3-turbo` (padrão), baixados no 1º uso para `<data_dir>/models/whisper` (cache do
Hugging Face); um modelo carregado por tamanho fica em memória (`release_models()` libera). Cancelamento entre
segmentos. Sem fala: o job termina com cópia do original, sem legendas, e um aviso.

`chunk_words(words, max_words=3, max_chars=24, pause_split=0.18, min_duration=0.30, gap=0.02)`: quebra em pausa
>= 180 ms e depois de `.`, `!`, `?`, `…`; ao estourar o limite, prefere quebrar depois de `,`/`;`/`:` e não deixa
preposição/artigo curto (de, da, do, das, dos, a, o, as, os, e, em, no, na, com, pra, para, que, um, uma) sozinho no
fim do bloco; fim = última palavra + 50 ms, nunca além de (próximo - 20 ms); duração mínima 300 ms sem invadir o
próximo; texto com espaços simples.

## Automação da interface (UI Automation)

### Flags de acessibilidade

O Qt do CapCut vem com a acessibilidade desligada: `User Data\Config\globalSetting` (INI do QSettings, `[General]`,
CRLF) tem `disable_qt_access=1` e `accessibleDisableUnignoredChildren=1`. `prepare_accessibility()` — chamado antes de
CADA abertura, com o CapCut fechado — guarda uma única vez o original em `<data_dir>/capcut/globalSetting.orig` e
grava os dois valores como `0`, preservando o fim de linha, o BOM UTF-8 (se houver) e as demais linhas (gravação
atômica). O CapCut regrava os
dois valores para `1` logo depois de subir, mas a acessibilidade continua ativa naquela execução (verificado em
2026-11-07: 27 filhos de UI Automation na tela inicial em ~10 s). O arquivo NÃO é marcado como somente leitura;
`restore_settings()` não tem nada a desfazer. Se a tela inicial aparecer sem árvore de UI Automation por 25 s:
`AccessibilityUnavailable`.

### Janelas e controles (CapCut 9.7.1, português)

Janelas de topo do processo `CapCut.exe` (classe win32 `Qt622QWindowIcon`); o tipo vem de
`auto.ControlFromHandle(h).ClassName` (usar o prefixo, não o número):

| Prefixo | Janela |
|---|---|
| `HomePageX_QMLTYPE_` | tela inicial |
| `MainWindow_QMLTYPE_` | editor |
| `ExportWindow_QMLTYPE_` | exportação (título `Exportar-<nome>`) |
| `LVPopupWindow_QMLTYPE_` | lista aberta de um combo |

| Name | Onde | Uso |
|---|---|---|
| `HomePageDraftTitle:<nome>` (Text) | tela inicial | localizar o cartão; o pai `HomePageDraft` tem `HomePageDraftMouseArea` |
| `HomePageDraftMouseArea` | tela inicial | 1 clique real abre o rascunho (Invoke não abre) |
| `MainWindowTitleBarExportBtn` | editor | clique real abre a exportação |
| `ExportFileNameInput`, `ExportPathInput` | exportação | o filho Text de `ExportPathInput` tem o caminho final completo (com `nome(1).mp4` se já existir) — lido logo antes de exportar |
| `ExportSharpnessInput` | exportação | resolução: `480P` `720P` `1080P` `2K` `4K` `8K` |
| `ExportbitRateInput` | exportação | `Abaixar` `Recomendado` `Superior` `Personalizado` |
| `ExportEncoderInput` | exportação | `H.264` `HEVC` ... `AV1` |
| `ExportFormatInput` | exportação | `mov` `mp4` |
| `FrameRateInput` | exportação | `24fps` `25fps` `29.97fps` `30fps` `50fps` `59.94fps` `60fps` `120fps` |
| `automationcheckÁudio` | exportação | (não alterado) |
| `ExportOkBtn` | exportação | clique real exporta (Invoke não) |
| `automationcloseBtn`, `automationopenFolderBtn`, `publish_pc_btn` | exportação | aparecem na vista de sucesso |

Combo: clique real abre `LVPopupWindow`; os itens são Text com o Name do valor; clique real no item seleciona e fecha.
O valor atual do combo NÃO é legível por UI Automation: o resultado é conferido depois no arquivo (ffprobe). Valor
ausente na lista vira aviso e o CapCut usa o que estiver selecionado.

`ExportOptions`: `resolution='original'` = maior rótulo (até 4K) <= menor lado do vídeo (mínimo 480P);
`bitrate` low/recommended/high -> Abaixar/Recomendado/Superior; `fps='original'` = item mais próximo do fps da origem;
codec H.264 e formato mp4 sempre.

### Cliques com trava

O dono usa o PC ao mesmo tempo. Todo clique real (`_safe_click`): traz a janela alvo para a frente
(`SetForegroundWindow`; a partir da 3ª tentativa com um toque de Alt antes, como na prova de conceito, porque o
Windows só libera o primeiro plano depois de uma entrada), confirma que ela é a janela em primeiro plano e que
`GetAncestor(WindowFromPoint(centro), GA_ROOT)` é a própria janela; senão `ForegroundLost` (transitório) SEM clicar.
O cursor volta à posição anterior. Nenhuma tecla é enviada ao CapCut. Fechar projeto, janela de exportação e o CapCut
usa `WM_CLOSE` (sem mouse).

### Processo

- CapCut já aberto por outra pessoa/sessão: o núcleo não fecha nem mexe; levanta `CapCutBusy` e o runner espera.
- `launch()` só abre se não houver `CapCut.exe`. "Nosso" é só a árvore do processo que iniciamos: o PID do
  `Popen` do lançador e os descendentes, reconhecidos pelo PID pai E pelo horário de criação (filho criado depois do
  pai; PID reaproveitado por outro processo não conta). A árvore é atualizada a cada consulta, então segue o executável
  versionado mesmo depois de o lançador sair. Um CapCut aberto pelo dono por fora (mesmo depois de o nosso cair) nunca
  entra. Não verificado com o CapCut aberto: que o lançador `Apps\CapCut.exe` inicia o `Apps\<versão>\CapCut.exe`
  como processo filho (se não for, a tela inicial não é encontrada e o job falha com `ui_timeout`, sem tocar em nada).
- Janelas usadas na automação e no fechamento: só as dos PIDs dessa árvore.
- `shutdown()`: `WM_CLOSE` nas janelas da árvore, espera até 40 s; `taskkill /PID <pid> /F` em cada PID da árvore só
  como último recurso.
- Intermediário exportado: apagado na finalização se estiver em `%LOCALAPPDATA%\CapCut\Videos` e o caminho não
  existia antes da exportação. Se a tentativa falha depois de exportar, o caminho fica anotado em
  `<data_dir>/capcut/work/<job>/exports.json`; a tentativa seguinte (antes de abrir o CapCut), a conclusão e a exclusão
  do job apagam os anotados, só os que estão na pasta padrão de vídeos do CapCut e cujo nome começa pelo nome do
  rascunho do job (`MT_...` seguido de `.` ou `(`).
- Pasta de trabalho `<data_dir>/capcut/work/<job>`: removida ao concluir e ao excluir o job; em falha só fica se houver
  o `exports.json`.

## Finalização

ffprobe no exportado: precisa ter vídeo e duração dentro de ±0,5 s da origem (`output_invalid`, transitório);
resolução, fps e codec diferentes do pedido viram avisos. Com `keep_original_audio` e áudio na origem:
`ffmpeg -i exportado -i original -map 0:v:0 -map 1:a:0 -c copy -movflags +faststart` e conferência de
codec/sample_rate/canais do áudio final (os testes conferem o hash dos pacotes de áudio, bit a bit). Se o remux
falhar, fica o áudio do CapCut com aviso. Saída em `output_dir` com nome seguro do Windows e sufixo ` (2)`, ` (3)`...
(nunca sobrescreve).

## Erros

| code | transitório | quando |
|---|---|---|
| `capcut_not_installed` | não | sem lançador |
| `capcut_busy` | sim (espera sem gastar tentativa) | CapCut aberto por outro |
| `accessibility_unavailable` | não | sem árvore de UI Automation |
| `desktop_busy` | sim (espera de 1 min sem gastar tentativa, sem limite) | o dono usou teclado/mouse nos últimos 15 s antes de abrir o CapCut, ou tomou o foco durante a automação (entrada que não é nossa depois da nossa última entrada sintética) |
| `foreground_lost` | sim | outra janela na frente na hora do clique com o PC parado (a classe da janela que bloqueou vai na mensagem; nunca o título). Balões/dicas do PRÓPRIO CapCut que abrimos (fora editor, exportação, tela inicial e listas) são fechados com WM_CLOSE e o clique é refeito |
| `ui_timeout` | sim | janela/controle não apareceu a tempo |
| `export_failed` | sim | caminho ilegível, janela fechou antes de concluir |
| `output_invalid` | sim | exportado ausente/inválido/duração errada |
| `output_write_failed` | sim | sem permissão/arquivo em uso/disco ao gravar o vídeo final na pasta de saída |
| `ffmpeg_timeout`, `ffmpeg_failed` | sim | FFmpeg/ffprobe passou do tempo limite / não pôde ser executado |
| `process_query_failed` | sim | não deu para consultar os processos do Windows (tasklist/Toolhelp) |
| `source_missing`, `no_video_stream` | não | original ausente / sem vídeo |
| `transcription_failed` | não (sim ao carregar/baixar o modelo) | |
| `deps_missing` | não | faster-whisper/uiautomation/pywin32/skill ausentes |
| `draft_not_found`, `no_text_segment` | não | `extract_style` |
| `draft_invalid`, `invalid_draft_name`, `invalid_style`, `ffmpeg_not_found` | não | |
| `cancelled` | não | cancelamento cooperativo |

## Testes

```
cd backend
PYTHONDONTWRITEBYTECODE=1 ../.venv/Scripts/python.exe -m pytest -p no:cacheprovider tests/test_capcut_core_*.py
```

Sem rede, sem CapCut real, sem whisper real: instalação falsa em `tmp_path` (`MEDIA_TOOL_CAPCUT_ROOT`), rascunho-base
da fixture, vídeo sintético do FFmpeg (`testsrc` + `sine`), controlador e transcritor falsos.

## Limitações conhecidas

- Formato e nomes de interface verificados só no CapCut 9.7.1.4203 em português; outra versão/idioma pode quebrar a
  automação (os rótulos `Abaixar`/`Recomendado`/`Superior` são do português).
- Abrir o rascunho depende do cartão estar visível na tela inicial (o rascunho novo fica no topo da lista).
- Durante a exportação o CapCut vem para a frente e o mouse é usado por alguns segundos.
- Segmentos de texto que usam modelos de texto (`text_templates`) não entram na lista de modelos: só textos comuns
  (`materials.texts`).
- Keyframes do texto do modelo não são copiados.
- Rascunhos cifrados (versões que cifram `draft_content.json`) não são suportados.
