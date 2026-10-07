# Arquitetura do LyricRenderer

App desktop em um arquivo só ([lyric_renderer.py](../lyric_renderer.py)): uma interface Tkinter onde você monta o projeto, e um renderizador que desenha cada frame com Pillow e manda os frames pro FFmpeg por um pipe.

```
┌───────────── GUI (Tkinter) ─────────────┐
│ App                                     │
│  ├─ SettingsPanel  (form ⇄ Project)     │
│  ├─ StropheList    (cards das estrofes) │
│  │   ├─ StropheEditor   (nova/editar)   │
│  │   └─ PasteBlockDialog (colar bloco)  │
│  └─ RenderDialog   (thread de render)   │
└──────────────┬──────────────────────────┘
               │ Project (dataclass)
               ▼
        FrameRenderer ──frames RGB/RGBA crus──▶ ffmpeg (stdin) ──▶ .mp4 / .webm
               ▲                                   ▲
           Pillow (texto, fundo)             áudio (opcional)
```

## Camadas

### 1. Modelo de dados
- **`Strophe`**: `id`, `start_time`, `end_time` (segundos) e `text` (várias linhas).
- **`Project`**: todas as configurações (título, fontes, cores, fundo, resolução, FPS, fade, áudio) mais a lista de estrofes. `to_dict()`/`from_dict()` fazem a conversão pro arquivo `.lyr`, que é só um JSON. `from_dict` ignora chaves desconhecidas, então arquivos de versões futuras continuam abrindo.

### 2. Funções puras (sem Tk, fáceis de testar)
| Função | O que faz |
|---|---|
| `parse_time` / `format_time` | `"01:30.50"` ⇄ `90.5` |
| `parse_lyrics_block` | Transforma o texto do "Colar Bloco" em `Strophe`s e devolve a lista de erros |
| `hex_to_rgb`, `safe_filename` | Utilitários |
| `find_font` | Acha uma fonte por caminho ou nome nas pastas de fontes do sistema |
| `build_ffmpeg_cmd` | Escolhe codec e container (veja abaixo) |
| `probe_duration` | Duração do áudio via `ffprobe` |

### 3. Renderizador (`FrameRenderer`)
- `render_frame(t)` monta um frame: base (transparente, cor sólida ou imagem de fundo) + overlay com o título e a estrofe ativa.
- **Fade**: `_alpha()` usa uma curva *smoothstep*. A duração do fade é `min(fade_duration, 30% da estrofe)`, então estrofes curtas não ficam só em fade.
- **Título**: aparece do início da primeira estrofe até o fim da última, com fade.
- **Estrofe ativa**: `active_strophe(t)` pega a primeira estrofe com `start < t < end`.
- `render_video()` abre o FFmpeg lendo `rawvideo` do stdin, escreve os frames um por um e lê o stderr numa thread separada (senão o pipe trava no Windows). A duração é `max(fim da última estrofe + 1,5s, duração do áudio)`.

**Codec por saída** (`build_ffmpeg_cmd`):

| Situação | Vídeo | Áudio |
|---|---|---|
| Fundo transparente (qualquer extensão vira `.webm`) | VP9 + alpha (`yuva420p`) | Opus |
| `.webm` com fundo | VP9 | Opus |
| Qualquer outra (ex.: `.mp4`) | H.264 | AAC |

> Se a imagem de fundo existe, a transparência é desligada automaticamente.

### 4. GUI
- **`SettingsPanel`**: o formulário da esquerda. `_apply()` copia o form → `Project` (valida os números e devolve `False` se algo estiver inválido). `load(project)` faz o caminho inverso, usado ao abrir ou criar um projeto.
- **`StropheList`**: lista de cards ordenada por tempo, com os botões de editar, apagar, "↓ Juntar" (`merge_with_next`: junta com a estrofe de baixo, do início da primeira ao fim da segunda), "Nova Estrofe", "Colar Bloco" e "Gerar Legenda".
- **`RenderDialog`**: roda `render_video` numa thread. Progresso e resultado voltam pra thread do Tk via `after()`. Cancelar (ou fechar a janela) liga uma flag que o loop de frames checa.
- **`App`**: janela principal, menu, atalhos (Ctrl+S salvar, Ctrl+R renderizar), abrir/salvar `.lyr`.

