# Edição CapCut: núcleo técnico

Este documento descreve o pacote `backend/app/integrations/capcut/` (o "núcleo"): como um vídeo vira um rascunho do
CapCut Desktop com legendas, como o Media Tool exporta esse rascunho pela interface do CapCut e quais são os limites.
A fila, a API e a tela ficam em outros módulos (ver `docs/CONTRACTS.md`, seção 8.9 "Edição CapCut").

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

## Editor visual (design -> estilo)

O editor da tela grava um `design` (JSON versionado, `version: 1`; contrato em `docs/CONTRACTS.md` 8.9) e o backend o
transforma num `CaptionStyle` comum (`integrations/capcut/design.py`, `style_from_design`), que segue pelo mesmo motor
dos modelos extraídos (`apply_style`/`build_draft`). Ponto de partida: o design equivalente ao embutido
(`default_design`: `en.ttf` embutida, tamanho 7, escala 1,919, y -0,70, branco, negrito, contorno preto 30, destaque
`#FFD400`), devolvido em `GET /api/capcut/styles` no item id 0. Modelos do editor ficam em `capcut_styles` com
`source_draft = NULL`, `design` (migration 0030) e `style` = o estilo gerado (é ele que vai para o job).

Formatos gerados (verificados no CapCut 9.7.1 em 2026-11-07 com rascunho por arquivo + exportação, salvo indicação):

- Material de texto = o nativo do capcut-agent-skill, alterado. `check_flag` = 7 + 8 (contorno) + 16 (fundo) + 32
  (sombra).
- Fonte do catálogo do CapCut só pelo id: `content.styles[i].font = {"id": "<resource_id>", "path": ""}` e no material
  `font_id`/`font_resource_id` = id, `font_title`/`font_name` = nome, `font_path` = "". O CapCut baixa a fonte ao
  abrir o rascunho. Fonte do Windows ou embutida por caminho: `font = {"id": "", "path": "C:/..."}` e `font_path`
  igual (embutida: mesmo formato, não verificado à parte; o embutido antigo só usava `font_path`).
- Tamanho `styles.size`; cor `fill.content.solid.color` (`alpha` = opacidade) + `text_color`/`text_alpha`; negrito
  `styles.bold`; itálico/sublinhado `styles.italic`/`styles.underline` (+ `underline` no material; NÃO verificado no
  CapCut real).
- Contorno: `styles.strokes = [{alpha: 1, content: {render_type: solid, solid: {alpha: 1, color}}, width}]`, `width` =
  valor/100 x 0,2 (30 -> 0,06, o do skill); desligado: `strokes = []` sem o bit 8.
- Sombra: `styles.shadows = [{diffuse: blur/100/6, alpha, distance, angle, content: {solid: {color}}}]` + material
  `has_shadow`, `shadow_color`, `shadow_alpha`, `shadow_distance`, `shadow_angle`, `shadow_smoothing` = blur/100.
- Caixa de fundo: `background_style = 1`, `background_color`, `background_alpha`, `background_round_radius` (0..1),
  `background_height`/`background_width` 0,14, deslocamentos 0.
- Alinhamento `alignment` 0/1/2 (esquerda/centro/direita), `letter_spacing` = valor x 0,05, `line_spacing` = 0,02 +
  valor x 0,05, `line_max_width` (unidades do pyCapCut; não verificados um a um no CapCut real).
- Segmento: o padrão do motor com `clip.scale` x = y = `scale` e `clip.transform` = `position`.
- Animações (só pelo id; o CapCut baixa ao abrir): um material em `materials.material_animations`
  `{id, type: "sticker_animation", multi_language_current: "none", animations: [...]}` referenciado em
  `extra_material_refs`, cada animação `{id, type: in|out|loop, start, duration (µs), path: "", platform: "all",
  resource_id, third_resource_id, source_platform: 1, name, category_id: ruchang|chuchang|xunhuan, category_name:
  Entrada|Saída|Loop, panel: "", material_type: "sticker", anim_adjust_params: null, request_id: ""}`. Com destaque por
  palavra, `apply_style` põe a entrada só no 1º pedaço do bloco e a saída só no último (início = duração do pedaço -
  duração da animação). Verificado no real: entrada (Fade-in). Saída e loop: mesmo formato, não exportados no real.
- `uppercase`: o texto (e `words`) de cada bloco vai em maiúsculas (`CaptionStyle.uppercase`).
- Destaque: o mecanismo atual (`HighlightOptions`) com a cor do design (`CaptionStyle.highlight_color`); destaque
  desligado no design (`CaptionStyle.highlight_enabled = False`) desliga o destaque mesmo com a configuração ligada.

Validação: o schema (pydantic) confere formato e faixas (cores `#RRGGBB`, `size` 2..30, `scale` 0,3..4, opacidades e
raio 0..1, contorno/desfoque/distância 0..100, ângulo -180..180, posição -1..1, `max_line_width` 0,1..1, espaçamentos
-20..100, animação 100..10000 ms); o núcleo (`validate_design`) confere a fonte (`windows`: arquivo .ttf/.otf existente
dentro das pastas de fontes do Windows; `bundled`: dentro de `Resources\Font` do CapCut; `capcut`: id presente no
catálogo local) e as animações (id presente no catálogo com o tipo pedido). Falha -> 422 `invalid_design`.

### Catálogo local (`integrations/capcut/catalog.py`, sem rede)

- Fontes embutidas: `Apps\<versão>\Resources\Font\SystemFont\*.ttf|otf` (`key` `bundled:<arquivo>`).
- Fontes do Windows: registro HKLM e HKCU `SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts` (nome sem
  "(TrueType)"/"(OpenType)"), arquivo relativo a `%WINDIR%\Fonts` ou absoluto; só .ttf/.otf que existam dentro de
  `%WINDIR%\Fonts` ou `%LOCALAPPDATA%\Microsoft\Windows\Fonts` (`key` `windows:<arquivo>`). Coleções `.ttc` ficam
  fora (o navegador não as usa no @font-face).
