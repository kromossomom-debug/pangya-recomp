# pangya-recomp

Recompilação estática de **Pangya: Fantasy Golf** (PSP, USA, `ULUS10438`) para Linux x86-64.
O código MIPS do jogo é traduzido para C++ e compilado como código nativo; o sistema do PSP
(threads, GPU GE → Vulkan/OpenGL, áudio, arquivos, savedata) vem do núcleo HLE do
[PPSSPP](https://github.com/hrydgard/ppsspp), usado como runtime. É o mesmo modelo do
[N64Recomp](https://github.com/N64Recomp/N64Recomp).

*Static recompilation of the PSP game Pangya: Fantasy Golf to native Linux code, using PPSSPP's
HLE core as the runtime. English summary at the bottom.*

> **Este repositório não contém o jogo nem nenhum código derivado dele.** Você precisa da sua
> própria cópia (ISO do UMD). O `build.sh` gera o código do jogo localmente a partir dela.

## Compilar

Requisitos: `cmake`, `ninja`, `gcc`/`g++`, `python3`, `git`, SDL3 + SDL3_ttf, GLEW e headers
do Vulkan. Opcional: `7z` (para o ícone).

```sh
./build.sh "/caminho/para/Pangya - Fantasy Golf (USA).iso"
dist/pangya "/caminho/para/Pangya - Fantasy Golf (USA).iso"
```

O primeiro build leva alguns minutos: ele clona o PPSSPP no commit testado, aplica
`runtime/ppsspp-recomp.patch`, gera cerca de 40 MB de C++ e compila tudo.

### Steam Deck / SteamOS

O SteamOS não tem compilador, então compile dentro de um distrobox. O binário resultante roda
direto no SteamOS:

```sh
distrobox create --name psp-build --image docker.io/library/archlinux:latest
distrobox enter psp-build -- sudo pacman -S --needed base-devel cmake ninja git python sdl3 sdl3_ttf glew vulkan-headers p7zip
BUILD_IN="distrobox enter psp-build --" ./build.sh "/caminho/para/a.iso"
dist/install.sh   # instala em ~/.local/share/pangya-recomp e cria a entrada no menu
```

No modo jogo, adicione `~/.local/share/pangya-recomp/app/pangya` como jogo não Steam.

## Jogar

```sh
dist/pangya [iso]          # padrão: ~/Downloads/Pangya - Fantasy Golf (USA).iso
PANGYA_WINDOWED=1 dist/pangya
```

Saves e configurações ficam em `~/.local/share/pangya-recomp`, separados de qualquer PPSSPP
instalado.

## Como funciona

| Peça | Onde |
|---|---|
| Recompilador MIPS (Allegrex) → C++ | `tools/recomp.py` |
| Helpers do código gerado, com a mesma semântica do interpretador do PPSSPP | `tools/recomp_rt.h` |
| Runtime (dump, validação por hash, dispatcher, fallback) | `Core/Recomp/` no patch |

1. **Dump.** Com `PPSSPP_RECOMP_DUMP=dir`, o runtime grava o módulo `projectg_psp` como o PPSSPP
   o carrega (relocado em `0x08804000`, imports resolvidos), junto com a decodificação de cada
   palavra feita pelas próprias tabelas do interpretador.
2. **Tradução.** As funções são descobertas por alvos de `jal`, pelo entry point, por ponteiros
   em `.data` e por pares `lui`/`addiu`. Cada função vira uma função C++ com um `switch` de
   entrada sobre seus labels, para poder ser retomada em qualquer ponto. O resultado são
   10.945 funções, cobrindo 835.336 instruções.
3. **Execução.** Todo o estado do guest fica no `MIPSState` do PPSSPP. Chamadas viram chamadas C++
   nativas; ao voltar, o chamador só continua se `pc` for o endereço de retorno esperado. Caso
   contrário (troca de thread, interrupção, fim de timeslice), a pilha nativa é desenrolada até o
   dispatcher, que retoma em `pc`. Imports viram chamadas HLE diretas.
4. **Segurança.** Na carga, o código do módulo é comparado por hash com a imagem recompilada. Se
   não bater, o jogo roda no interpretador. PCs sem código compilado (bibliotecas PRX carregadas
   do disco, stubs, as cerca de 4 mil instruções VFPU) também passam pelo interpretador.

### Validação

- Com as mesmas entradas, as capturas em 9 pontos ao longo de 5 minutos de jogo saíram
  idênticas pixel a pixel às do interpretador do PPSSPP.
- Velocidade: 300 s de jogo em 2,1 s com o código recompilado, contra 13,8 s no interpretador
  (headless, Steam Deck).

### Depuração

- `PPSSPP_RECOMP_DISABLE=1`: roda tudo no interpretador.
- `PPSSPP_RECOMP_OFF=lo-hi,...`: manda faixas de funções para o interpretador.
- `tools/bisect.py gen -- tools/check_run.sh build ISO`: acha a função responsável por uma
  divergência.
- `tools/hlediff.sh`: normaliza logs do PPSSPP para comparar duas execuções.

## Limitações

- O código gerado ainda não guarda os registradores em variáveis locais, então hoje fica um pouco
  atrás do JIT do PPSSPP em velocidade bruta.
- As bibliotecas de vídeo (`mpeg`/`psmf`/`psmfplayer`) que o jogo carrega do disco rodam no
  interpretador.
- O modo ad hoc (multiplayer local) não foi testado.

## Licença

GPL-2.0-or-later, a mesma do PPSSPP, do qual o runtime é derivado. Pangya é marca da Ntreev Soft;
este projeto não tem relação com a Ntreev nem com a Sony.

---

### English summary

Ahead-of-time recompiler that turns the PSP game's MIPS code into C++, linked into a patched
PPSSPP that acts as the platform runtime (HLE kernel, GE renderer, audio, I/O). Bring your own
ISO: `./build.sh game.iso` fetches PPSSPP at the tested commit, applies the patch, dumps the
module, recompiles it and builds `dist/pangya`. No game code or assets are included.