## Legenda automática ([auto_lyrics.py](../auto_lyrics.py))
Botão **🎤 Gerar Legenda** → `AutoLyricsDialog` → `auto_lyrics.generate()` numa thread → as estrofes geradas substituem a lista (pergunta antes se já houver estrofes) pra revisão.

```
áudio ──ffmpeg──▶ PCM 16 kHz ──faster-whisper (CPU, int8)──▶ palavras com tempo
                                                              │
                     com letra ─▶ align_lyrics: casa palavra da letra ↔ palavra ouvida
                     sem letra ─▶ group_lines: linhas e estrofes pelas pausas
                                                              ▼
                                                    TimedStrophe(start, end, text)
```
- **Com letra (alinhar)**: o texto final é exatamente a letra colada (estrofes separadas por linha em branco, tags tipo `[Refrão]` são ignoradas). A letra também vai como *prompt* pro Whisper, o que melhora o reconhecimento. As palavras são normalizadas (sem acento/pontuação) e casadas com `difflib`; cada estrofe vai da primeira à última palavra casada. Estrofe sem nenhuma palavra casada divide o espaço entre as vizinhas.
- **Sem letra (transcrever)**: linha nova em pausa ≥ 0,6s; linha com mais de 9 palavras é cortada perto do meio, onde a palavra anterior é mais longa (o Whisper "esconde" a pausa dentro da palavra). Estrofe nova em pausa ≥ 1,8s ou a cada 4 linhas.
- `_finish` dá uma folga de 0,3s antes / 0,4s depois (pro fade não comer sílaba) e garante que as estrofes não se encostem.
- Modelos `small` (padrão) e `medium`, baixados na primeira vez pro cache do Hugging Face e mantidos em memória entre gerações.
- **PyAV**: o faster-whisper importa o PyAV só pra decodificar áudio. Como o FFmpeg já faz isso (e o Smart App Control do Windows pode bloquear as DLLs do PyAV), `_import_whisper_model` usa um módulo vazio no lugar se o import falhar. `.wav` é lido em Python puro se o FFmpeg não rodar.
- A comunicação da thread com a interface é por uma `queue.Queue` lida pela thread principal (a thread nunca mexe no Tk).

## Launcher (`run.py`)
Instala o Pillow e o faster-whisper se estiverem faltando (checa com `find_spec`, sem importar), avisa se não achar o FFmpeg e abre o app.

## Testes
```bash
python -m unittest discover -s tests -t .
```
- `test_core.py`: tempo, parser do bloco, serialização do projeto
- `test_renderer.py`: curva de fade, estrofe ativa, frames em memória, comando do FFmpeg
- `test_render_ffmpeg.py`: renders reais pequenos (160×90), pulado se não tiver FFmpeg
- `test_gui.py`: abrir/novo projeto, toggle de fundo, validação (janela escondida, diálogos simulados)
- `test_auto_lyrics.py`: agrupamento, alinhamento, leitura de WAV. O teste com o Whisper real é lento e só roda com `LYRIC_SLOW_TESTS=1 LYRIC_TEST_AUDIO=musica.wav [LYRIC_TEST_LYRICS=letra.txt]`
- `test_run.py`: launcher

## Limitações conhecidas
- Cada frame é desenhado do zero em Python, então o render é mais lento que o tempo real em 1080p. VP9 com alpha é a opção mais lenta.
- Estrofes sobrepostas: só a que começa primeiro aparece.

## Rolagem com o mouse
O evento da roda vai pro widget que está embaixo do ponteiro (um label, um botão do card), não pro canvas. Por isso `bind_wheel_scroll(canvas)` escuta a roda no app inteiro (`bind_all`) e só rola se o widget do evento estiver dentro do canvas (pelo caminho do widget no Tk). É usado na lista de estrofes e no painel de configurações.
