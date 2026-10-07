# LyricRenderer 🎵

Gera vídeos de letra de música: fundo + título + cada estrofe no centro, com fade. A legenda pode ser gerada automaticamente a partir do áudio (Whisper, roda no seu PC).

## Instalar

- Python 3.9+
- [FFmpeg](https://www.gyan.dev/ffmpeg/builds/) no PATH (é ele que monta o vídeo)

As bibliotecas do Python (Pillow e faster-whisper) o próprio app oferece pra instalar quando abre. Se preferir instalar na mão:

```bash
pip install -r requirements.txt
```

## Abrir

- **Duplo clique** no atalho **LyricRenderer** da Área de Trabalho (abre sem terminal)
- ou pelo terminal: `python lyric_renderer.py`

Pra criar o atalho de novo (ex.: mudou a pasta de lugar): `python tools/create_shortcut.py`

## Como usar

1. **Áudio** → `Escolher…`. O **título** é preenchido sozinho a partir do nome do arquivo (`03_onde_eu_fui (1).mp3` → `Onde Eu Fui`); dá pra editar.
2. **Fundo** → `Escolher…`. A imagem fica salva pras próximas músicas.
3. **🎤 Gerar automática** → cole a letra (opcional, mas deixa bem mais preciso) → `Gerar`.
   - Estrofes separadas por linha em branco viram estrofes separadas.
   - O que estiver entre `[ ]` (ex.: `[refrão]`) é ignorado, igual ao Suno. Dá pra desligar.
4. **Confira**: clique numa estrofe pra ver na **prévia** como fica. `✎` edita (setinhas ±1s), `↓ Juntar` junta com a de baixo, `✕` apaga.
5. **▶ Renderizar vídeo** → escolha a pasta e o nome. Se o arquivo já existir, ele pergunta antes de substituir.

Pra próxima música: troque o áudio (o título acompanha), gere a legenda, confira e renderize.

**⚙ Configurações** (fonte, tamanhos, cores, fundo sem imagem, fade, FPS, resolução) ficam numa janela separada e são lembradas entre uma vez e outra.

Atalhos: `Ctrl+R` renderizar · `Ctrl+S` salvar projeto · `Ctrl+O` abrir projeto · `Ctrl+N` novo projeto.

### Colar com tempos (📋 Colar)

```
00:05 - 00:15
Letra da estrofe aqui
mais letra da música

00:20 - 00:30
Próxima estrofe
```

Formatos de tempo: `00:05`, `01:30`, `00:05.50`, `00:05,50`, `1:02:03`.

## Formato de saída

- **Com imagem de fundo ou cor sólida** → `.mp4` (H.264 + AAC)
- **Fundo transparente** (sem imagem, opção nas Configurações) → `.webm` (VP9 com alpha + Opus), pra sobrepor no editor
- Com áudio, o vídeo dura a música inteira

## Desenvolvimento

- Arquitetura: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Testes: `python -m unittest discover -s tests -t .`
- Ícone: `python tools/make_icon.py` gera `assets/icon.png` e `assets/icon.ico`
