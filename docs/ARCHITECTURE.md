# Arquitetura do LyricRenderer

App desktop em Tkinter. O arquivo principal ([lyric_renderer.py](../lyric_renderer.py)) tem o modelo, o renderizador e a interface; a legenda automática fica em [auto_lyrics.py](../auto_lyrics.py) e a checagem de dependências em [deps.py](../deps.py). Cada quadro do vídeo é desenhado com Pillow e enviado pro FFmpeg por um pipe.

```
┌──────────────────────────── App (Tk) ─────────────────────────────┐
│ header: Abrir · Salvar · ⚙ Configurações (SettingsDialog)          │
│ ┌ SongPanel ─────────────┐  ┌ StropheList ──────────────────────┐ │
│ │ Áudio / Título / Fundo │  │ 🎤 Gerar (AutoLyricsDialog)        │ │
│ │ Prévia (render_preview)│  │ 📋 Colar (PasteBlockDialog)        │ │
│ │ ▶ Renderizar ──────────┼─▶│ + Nova / ✎ (StropheEditor+TimeEntry)│ │
│ └────────────────────────┘  └───────────────────────────────────┘ │
└──────────────┬────────────────────────────────────────────────────┘
               │ Project (dataclass)            RenderDialog (thread)
               ▼                                      │
        FrameRenderer ──frames RGB/RGBA crus──▶ ffmpeg (stdin) ──▶ .mp4 / .webm
```

## Camadas

### 1. Modelo de dados
- **`Strophe`**: `id`, `start_time`, `end_time` (segundos) e `text` (várias linhas).
- **`Project`**: título, áudio, imagem de fundo, estilo (fontes, cores, fundo, resolução, FPS, fade) e estrofes. `to_dict()`/`from_dict()` ⇄ arquivo `.lyr` (JSON). `from_dict` ignora chaves desconhecidas.

### 2. Configuração do app (lembrada entre sessões)
`config.json` em `%APPDATA%\LyricRenderer\` (ou o caminho em `LYRIC_RENDERER_CONFIG`, usado nos testes).
- Estilo (`STYLE_FIELDS`, salvo pelo ⚙), última imagem de fundo, pastas usadas por último (áudio, saída) e as escolhas da legenda automática (modelo, idioma, ignorar `[ ]`).
- `load_config`/`save_config` nunca derrubam o app (arquivo ruim = config vazia). `apply_style` ignora valores com tipo errado.
- Projeto novo = estilo salvo + último fundo. Um `.lyr` aberto usa o estilo dele.

### 3. Funções puras (sem Tk, testadas)
| Função | O que faz |
|---|---|
| `parse_time` / `format_time` | `"01:30.50"` ⇄ `90.5` |
| `mask_time_digits` & cia | máscara "estilo banco" do campo de tempo |
| `title_from_filename` | `03_onde_eu_fui (1).mp3` → `Onde Eu Fui` (tira número de faixa só se tiver zero à esquerda ou `-`/`.` depois, pra não estragar "22 de Outubro") |
| `parse_lyrics_block` | texto do 📋 Colar → estrofes + erros |
| `merge_with_next`, `next_strophe_times` | ↓ Juntar; tempos sugeridos pra nova estrofe |
| `final_output_path`, `project_is_transparent` | transparente sempre vira `.webm` (imagem de fundo desliga a transparência) |
| `build_ffmpeg_cmd`, `probe_duration` | comando/duração do FFmpeg |

### 4. Renderizador (`FrameRenderer`)
- `render_frame(t)`: base (transparente, cor ou imagem) + título + estrofe ativa, com fade *smoothstep* (`min(fade, 30% da estrofe)`).
- `render_video()`: FFmpeg lendo `rawvideo` do stdin; stderr lido numa thread (senão trava no Windows). Duração = `max(fim da letra + 1,5s, duração do áudio)`.
- Busca de fonte (`_search_font_file`) e imagem de fundo redimensionada (`_load_background`) têm cache: a prévia cria um renderizador a cada mudança.

| Saída | Vídeo | Áudio |
|---|---|---|
| Transparente (`.webm` forçado) | VP9 + alpha | Opus |
| `.webm` com fundo | VP9 | Opus |
| `.mp4` | H.264 | AAC |

### 5. Interface
- **`SongPanel`** (esquerda): áudio (preenche o título), título, fundo com miniatura, **prévia** 400×225 da estrofe selecionada (`render_preview`: um quadro em tamanho real reduzido; transparente aparece sobre xadrez) e o botão de renderizar.
- **`StropheList`** (direita): cards ordenados por tempo; clicar seleciona (prévia), duplo clique edita; `✎`, `✕`, `↓ Juntar`.
- **`SettingsDialog`/`SettingsPanel`**: estilo. Só grava no projeto ao **Salvar** (`_apply`, que valida os números); o App então salva no `config.json`.
- **`AutoLyricsDialog`**, **`RenderDialog`**: trabalho pesado numa thread (`WorkerMixin`): a thread só coloca callbacks numa `queue.Queue` que a thread do Tk executa (Tk não é thread-safe).
- **Render**: diálogo nativo de "Salvar como" na última pasta usada, com o título como nome; `_confirm_output` pergunta antes de substituir um arquivo existente (checando o arquivo que vai ser realmente escrito, ex. `.mp4` → `.webm`).
- **Legenda automática de outra música** é substituída sem perguntar; da mesma música, pergunta (pode ter edição sua).
- `bind_wheel_scroll`: a roda vai pro widget embaixo do mouse, então escutamos no app inteiro e filtramos pelo caminho do widget.
- `report_callback_exception`: erro num botão vira janela de erro + `erros.log` na pasta da config (pelo atalho não há terminal).

## Legenda automática ([auto_lyrics.py](../auto_lyrics.py))
```
áudio ──ffmpeg──▶ PCM 16 kHz ──faster-whisper (CPU, int8)──▶ palavras com tempo
                                                              │
                     com letra ─▶ align_lyrics: casa palavra da letra ↔ palavra ouvida
                     sem letra ─▶ group_lines: linhas e estrofes pelas pausas
                                                              ▼
                                                    TimedStrophe(start, end, text)
