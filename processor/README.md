# media_processor

Pacote Python (sem dependências de runtime) que concentra toda a interação do Media Tool com FFmpeg/ffprobe.
Contrato: `docs/CONTRACTS.md`, seção 9.

Instalação (editable no venv do projeto) e testes:

```
/c/media-tool/.venv/Scripts/python.exe -m pip install -e /c/media-tool/processor
cd /c/media-tool/processor && /c/media-tool/.venv/Scripts/python.exe -m pytest -q
```

## API

```python
from media_processor import (MediaProcessor, ProcessingOptions, ProbeResult, ProcessResult, ToolInfo, ProcessorError,
    FFmpegNotFoundError, InvalidMediaError, ProcessingFailedError, ProcessingCancelledError, sha256_file)

mp = MediaProcessor(ffmpeg_path=None, ffprobe_path=None)   # explícito > MEDIA_TOOL_FFMPEG/MEDIA_TOOL_FFPROBE > PATH
mp.ffmpeg_info() / mp.ffprobe_info() -> ToolInfo           # nunca levantam
mp.probe(path) -> ProbeResult                              # InvalidMediaError se inválido/inexistente
opts = ProcessingOptions.from_params(preset_params)        # chave desconhecida ou valor inválido -> ValueError
suffix = mp.output_suffix(probe, opts)                     # ".mp4" (vídeo, inclusive stream copy), ".jpg", ".png"
mp.build_command(src, dst, probe, opts) -> list[str]       # puro, não executa
mp.process(src, dst, opts, progress=cb, cancel=event, timeout=s) -> ProcessResult
mp.make_thumbnail(src, dst, width=320) -> Path             # JPEG; dst existente -> ProcessorError
sha256_file(path) -> str
```

