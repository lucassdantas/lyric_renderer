# LyricRenderer 🎵

Gerador de vídeos de letras de música — rápido, centralizado, com fade pro transparente.

## Requisitos

- Python 3.9+
- FFmpeg instalado no sistema
- Pillow (`pip install Pillow`)

## Como instalar

```bash
# Instalar dependências
pip install Pillow

# OU use o launcher que instala automaticamente:
python run.py
```

**FFmpeg:**
- Ubuntu/Debian: `sudo apt install ffmpeg`
- macOS: `brew install ffmpeg`
- Windows: https://ffmpeg.org/download.html

## Como usar

```bash
python lyric_renderer.py
```

### Fluxo de trabalho

1. **Configurações** (painel esquerdo):
   - Título da música, fonte, tamanho
   - Cor do texto e do fundo
   - Arquivo de áudio (opcional)
   - FPS e resolução

2. **Estrofes** (painel direito):
   - Clique `+ Nova Estrofe` para adicionar manualmente
   - OU clique `📋 Colar Bloco` para importar várias de uma vez

### Formato do Colar Bloco

```
00:05 - 00:15
Letra da estrofe aqui
mais letra da música

00:20 - 00:30
Próxima estrofe
mais letra

01:10 - 01:25
Mais uma estrofe
```

- Separe estrofes por **linha em branco**
- Formatos de tempo aceitos: `00:05`, `01:30`, `00:05.50`, `00:05,50`, `1:30.00`, `1:02:03`
- Estrofes com erro (sem tempo, sem letra, fim antes do início) são listadas e puladas

### Renderizar

- **Ctrl+R** ou botão `▶ Renderizar`
- Escolha `.mp4` para vídeo normal com fundo
- Escolha `.webm` para vídeo com **transparência** (para sobrepor no editor)

### Salvar/abrir projeto

- **Ctrl+S** salva o projeto como `.lyr` (JSON)
- Pode reabrir e editar depois

## Vantagens sobre o método anterior

| Antes (Kdenlive manual) | LyricRenderer |
|------------------------|---------------|
| Colar cada estrofe individualmente | Colar tudo de uma vez |
| Posicionar cada texto | Centralização automática |
| Efeito fade em cada uma | Fade automático em todas |
| Trabalho repetitivo | Um projeto, render em segundos |

## Formato de saída

- **MP4** (H.264 + AAC): Fundo sólido ou imagem, pronto para usar
- **WebM** (VP9 + Opus): Transparência total, ideal para sobrepor no Kdenlive como overlay
- Com áudio, o vídeo dura a música inteira (ou até a última estrofe + 1,5s, o que for maior)

## Desenvolvimento

- Arquitetura: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Testes (sem dependências extras): `python -m unittest discover -s tests -t .`

## Dicas

- Use `.webm` se quiser continuar usando o Kdenlive só para combinar com o vídeo
- O título aparece fixo durante toda a música com fade no início/fim
- Cada estrofe aparece no **centro exato** da tela
- O fade é pro **transparente** (não pro preto)