```
- **Com letra**: o texto final é a letra colada. `split_lyrics`: linha em branco separa estrofes; com "ignorar `[ ]`", tudo entre colchetes some e uma linha que era só tag (`[refrão]`) também separa estrofe. Parênteses ficam (o Suno canta). Se a opção estiver desligada, as tags aparecem no texto mas nunca são casadas com o áudio. O *prompt* do Whisper é sempre a letra sem tags. Palavras normalizadas (sem acento/pontuação) e casadas com `difflib`; estrofe sem nenhuma palavra casada divide o espaço entre as vizinhas.
- **Sem letra**: linha nova em pausa ≥ 0,6s; linha com > 9 palavras cortada perto do meio onde a palavra anterior é mais longa (o Whisper "esconde" a pausa dentro da palavra); estrofe nova em pausa ≥ 1,8s ou a cada 4 linhas.
- `_finish`: folga de 0,3s antes / 0,4s depois (o fade não come sílaba) e estrofes sem encostar.
- Modelos `small` (padrão) e `medium`, baixados na primeira vez pro cache do Hugging Face (`is_downloaded` checa) e mantidos em memória.
- **PyAV**: o faster-whisper importa só pra decodificar áudio, o que fazemos com o FFmpeg. Se o import falhar (ex.: DLL bloqueada pelo Smart App Control), `_import_whisper_model` usa um módulo vazio no lugar. `.wav` é lido em Python puro se o FFmpeg não rodar.

## Inicialização (`main`)
1. `_ensure_std_streams`: com `pythonw` (atalho) não existe console; algumas bibliotecas quebram ao escrever nele.
2. `_set_windows_app_id`: barra de tarefas mostra o ícone do app, não o do Python.
3. `check_dependencies` ([deps.py](../deps.py)): pacotes checados com `find_spec` (sem importar). Faltando algo, pergunta, instala com pip numa thread e reabre o app. Pillow é obrigatório; faster-whisper é opcional (só a legenda automática).
4. Abre o `App` e, logo depois, `check_ffmpeg` avisa se o FFmpeg está faltando ou bloqueado.

## Arquivos de apoio
- `assets/icon.png`, `assets/icon.ico`: gerados por `tools/make_icon.py`.
- `tools/create_shortcut.py`: atalho na Área de Trabalho apontando pro `pythonw.exe` + `lyric_renderer.py`, com o ícone.

## Testes
```bash
python -m unittest discover -s tests -t .
```
- `test_core.py`: tempo, máscara, parser, juntar, título pelo nome do arquivo, config, serialização
- `test_renderer.py`: fade, estrofe ativa, quadros, comando do FFmpeg
- `test_render_ffmpeg.py`: renders reais pequenos (pulado se o FFmpeg não roda)
- `test_auto_lyrics.py`: colchetes, agrupamento, alinhamento, leitura de WAV. Whisper real só com `LYRIC_SLOW_TESTS=1 LYRIC_TEST_AUDIO=musica.wav [LYRIC_TEST_LYRICS=letra.txt]`
- `test_gui.py`: tela principal, configurações salvas, prévia, render (aviso de substituir), legenda automática, rolagem, checagem de dependências (janelas escondidas, diálogos simulados)
- `test_deps.py`: pacotes faltando, pip, estados do FFmpeg

## Limitações conhecidas
- Cada quadro é desenhado em Python: em 1080p o render é mais lento que o tempo real (VP9 com alpha é o mais lento).
- Estrofes sobrepostas: só a que começa primeiro aparece.