Erros: todos derivam de `ProcessorError` e têm `code`: `ffmpeg_not_found`, `invalid_media`, `processing_failed`
(com `returncode` e `stderr`, cauda limitada a 64 KiB; a mensagem traz os últimos 1500 caracteres), `cancelled`.
`src == dst` levanta `ProcessorError`. `dst` já existente levanta `ProcessorError` (nunca sobrescreve; o backend gera
nomes únicos). Se sobrar menos de max(0,1 s, 1 frame) depois do trim, levanta `InvalidMediaError` ("O corte inicial
deixa o vídeo sem duração útil") antes de executar. Falha no `os.replace` final (ex.: permissão) vira
`ProcessingFailedError` e o temporário é removido.

`ProbeResult.width/height` são as dimensões de exibição (rotação já considerada). `rotation` é em graus no sentido
horário (0/90/180/270), vindo da displaymatrix da stream, da tag antiga `rotate` ou, em imagens, da orientação
EXIF (lida do primeiro frame decodificado).

Imagem x vídeo: quando o formato é de imagem (`image2`, `*_pipe`), o codec é de imagem (mjpeg, png, webp, gif...) ou
o container não informa duração, o probe lê os cabeçalhos dos pacotes da stream (`-show_entries
packet=duration_time`, sem decodificar). Mais de 1 pacote = vídeo: GIF/WebP animado e H.264/HEVC cru viram vídeo
(mp4 H.264, sem áudio). Sem duração no container, a duração é a soma das durações dos pacotes e o fps sai dela (o
fps declarado nesses casos é o padrão 25 do demuxer, não o real).

## Decisões

- **Execução**: lista de argumentos, `shell=False`, `CREATE_NO_WINDOW` no Windows, stdin fechado. O env passado
  ao ffmpeg/ffprobe (e ao taskkill) é uma cópia de `os.environ` sem chaves que, em maiúsculas, comecem com
  `INSTAGRAM_` ou contenham `SESSION`, `COOKIE`, `TOKEN`, `SECRET` ou `PASSWORD` (PATH, SYSTEMROOT, TEMP ficam). stdout
  (`-progress pipe:1 -nostats`) e stderr lidos em threads; o callback de progresso roda na thread de quem chamou
  `process`. Progresso = `out_time_us / duração esperada`, clamp 0..1, só valores crescentes, 1.0 ao final; imagens
  só recebem 1.0. Cancelamento (`threading.Event`) e timeout matam a árvore do processo (`taskkill /F /T` no
  Windows, `killpg` no POSIX) e removem o temporário.
- **Escrita atômica**: saída em `.<nome>.<hex>.part<sufixo>` no diretório de `dst`, probe da saída, depois
  `os.replace`. `src` só é lido.
- **Caminhos**: sempre absolutos (letra de drive no Windows), então `-evil.mp4` nunca vira opção. Muxer passado
  explicitamente com `-f`. Nomes com espaço, acento e unicode são testados.
- **Trim**: filtros `trim=start=T` / `atrim=start=T` seguidos do mesmo deslocamento constante nas duas streams
  (`setpts=(PTS-T/TB)/speed`, `asetpts=PTS-T/TB`). O ffmpeg já põe a linha do tempo relativa ao start_time do
  container, então o corte é o mesmo ponto para vídeo e áudio e o alinhamento relativo é preservado (inclusive
  áudio que começa depois do vídeo). Não usamos `-ss` antes do `-i`: em MPEG-TS (start_time != 0, sem índice) a
  busca cai no meio do GOP e o ffmpeg 9 gerava mp4 vazio com código 0. Nem `PTS-STARTPTS`, que normaliza cada
  stream pelo próprio primeiro frame. Custo: decodifica desde o início (irrelevante para trims de até 30 s).
- **Velocidade**: `setpts=PTS/speed` + `atempo` (encadeado fora de 0.5..2). 1.00 não gera filtro. Sem áudio, nada
  de `-filter:a`.
- **FPS**: com velocidade diferente de 1, `fps=<fps de origem>` (teto 60) depois do `setpts`: a saída mantém a
  cadência original (30 fps continua 30) em vez de virar 31,5 fps, descartando ou duplicando frames conforme
  necessário. Sem mudança de velocidade, o fps de origem passa intacto.
- **A/V sync**: medido nos testes, diferença de duração entre as streams < 0,1 s e start_time igual.
  `-avoid_negative_ts make_zero` **não** é usado: no ffmpeg 9 ele deslocou o início no mp4 (vídeo 0,067 s por
  atraso de B-frames, áudio 0,043 s pelo priming do AAC); o padrão do muxer mp4 (edit list) dá start_time 0.
  `-fflags +genpts` na entrada quando `auto_adjust`. Também testado com MPEG-TS (start_time 1,4 s) e com áudio que
  começa 1 s depois do vídeo.
- **Ajustes automáticos**: rotação aplicada pelo autorotate do ffmpeg (saída sem displaymatrix); dimensões ímpares
  são recortadas em 1 px (`crop`, não reescala, não deforma); SAR não quadrado vira pixels quadrados ajustando a
  largura; `setsar=1`; `yuv420p`. Sem filtros de cor ou nitidez. Com `auto_adjust=False` usa `-noautorotate` (a
  rotação continua como metadata de exibição), mas o recorte para dimensões pares continua quando há reencode,
  porque o libx264 em yuv420p exige.
- **Reencode**: libx264 `-preset medium -crf 20 -profile:v high -pix_fmt yuv420p`, AAC 160k (mais de 2 canais vira
  estéreo; mono continua mono), `-movflags +faststart`, `.mp4`. `reencode=False` com qualquer filtro ativo (trim,
  mirror, velocidade ou ajuste automático com algo a corrigir: rotação, dimensão ímpar, SAR, pix_fmt) força reencode
  (`forced_reencode=True`). Só cleanup -> `-c copy` remuxando sempre para mp4 (o mp4 é o único container em que o
  cleanup fica completo: matroska/webm gravam o MuxingApp obrigatório "Lavf", que o ffprobe mostra como tag
  `encoder`, e mov grava `vendor_id=FFMP`). Codecs de vídeo h264/hevc/av1/vp9/mpeg4 e de áudio
  aac/mp3/opus/flac/alac/ac3/eac3 vão por cópia; outros (ex.: vp8, vorbis) são recodificados
  (`forced_reencode=True`). HEVC copiado recebe `-tag:v hvc1` (tag esperada por players da Apple).
- **Imagens**: sem trim/velocidade/fps; `hflip` se mirror; orientação EXIF sempre aplicada; JPEG `-q:v 2` para
  JPEG/WebP/HEIC/BMP etc., PNG continua PNG, WebP com
  canal alpha vira PNG (preserva a transparência). Imagens são sempre recodificadas: copiar o pacote JPEG manteria o
  segmento APP1 (EXIF) e perderia a orientação. Por isso `reencode=False` em imagem retorna `forced_reencode=True`.
  O encoder mjpeg do ffmpeg 9 não escreve EXIF, e com bitexact também não grava o comentário `Lavc`.
- **Miniatura**: frame em 1 s (0 s se o vídeo tiver até 1,5 s), largura 320, altura proporcional par, JPEG.
- **Aceleração de hardware**: não usada na V1. Sempre codificação por software (libx264), o que dá saída
  determinística e funciona em qualquer máquina.

## Metadata residual (ffmpeg 9.0, verificado com ffprobe nos testes)

Cleanup = `-map_metadata -1 -map_chapters -1 -fflags +bitexact -flags:v +bitexact -flags:a +bitexact
-metadata:s:v encoder= -metadata:s:a encoder=` e, no reencode, `-bsf:v filter_units=remove_types=6` (remove a SEI
do x264 com versão e opções em texto). Saem title, comment, creation_time, location, encoder (formato e stream),
handler_name customizado, `Lavf`/`Lavc`/`x264` nos bytes do arquivo.

Ficam, porque o muxer mp4 grava sempre (são essenciais à reprodução ou obrigatórios no formato):
- formato: `major_brand=isom`, `minor_version=512`, `compatible_brands=isomiso2avc1mp41` (caixa `ftyp`);
- streams: `language=und` (caixa `mdhd`) e `handler_name=VideoHandler` / `SoundHandler` (caixa `hdlr`; um valor
  vazio é substituído pelo padrão).

No stream copy, o bitstream não é alterado: SEIs que já existam no vídeo de origem (por exemplo a do x264)
permanecem.
