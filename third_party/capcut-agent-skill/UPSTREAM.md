# capcut-agent-skill (cópia de terceiros)

- Origem: https://github.com/greedygains/capcut-agent-skill
- Commit: bd8b88f912ba1e6c4c17776881559c7151fd63ba (2026-09-25)
- Copiado em: 2026-11-07, sem a pasta `.git` e SEM modificações (conferido com `diff -r` contra o clone).
- Licença: MIT (arquivo `LICENSE` preservado).

## O que o Media Tool reaproveita

Carregado por caminho (importlib) em `backend/app/integrations/capcut/skill.py`:

- `.agents/skills/capcut-editor/scripts/update_scribe_subtitles.py` -> `create_native_text_material`: o material de
  texto nativo (preenchimento branco, contorno preto 0,06, `check_flag` 15) é o ESTILO PADRÃO das legendas quando o
  dono ainda não escolheu um modelo. A posição/escala do skill (y -0,70, escala 1,919) também.
- `.agents/skills/capcut-editor/scripts/capcut_draft.py` -> `parse_time_us`.
- Invariantes documentadas em `references/draft_schema.md` §8 e `rules/`: tempos inteiros em microssegundos, espelho
  da linha do tempo idêntico ao arquivo da raiz, não sobreposição com folga >= 20 ms, limpar a legenda em pausas
  >= 180 ms (fim = última palavra + 50 ms), uma única trilha de texto de diálogo, gravação atômica e backup do
  índice global.

## Por que o motor de rascunho é próprio

O skill foi escrito para o CapCut do macOS: usa `draft_info.json`, `Timelines/project.json` com
`current_timeline_id`, a pasta `~/Movies/CapCut/...` e `pkill`/`open -a`. No CapCut 9.7 do Windows a linha do tempo
fica em `draft_content.json` (raiz e espelho em `Timelines/<id>/`), o `project.json` usa `main_timeline_id` e a
pasta é `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft`. Por isso o Media Tool usa só as funções
acima e implementa o motor de rascunho em `backend/app/integrations/capcut/draft.py` (ver `docs/CAPCUT.md`).

Para atualizar: substituir a pasta inteira por uma cópia nova do upstream (sem `.git`), atualizar este arquivo e
rodar `backend/tests/test_capcut_*.py`.
