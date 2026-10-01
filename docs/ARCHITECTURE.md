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
- **`StropheList`**: lista de cards ordenada por tempo, com os botões de editar, apagar, "Nova Estrofe" e "Colar Bloco".
- **`RenderDialog`**: roda `render_video` numa thread. Progresso e resultado voltam pra thread do Tk via `after()`. Cancelar (ou fechar a janela) liga uma flag que o loop de frames checa.
- **`App`**: janela principal, menu, atalhos (Ctrl+S salvar, Ctrl+R renderizar), abrir/salvar `.lyr`.

## Launcher (`run.py`)
Instala o Pillow se estiver faltando, avisa se não achar o FFmpeg e abre o app.

## Testes
```bash
python -m unittest discover -s tests -t .
```
- `test_core.py`: tempo, parser do bloco, serialização do projeto
- `test_renderer.py`: curva de fade, estrofe ativa, frames em memória, comando do FFmpeg
- `test_render_ffmpeg.py`: renders reais pequenos (160×90), pulado se não tiver FFmpeg
- `test_gui.py`: abrir/novo projeto, toggle de fundo, validação (janela escondida, diálogos simulados)
- `test_run.py`: launcher

## Limitações conhecidas
- Cada frame é desenhado do zero em Python, então o render é mais lento que o tempo real em 1080p. VP9 com alpha é a opção mais lenta.
- Estrofes sobrepostas: só a que começa primeiro aparece.
- O scroll do mouse no painel de configurações só funciona em cima das áreas vazias, não dos campos.