- Catálogo do CapCut: cada `User Data\Cache\ressdk_db\<hash>\rp.db` é COPIADO (com `-wal`/`-shm`) para uma pasta
  temporária e lido de lá (o original nunca é aberto); só as respostas `get_panel_info` da tabela `http_cache` (texto
  ou zlib). Fontes: itens `common_attr.effect_type == 10` (`key` `capcut:<id>`). Animações: itens `effect_type == 18`
  dentro de `data.category_resources[<categoria>].effect_item_list`, classificados pela chave da categoria do painel
  (`ruchang` = entrada, `chuchang` = saída, `xunhuan` = loop) e só quando todas as categorias do item apontam um único
  tipo e o `text_animation.animation_type` dele (se houver) concorda. Ficam FORA: `caption_animation` ("Legenda") e
  `text_animation` (tipo `caption`) e itens com sinais conflitantes. Nos bancos desta máquina (2026-11-07): 6.705
  fontes (3.342 Pro), 2.226 animações únicas, 1.727 classificadas (in 618, out 584, loop 525) e 499 fora.
- Pro: `business_info.json_str` com `is_vip` ou `paid_type` diferente de `free` (senão `extra.is_vip`). A API esconde
  Pro por padrão (`include_pro=1` mostra). Duração padrão: `text_animation.duration` (ms), senão
  `sdk_extra.setting.animation_duration` (s), senão 500 ms. Baixado: existe `Cache\effect\<id>\<md5>\` (pasta que
  não termina em `_tmp`, com arquivos); fonte baixada = primeiro .ttf/.otf dentro dela. `cover_url` não é usado.
- Cache: em memória e em `<data_dir>/capcut/catalog_cache.json`, por banco, invalidado pelo mtime/tamanho do `rp.db`
  e do `-wal`; só os bancos que mudaram são copiados de novo (a 1ª leitura dos bancos desta máquina, ~550 MB, levou
  ~4,5 s). Banco ilegível/em gravação: fica a leitura anterior (ou vazio) até o arquivo mudar.
- `GET /fonts/file?key=` só serve arquivos de `%WINDIR%\Fonts`, `%LOCALAPPDATA%\Microsoft\Windows\Fonts`,
  `Resources\Font` do CapCut e `Cache\effect\<id>` já baixado: a chave tem só um nome de arquivo (sem separadores,
  `..`, `:` ou NUL), o caminho é resolvido (links seguidos) e precisa ficar dentro da pasta permitida; senão 404.

### Prévia real

`POST /api/capcut/previews` põe uma prévia na MESMA fila/executor dos jobs (um CapCut por vez), em memória (fora do
banco e da lista de jobs; some ao reiniciar o app), com prioridade sobre os jobs comuns (roda mesmo com a fila pausada)
e sem mexer em tentativas. Uma prévia nova cancela as prévias ainda na fila. Mídia padrão: o vídeo original mais recente
da Biblioteca. O executor monta um `CapCutJobSpec` com `clip_start`/`clip_duration` (trecho `start_s .. start_s +
seconds`, 3..12 s; início além do fim = os últimos segundos), `sample_text` "Exemplo de legenda no seu estilo",
`resolution` 720P, `fps` 30, `keep_original_audio=False` (sem remux), rascunho `MT_preview_<id>` e saída
`<data_dir>/capcut/previews/<id>.mp4` (ficam os 10 vídeos mais recentes). O pipeline é o mesmo dos jobs (espera de PC
livre, trava de clique, fechamento seguro, limpeza do rascunho); com recorte:

- `build_draft(..., clip=(início, duração))`: o segmento de vídeo usa `source_timerange = {start: início, duration:
  duração}` e `target_timerange = {0, duração}`; a duração do rascunho é a do trecho; o material continua sendo o
  arquivo inteiro.
- Transcrição só do trecho: FFmpeg separa o áudio do trecho (`clip.wav` mono 16 kHz na pasta de trabalho, apagado logo
  depois) e o whisper transcreve só ele (tempos já relativos ao trecho). Sem fala: o texto de exemplo distribuído no
  trecho com tempos sintéticos, em blocos pelas mesmas regras.
- Conferência da exportação e `finalize_output(..., expected_duration=duração do trecho)`.
- CapCut ocupado / PC em uso -> `waiting` (nova checagem em 30 s / 60 s); outro erro -> `failed` sem nova tentativa.

## Transcrição e blocos

FFmpeg decodifica para PCM float32 16 kHz mono (o decodificador PyAV do faster-whisper falha com o av 19:
`open() got an unexpected keyword argument 'metadata_errors'`). faster-whisper com `language='pt'`,
`word_timestamps=True`, `vad_filter=True`, `condition_on_previous_text=False`, `beam_size=5`, CPU `int8`. Modelos
`large-v3` (padrão), `large-v3-turbo`, `medium`, `small` (ver "Fidelidade das legendas"), baixados no 1º uso para `<data_dir>/models/whisper` (cache do
Hugging Face); um modelo carregado por tamanho fica em memória (`release_models()` libera). Cancelamento entre
segmentos. Sem fala: o job termina com cópia do original, sem legendas, e um aviso.

`chunk_words(words, max_words=3, max_chars=24, pause_split=0.18, min_duration=0.30, gap=0.02)`: quebra em pausa
>= 180 ms e depois de `.`, `!`, `?`, `…`; ao estourar o limite, prefere quebrar depois de `,`/`;`/`:` e não deixa
preposição/artigo curto (de, da, do, das, dos, a, o, as, os, e, em, no, na, com, pra, para, que, um, uma) sozinho no
fim do bloco; fim = última palavra + 50 ms, nunca além de (próximo - 20 ms); duração mínima 300 ms sem invadir o
próximo; texto com espaços simples. Bloco com menos de 350 ms de fala pisca na tela (verificado: "Ei," ficava 0,07 s): o corte na vírgula não deixa para trás bloco tão curto, e um bloco curto se junta ao vizinho quando cabe nos limites configurados (palavras e caracteres), sem atravessar fim de frase nem pausa longa.

## Fidelidade das legendas

**Causa verificada** (2026-11-07, Reel com diálogo rápido, duas vozes e ruído): as legendas saíam diferentes do que
é falado. Na janela de 30 s o Whisper completa o que ouve com o contexto: inventou "Esse é o meu laranjinha" (repetição
de uma frase posterior) espremido em 0,36 s, com 4 palavras de duração zero, e trocou palavras ("Gerson" por "mesmo").
Os tempos das palavras ficam presos ao fim do segmento (palavras amontoadas no mesmo instante). Retranscrever o trecho
sozinho consertou uns erros e criou outros: nenhuma configuração automática é confiável nesse tipo de áudio. Por isso
o trecho duvidoso não é reescrito às cegas; ele vai marcado para o dono revisar.

**Alinhamento forçado** (`integrations/capcut/align.py`): wav2vec2 XLSR-53 em português (CTC, licença Apache-2.0)
exportado para ONNX e rodado com `onnxruntime` na CPU, em `<data_dir>/models/aligner/wav2vec2-xlsr-53-pt`
(`model.onnx` + `vocab.json`). Dado o texto, acha onde cada palavra está no áudio (Viterbi sobre as letras). Na
transcrição, o tempo de cada palavra vem do alinhador quando a nota acústica é boa; senão fica o do Whisper,
consertado (ordem crescente, sem duração zero, amontoadas redistribuídas pelas sílabas). Sem o modelo instalado, só o
conserto; o status (`GET /api/capcut/status`) informa `aligner`.

**Marcações de dúvida** (`transcribe.mark_doubts`): cada palavra leva `seg` (a frase do Whisper, unidade da revisão)
e `doubt`. A frase inteira fica duvidosa quando tem 2+ palavras de duração ~0, ritmo impossível (mais de 9 sílabas por
segundo) ou confiança média abaixo de 0,60; fora isso, só a palavra com confiança abaixo de 0,6. Transcrições antigas
(sem `seg`/`doubt`) continuam válidas: tudo na frase 0, sem dúvidas.

**Revisão e realinhamento**: configuração `review_mode` (`doubtful` padrão, "Só quando houver trechos duvidosos";
`always`, "Sempre"; `never`, "Nunca, exportar direto"), no instantâneo do job e sobrescrevível no lote; job antigo sem
a chave = `never`. Num job com legenda (`caption`/`edit_caption`) ainda não aprovado, o executor primeiro só transcreve
(`transcribe_only`, sem CapCut). Com `always`, ou `doubtful` e alguma palavra com `doubt`, o job para em
`waiting`/`wait_reason='review'` ("Aguardando revisão das legendas") sem gastar a tentativa e o executor não o pega
até a aprovação; sem necessidade, segue direto para o CapCut com a transcrição já pronta. A revisão é etapa do dono,
não falha: não conta tentativa nem dispara nova tentativa; sobrevive ao reinício; cancelar funciona como em qualquer
job na fila. Na tela "Revisar legendas" o dono ouve cada frase (▶ toca só o trecho, do arquivo exato que foi
transcrito), corrige o texto e aprova. Cada frase alterada é separada em palavras e realinhada no áudio dentro da
janela [início da frase - 0,15 s (nunca antes do fim da frase anterior), início da próxima frase (a última: fim + 0,6
s, limitado à duração)] por `align_text`: alinhamento forçado quando o modelo existe; senão as palavras são repartidas
pelas sílabas. Texto vazio remove a frase. Todas as dúvidas são limpas, `options.review = {approved, at, edited}` é
gravado e o job volta para a fila.

**Fonte da transcrição**: ao gravar a transcrição, `options.transcript_source = {path, size, mtime_ns}` do arquivo
transcrito (original da mídia ou rendition da edição). Ao rodar, se a fonte atual for outra (edição refeita, arquivo
trocado), a transcrição e a aprovação são descartadas e o vídeo é transcrito de novo: legenda de um arquivo nunca vai
para outro.

**Modelos**: `large-v3` é o padrão ("Máxima precisão", ~3 GB, mais lento); `large-v3-turbo` ("Rápido", ~1,6 GB),
`medium` ("Equilibrado") e `small` ("Mais rápido") continuam como opção nas configurações.

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

### Cliques com trava (modo interativo)

Vale só para o modo `interactive` (no modo isolado, padrão, os cliques são mensagens; ver "Modo de exportação
isolado"). O dono usa o PC ao mesmo tempo. Todo clique real (`_safe_click`): traz a janela alvo para a frente
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
  entra. Verificado em 2026-11-07: o lançador `Apps\CapCut.exe` inicia o `Apps\<versão>\CapCut.exe` como processo
  filho, e as janelas do editor e da exportação pertencem a esse filho.
- Janelas usadas na automação e no fechamento: só as dos PIDs dessa árvore.
- Antes de fechar o projeto, o pipeline espera o MP4 ficar FINALIZADO e íntegro (`finalize.export_problems`, até
  180 s): ninguém está gravando o arquivo (abrir aceitando só outros leitores funciona; o CapCut mantém o MP4 aberto só para leitura enquanto o projeto está aberto, e isso não impede), o ffprobe lê o contêiner com vídeo, a
  duração bate com a origem (±0,5 s) e o último segundo decodifica sem erro. Senão `output_invalid` (transitório).
- Fechamento (`close_project()` e `shutdown()`): `WM_CLOSE` nas janelas da árvore. Se o CapCut mostrar o aviso modal
  (QML `modalDialog`, nomes acessíveis `mainText: <título>` / `subText: <detalhe>`, ex.: "Confirmar saída? Uma tarefa
  está em andamento. Deseja sair?"), o Media Tool NUNCA confirma: responde com o botão de cancelar (`Cancelar`,
  `Não`, `Voltar`...; Invoke e, se preciso, clique travado) ou fecha só a janela do aviso, espera e pede o fechamento
  de novo a cada 15 s, por até 10 min. Sem aviso, o pedido é repetido a cada 5 s até o prazo (o 1º só leva do editor
  à tela inicial). Se a tarefa não terminar, o CapCut fica ABERTO e o job registra um aviso; a fila
  espera (`capcut_busy`) até alguém fechá-lo. NÃO há `taskkill`: o CapCut nunca é forçado a fechar.
- Intermediário exportado: apagado na finalização se estiver em `%LOCALAPPDATA%\CapCut\Videos` e o caminho não
  existia antes da exportação. Se a tentativa falha depois de exportar, o caminho fica anotado em
  `<data_dir>/capcut/work/<job>/exports.json`; a tentativa seguinte (antes de abrir o CapCut), a conclusão e a exclusão
  do job apagam os anotados, só os que estão na pasta padrão de vídeos do CapCut e cujo nome começa pelo nome do
  rascunho do job (`MT_...` seguido de `.` ou `(`).
- Pasta de trabalho `<data_dir>/capcut/work/<job>`: removida ao concluir e ao excluir o job; em falha só fica se houver
  o `exports.json`.

## Modo de exportação isolado

Configuração `export_mode` (Edição CapCut > Configurações avançadas): `isolated` (padrão) ou `interactive` (a automação de
"Cliques com trava", preservada como alternativa). O modo é lido quando o job/prévia COMEÇA (não entra no instantâneo
do job, para que trocar o modo valha na nova tentativa) e o modo usado fica gravado no job (`options.export_mode`,
`export_mode` na API) e na prévia (`export_mode`). Código em `integrations/capcut/isolated.py`.

Como funciona:
- Área de trabalho própria `MediaToolCapCut` (`win32service.CreateDesktop("MediaToolCapCut", 0, GENERIC_ALL, None)`;
  se já existir, é aberta). O dono não a vê e ela não troca a tela dele.
- O CapCut é aberto com `win32process.CreateProcess(lançador, ..., STARTUPINFO(lpDesktop="WinSta0\MediaToolCapCut"))`;
  os processos filhos (o `Apps\<versão>\CapCut.exe`) herdam a área. A árvore de processos é a mesma do modo
  interativo (PID + horário de criação).
- Todas as operações do controlador rodam numa thread dedicada (`DesktopThread`) que chama `SetThreadDesktop` ANTES de
  qualquer janela/COM e depois `pythoncom.CoInitialize()`; o objeto IUIAutomation do `uiautomation` (um por processo)
  é zerado ao entrar e ao sair dessa thread, para nascer no apartamento COM dela. O runner chama de outra thread por um
  proxy (`IsolatedControllerProxy`) que encaminha cada chamada; o andamento (`progress`) volta para a thread do runner.
- O controlador isolado (`IsolatedCapCutController`) é uma subclasse do `CapCutController` e reaproveita tudo: escolha
  das opções de exportação, conferência do MP4, fechamento seguro com o aviso "Confirmar saída" (nunca confirma, nunca
  força), árvore de processos. Muda só: janelas listadas por `EnumDesktopWindows` da área; clique =
  `PostMessage(hwnd, WM_MOUSEMOVE / WM_LBUTTONDOWN(MK_LBUTTON) / WM_LBUTTONUP, lParam = coordenadas da área CLIENTE)`;
  rolagem = `PostMessage(hwnd, WM_MOUSEWHEEL, (delta & 0xFFFF) << 16, coordenadas de TELA)`; maximizar =
  `ShowWindow(SW_MAXIMIZE)` (na área isolada não afeta o dono). Nada de `SetCursorPos`, `mouse_event`,
  `keybd_event`, `SetForegroundWindow` nem espera de PC livre (`ensure_desktop_free` não se aplica; não existe
  `desktop_busy` neste modo). Sem captura de tela de diagnóstico (ela mostraria a tela do dono).
- Ao final: `shutdown()` (fechamento seguro), a thread é encerrada e a área é fechada (`CloseDesktop`). Pastas
  temporárias e intermediários seguem a limpeza de sempre.
- Sem fallback: se a área não puder ser criada, a thread não puder ser presa a ela, a UI Automation não responder nela
  ou a interface do CapCut não ficar acessível nela, o job FALHA com `isolated_unavailable` (não transitório; a
  mensagem sugere trocar o modo para "Interativo" nas configurações). Nunca cai sozinho para o interativo.
- Sobra de queda do Media Tool: antes de conferir se o CapCut está aberto, se houver `CapCut.exe` rodando, a área
  `MediaToolCapCut` existir (`OpenDesktop`) e TODAS as janelas desses processos estiverem nela (nenhuma na área normal
  `Default` do dono), o CapCut é tratado como nosso e fechado com o fechamento seguro, numa thread presa à área (vale
  nos dois modos). Se ele não fechar (tarefa em andamento), `capcut_busy` (espera). CapCut com janela na área normal
  (aberto pelo dono) continua sendo `capcut_busy` e nunca é tocado.
- Status (`GET /api/capcut/status`): `export_mode` e `isolated: {available, reason}` = pré-requisitos presentes
  (Windows + pywin32 + pythoncom + uiautomation). Não cria a área: a prova real acontece no job.

Evidências (2026-11-07, Windows 11 Home, CapCut 9.7.1, protótipo com as mesmas chamadas, nesta máquina): pipeline
inteiro nesse modo (abrir o rascunho, Exportar, combos de resolução/taxa/codec/formato/60 fps, `ExportOkBtn`,
conferência do MP4, fechamento seguro e limpeza). Resultado: MP4 1080x1920, 60/1 fps, duração certa, áudio idêntico
ao original (hash dos pacotes), legendas com destaque. Um monitor do primeiro plano da sessão do dono registrou 0 de
436 amostras com o CapCut na frente. Mais duas exportações de diagnóstico rodaram nesse modo sem problema. A tela
inicial na área isolada expôs 23 controles por UI Automation.

Limitações:
- O CapCut é de instância única. Se o dono abrir o CapCut enquanto a automação usa o dela na área oculta, o CapCut do
  dono pode não aparecer (o pedido pode ir para a instância que está na área isolada). O que fazer: esperar o
  job/prévia terminar (o Media Tool fecha o CapCut dele no fim) ou pausar a fila e cancelar o job em andamento; depois
  abrir o CapCut de novo. Se o dono precisa usar o CapCut com frequência durante a fila, o modo "Interativo" evita
  isso (mas usa a tela e precisa do PC livre).
- Só Windows. O código de produção deste modo foi testado só com fakes; o comportamento real verificado é o do
  protótipo acima.

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
| `isolated_unavailable` | não | modo isolado não pôde ser usado (área de trabalho, thread presa a ela, UI Automation ou interface do CapCut inacessível nela); sem fallback, a mensagem sugere trocar o modo para "Interativo" |
| `invalid_export_mode` | não | `export_mode` desconhecido na especificação do job |
| `desktop_busy` | sim (espera de 1 min sem gastar tentativa, sem limite) | só no modo interativo: o dono usou teclado/mouse nos últimos 15 s antes de abrir o CapCut, ou tomou o foco durante a automação (entrada que não é nossa depois da nossa última entrada sintética) |
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

Editor visual: `tests/test_capcut_editor_core.py` (catálogo a partir de um `rp.db` sintético mínimo, design ->
material, validação), `tests/test_capcut_editor_api.py` (API, prévias com processador falso, migração 0030) e
`tests/test_capcut_editor_preview.py` (pipeline da prévia com recorte); fakes em `tests/capcut_editor_fakes.py`
(registro e pastas de fontes do Windows também falsos).

Modo isolado: `tests/test_capcut_isolated.py` (controlador isolado com janelas/UI Automation falsas: só `PostMessage`
e `ShowWindow`; `SetCursorPos`, `mouse_event`, `keybd_event`, `SetForegroundWindow`, `EnumWindows` etc. falham o
teste se chamados; tudo na thread presa à área; recuperação de sobra; escolha do modo e ausência de fallback no
pipeline) e `tests/test_capcut_export_mode.py` (configuração, status, registro do modo no job/prévia, avisos do
design na API).

## Queda, cancelamento e uso simultâneo (validado no real em 2026-11-07, instância de teste)

- **Queda do Media Tool durante a exportação isolada:** o job volta para a fila sem gastar tentativa; na tentativa
  seguinte, `isolated.recover_leftover` fecha com o fechamento seguro o CapCut órfão — só a ÁRVORE de processos de quem
  tem janela na área `MediaToolCapCut`; se houver qualquer outro CapCut fora dessa árvore (ex. o do dono), nada é tocado
  e o job espera (`capcut_busy`). Depois, `finalize.discard_job_exports` apaga da pasta de vídeos do CapCut só os
  arquivos `MT_<job>...` desse job (ex. a exportação que o órfão terminou antes de fechar). Real: concluiu sem sobras.
- **Cancelamento durante a exportação:** o fechamento seguro roda com o cancelamento desligado (`_closing`): se o
  CapCut avisar "tarefa em andamento", espera a exportação terminar e fecha, sem forçar; em seguida o rascunho e o
  vídeo intermediário do job são apagados (antes o cancelamento abortava o fechamento e deixava CapCut, rascunho e
  vídeo sobrando). Real: cancelado em ~60 s com tudo limpo; "Tentar de novo" concluiu.
- **CapCut do dono aberto antes:** jobs ficam em `waiting` (`capcut_busy`) sem gastar tentativa e sem tocar nele;
  quando ele fecha, a fila roda um job por vez (real: 2 jobs em sequência).
- **Dono abre o CapCut durante a exportação isolada:** o CapCut só abre uma vez; o pedido do dono vai para a
  instância oculta da automação e NADA aparece para ele (verificado). A sessão isolada vigia processos `CapCut.exe`
  fora da sua árvore criados depois do launch; ao detectar, o vídeo atual termina normalmente e a fila é PAUSADA com
  o motivo (`queue.pause_reason`, mostrado na página), liberando o CapCut para o dono; ele retoma a fila quando
  terminar. Limitação: a tentativa do dono durante aquele vídeo (até ~1–2 min) não abre nada; precisa abrir de novo.

## Lotes

Pedido em massa da página Edição CapCut (`/edicao-capcut`; `/legendas` redireciona; migration 0031; contrato em
`docs/CONTRACTS.md` 8.9). Código:
`services/capcut_batch_service.py` (criação, apresentação, ações, ZIP, seletor), `db/repositories/capcut.py` (tabela
`capcut_batches`, contagens e status derivado em SQL), `jobs/capcut_runner.py` (fonte editada e modo só edição). Não
há fila paralela: o lote é um `capcut_batches` com um `capcut_jobs` por vídeo, na MESMA fila e no MESMO executor dos
jobs avulsos (um por vez, ordem por id, então os jobs de um lote ficam contíguos). Jobs antigos e os criados por
`POST /jobs`, "Legendar no CapCut" da Biblioteca e o automático após o download continuam `mode = caption`, sem lote.

Modos (`mode` do lote e de cada job; na página: Legendar = `caption`, Editar = `edit`, Editar e legendar =
`edit_caption`):

- `caption`: só legendas, fonte = original da mídia (igual ao job avulso).
- `edit_caption`: na criação, UM lote `batches` de edição (mesma forma do `edit_service.start`: `source = library`,
  `preset_id` NULL, `preset_name = "Edição"`, `process = 1`) com um job `process` por vídeo; o capcut job guarda
  `edit_job_id`. O worker edita em paralelo à fila do CapCut. `claim_next` só pega o job quando o job de edição está
  em estado final (`completed`/`failed`/`cancelled`; job de edição inexistente não segura). Edição concluída: fonte
  = arquivo da rendition (`jobs.rendition_id` -> `renditions.processed_path`). Edição com erro ou cancelada: o job
  termina `failed` `edit_failed` ("A edição deste vídeo falhou: <erro>"), sem nova tentativa. Rendition sem arquivo:
  `source_missing`. O dedupe de renditions do worker continua (mesma mídia + mesmos params reaproveitam a rendition).
  Se o job de edição tiver sido apagado do histórico, a rendition é procurada pelos params gravados em
  `options.edit_params` do job.
- `edit`: igual, mas o CapCut NÃO é aberto (não depende dele nem do núcleo instalado): a rendition é copiada para a
  pasta do lote como `<nome>_editado.mp4` (`<nome>_editado (2).mp4` e assim por diante, sem sobrescrever; cópia em
  `.part` e troca atômica), `output_probe` = dados da rendition (largura, altura, duração, fps, áudio), `progress`
  100. Falha na cópia: `failed` `copy_failed`. A pausa global da fila também segura esses jobs.

Params da edição: base = params do preset escolhido (`edit.preset_id`) ou, sem preset, os da edição
(`metadata_cleanup`, `mirror` e `auto_adjust` desligados, como `edit_service.edit_params`); por cima: `speed`
(0,5 a 2,0), `sharpen`, `max_duration` = limite + 0,03 s (`EDIT_DURATION_MARGIN`; `original` = sem limite),
`reencode = true` (o CapCut precisa de MP4) e `audio_sync_start = true`. Validados pelo `PresetParams`; o início do
corte (`start_seconds` -> `trim_start`, até 86400 s) é gravado depois da validação, como no `edit_service`. Sem preset
e sem nenhuma mudança: 422 `edit_no_changes`.

Legendas e exportação (modos com legenda): modelo (`caption.style_id`, 0 = embutido) e `caption.overrides` (as mesmas
opções do job avulso: resolução, fps, qualidade, manter o áudio original), com o mesmo instantâneo por job. Saída na
pasta do lote com o nome atual `<nome>_legendado.mp4`. Resolução/FPS/qualidade não se aplicam ao modo `edit` (o
resumo do lote traz esses campos nulos). `keep_original_audio` vai no instantâneo; no `edit_caption` o "áudio
original" é o do vídeo editado (a fonte do CapCut).

Pasta do lote: `<pasta de saída>/Lote <id>` (absoluta, criada quando o primeiro vídeo termina). Nome padrão
"Lote <id> · dd/mm HH:MM" (fuso das exportações), editável no pedido. `options` do lote = instantâneo do pedido:
`mode`, `include_done`, `output_dir`, `selection` (quantidade de ids ou o filtro), `caption` (opções de legenda sem o
estilo serializado), `keep_original_audio`, `edit` (params, preset, duração, início, velocidade, nitidez, resumo) e
`summary` (resumo legível).

Seleção: `media_ids` OU `filter` = `{q, profile, folder (chave de pasta do Editar), captioned: any|yes|no}`, resolvido
no servidor (todos os filtrados). Só vídeos originais (sem capas). Teto de 1000 vídeos por lote (422
`batch_too_large` com a contagem, conferido antes de pular duplicados). Duplicação: pula mídia com job das Legendas
aberto em qualquer modo (`already_queued`) e mídia com job concluído no mesmo `mode` (`already_done`;
`include_done: true` reprocessa). "Já legendado" (`captioned`, marca do seletor) = job concluído em `caption` ou
`edit_caption`. Nenhum vídeo criado: 422 `batch_empty` e nada é gravado.

Pausas: lote pausado (`paused = 1`): o executor não pega jobs dele (o que está rodando termina); a edição no worker
continua. Retomar acorda o executor. Pausa individual (`held = 1`) só em job `pending`/`waiting` (409
`capcut_job_not_holdable` em execução ou encerrado). Nenhuma das duas mexe na pausa global (`pause_reason` continua
igual).

Cancelar o lote: grava `cancel_requested` (o executor para de pegar jobs do lote), cancela na hora os `pending`/
`waiting`, o job em execução recebe o cancelamento individual de sempre (sinal ao núcleo, fechamento seguro do
CapCut) e o lote de edição, se ainda aberto, é cancelado pelo cancelamento de lote do worker
(`import_service.cancel_batch`). Concluídos ficam. Lote sem job aberto: 409 `capcut_batch_finished`, nada é
gravado. Cancelar (lote ou job) zera a pausa individual. Cancelar UM job do lote não cancela a edição dele. Reprocessar
falhas: `failed`/`cancelled` do lote voltam para a fila (tentativas zeradas, transcrição mantida, `cancel_requested`
do lote limpo, pausa individual zerada); se a edição do job falhou, foi cancelada, está aberta com cancelamento
pedido, sumiu ou concluiu sem a rendition (linha ou arquivo), um novo job `process` com os mesmos params entra no
mesmo lote de edição (reaberto) e `edit_job_id` passa a apontar para ele. "Tentar de novo" de um job do lote segue a
mesma regra. Concluídos nunca são refeitos.

Status derivado (contagens: `pending`/`waiting` sem pausa individual; `held` = na fila com pausa individual): sem job
aberto, `importing` (lote de uma importação ainda ativa, ver "Lote da importação" abaixo), `cancelled` (cancelamento pedido ou todos cancelados), `completed_with_errors` (algum com erro ou cancelado)
ou `completed`; com job aberto, `cancelled` (cancelamento em andamento), `running` (há job em execução), `paused`
(lote pausado ou só restam jobs com pausa individual) ou `running`. `finished_at` é gravado quando não resta job
aberto e limpo quando volta a ter (executor ao fim de cada job, ações da API e recuperação no startup). `progress` =
média do `progress` dos jobs (concluído = 100).

Estimativa: média real de `finished_at - started_at` dos concluídos do lote; sem nenhum, dos últimos 20 concluídos do
mesmo modo (qualquer lote ou avulso); sem histórico, null. `eta_seconds` = média x abertos - tempo já decorrido do
job em execução (mín. 0). `started_at` é o da primeira vez que o job foi pego, então esperas de "CapCut ocupado" e
tentativas entram na média. `GET /estimate?mode=&count=` usa a média do modo para a seleção antes de criar.

ZIP (`GET /batches/{id}/zip`): os MP4 concluídos do lote que ainda existem (só dentro da pasta registrada no job),
sem compressão, num arquivo temporário `<data_dir>/tmp/capcut-lote-<id>-<hex>.zip` apagado depois do envio (e na
falha); nomes repetidos ganham " (2)". Antes, a mesma conta de espaço do ZIP da exportação (`services/export_zip.py`:
soma + margem contra o livre menos a reserva dos downloads e os ZIPs na fila): sem espaço, 507 `export_no_space`.
Sobras `capcut-lote-*.zip` (ex.: envio interrompido) são apagadas no startup (`recover_capcut_jobs`).
Remover do histórico (`DELETE`) só oculta o lote (`hidden = 1`); os arquivos ficam. O lote de edição aparece também
no histórico da página Editar (é um lote de edição comum).

Recuperação: o lote, o instantâneo e o vínculo `edit_job_id` estão no banco; a recuperação de sempre devolve o job
`running` para a fila e a do worker cuida da edição. Queda no meio do cancelamento de um lote (`cancel_requested = 1`
gravado, jobs ainda abertos): no startup os jobs abertos desse lote terminam `cancelled` (no startup nada está
rodando, então isso vale também para o que estava `running`); "Reprocessar falhas" limpa o cancelamento do lote.

Lista do seletor ("Limpar lista", sem migração): a visão fica em `app_state`, chave `capcut_library_view`, JSON
`{"cleared_before_id": int|null, "hidden": [ids], "shown": [ids]}`. Um vídeo é visível quando (`cleared_before_id`
é null OU `id > cleared_before_id` OU id em `shown`) E id fora de `hidden`. Vale para `GET /library` (itens e
`total`) e para o `filter` do lote ("todos os filtrados"); `media_ids` explícitos NÃO passam pela visão. `POST
/library/clear` sem `media_ids` grava `cleared_before_id` = maior id de mídia existente e zera as listas (os novos
downloads e envios têm id maior e continuam aparecendo); com ids, eles entram em `hidden` e saem de `shown`. `POST
/library/show` faz o contrário (entram em `shown`, saem de `hidden`): a página chama para o envio do computador que
volta como duplicata de um vídeo antigo que saiu da lista. `POST /library/restore` apaga a chave (tudo volta). As
listas não repetem ids e guardam no máximo 20.000 (acima disso ficam os mais recentes); a consulta usa `json_each`
(sem um parâmetro por id). Valor ausente ou inválido = visão vazia. Nada disso apaga mídia: a caixa "Também apagar
os arquivos da biblioteca" da confirmação usa a exclusão comum (`POST /api/media/delete`, até 500 ids por pedido,
então a página manda em blocos). Antes de limpar, a página lê a lista visível inteira (para "Limpar lista" apagar só
o que estava visível e para saber quem tem job do CapCut aberto); vídeos com job do CapCut aberto não são enviados
para exclusão. A exclusão comum só pula mídia com job do worker em andamento ou exportação aberta (não olha os jobs do
CapCut), por isso o filtro fica na página.

### Lote da importação ("Edição CapCut" após baixar)

Na Importação, o pós-processamento "Edição CapCut" (campo `capcut` de `POST /api/imports`, `/imports/start` com
`post_processing: "capcut"`, `/import-groups` e `/import-links`; contrato em `docs/CONTRACTS.md` 8.9) manda cada vídeo
novo baixado para um lote desta página. Sem migração: o vínculo fica em `app_state` e a origem no `options` do lote.

- Pedido: `capcut = {mode, caption?, edit?, name?}`, o mesmo formato de `POST /batches` sem a seleção (`media_ids`,
  `filter`, `include_done`). Exclusivo com o preset e com `edit` (422 `capcut_with_post_processing`). A configuração é
  validada na criação da importação pela mesma função do lote da página (`capcut_batch_service.prepare`), com os
  mesmos erros (`edit_required`, `edit_no_changes`, `style_not_found`, `preset_not_found`, `invalid_preset`...), antes
  de criar qualquer lote. Na importação unificada, "Somente capas" e "Somente fotos" ignoram a escolha (nada é
  editado), e a exportação automática "editados"/"ambos" é recusada (422 `auto_export_capcut`: os editados ficam no
  lote da Edição CapCut, não na exportação da importação).
- Criação (na mesma transação da importação): UM lote `capcut_batches` vazio por pedido (perfil, grupo e links da
  importação unificada dividem o mesmo), nome padrão "Importação dd/mm HH:MM" ou `name`, pasta `Lote <id>` e o mesmo
  instantâneo de `options` do lote da página, mais `selection = {"import": true}`, `source = {"kind": "import",
  "batch_ids": [...], "group_id": n|null}` e `job_options` (as opções completas dos jobs, com o modelo de legenda;
  fora da resposta da API). Vínculos em `app_state`: `capcut_import_link:batch:<id>` para cada lote de importação
  avulso (perfil único e links) e `capcut_import_link:group:<id>` para o grupo (vale para todos os filhos, inclusive
  os criados depois). Valor = id do lote CapCut. A resposta da criação traz `capcut_batch_id`.
- Vídeo novo baixado (`pipeline._queue_capcut` -> `capcut_service.enqueue_after_download`): se o lote de importação
  do job (ou o grupo dele) tem vínculo, `capcut_batch_service.add_import_video` adiciona o vídeo pelo mesmo caminho de
  `create_batch` por vídeo (`_add_video`: job `process` da edição nos modos com edição + job do CapCut, `origin =
  auto`, `batch_id` = lote de importação). O lote de edição (`batches`) é criado no primeiro vídeo que precisa dele
  (e reaberto se a edição dos anteriores já terminou). Pulados: lote CapCut inexistente, removido do histórico ou com
  cancelamento pedido; mídia que não é vídeo original (foto, capa); mídia com job CapCut aberto. Duplicatas e
  reaproveitados nunca passam pelo gancho. Com vínculo, o "legendar automaticamente após o download" NÃO cria outro
  job; sem vínculo, tudo continua como antes.
- Status `importing` ("Aguardando downloads"): sem job aberto, sem cancelamento pedido e com algum lote vinculado
  (`source.batch_ids` ou filho de `source.group_id`) ainda `listing` ou `running`. Vale no `GET /batches/{id}` e no
  filtro `status=importing`. A importação terminou sem nenhum vídeo novo: o lote fica `completed` com 0 vídeos.
- Cancelamento independente: cancelar o lote CapCut (aceito também em `importing`, sem vídeo) não cancela a
  importação, e cancelar a importação não cancela o lote CapCut. Depois de cancelado, os vídeos que ainda chegarem
  não entram. "Reprocessar falhas" limpa o cancelamento do lote, e os vídeos seguintes voltam a entrar.
- Página: a opção "Edição CapCut" mostra o mesmo formulário de configuração do lote (`BatchConfigForm`, com os mesmos
  padrões, sem "Reprocessar vídeos já feitos") e o nome opcional. Criada a importação, a tela mostra "Os vídeos vão
  para a Edição CapCut (lote N)" com o link `/edicao-capcut?lote=N`, que abre o lote no painel da página.

Testes: `tests/test_import_capcut.py` (lote da importação), `tests/test_capcut_library_view.py` (visão da lista do
seletor), `tests/test_capcut_batches.py` (processador do CapCut falso e worker com o processor falso) e
`tests/test_capcut_batches_real.py` (modo só edição com FFmpeg real e vídeo sintético: duração com corte no início,
velocidade e limite).

## Limitações conhecidas

- Formato e nomes de interface verificados só no CapCut 9.7.1.4203 em português; outra versão/idioma pode quebrar a
  automação (os rótulos `Abaixar`/`Recomendado`/`Superior` são do português).
- Abrir o rascunho depende do cartão estar visível na tela inicial (o rascunho novo fica no topo da lista).
- Modo interativo: durante a exportação o CapCut vem para a frente e o mouse é usado por alguns segundos (o modo
  isolado, padrão, não faz isso; ver "Modo de exportação isolado").
- Segmentos de texto que usam modelos de texto (`text_templates`) não entram na lista de modelos: só textos comuns
  (`materials.texts`).
- Keyframes do texto do modelo não são copiados.
- Rascunhos cifrados (versões que cifram `draft_content.json`) não são suportados.
- Editor visual: alinhamento, espaçamento entre linhas, largura máxima de linha, fonte embutida pelo caminho e
  entrada + saída + loop juntas não foram exportados no CapCut real (só o formato). Itálico, sublinhado, espaçamento
  entre letras e as animações de saída e de loop foram conferidos em vídeo em 2026-11-07 (ver o item abaixo).
  Recurso Pro pode fazer a exportação ser bloqueada nesta conta (`capcut_export_blocked`).
- Editor visual, caixa de fundo + animação de ENTRADA/SAÍDA (verificado em vídeo no CapCut 9.7.1, 2026-11-07): a
  animação atua só no texto e a caixa de fundo aparece vazia por alguns instantes. O loop não tem esse efeito. Não é
  bloqueado: `design_warnings` (backend, `integrations/capcut/design.py`) e `designWarnings` (frontend,
  `lib/captionDesign.ts`), mesma regra e mesmo texto, geram o aviso mostrado no editor e devolvido em `warnings` no
  POST/PUT de modelos e na prévia. Conferidos em vídeo e corretos no gerador de estilo: Impact + itálico, amarelo com
  fundo, sublinhado, espaçamento, destaque, saída e loop.
- O catálogo do CapCut é o que o próprio CapCut já guardou em `ressdk_db` (painéis abertos alguma vez); fonte/animação
  que nunca apareceu num painel não aparece. Animações "Legenda" (`caption`) não entram na lista.
- Prévias ficam só em memória: reiniciar o app perde o estado (os arquivos em `previews/` ficam até a limpeza).
