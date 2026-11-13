# Minutagem

Minutagem automática de atletas por jogo (fonte: API pública do ESPN), com dashboard no GitHub Pages.

- `coletor/coletar.py`: baixa os jogos e gera `docs/data/dados.json`. Roda sozinho todo dia às 06:00 (Brasília) pelo GitHub Actions.
- `posicoes.csv`: grupo de posição de cada atleta. Preencha `grupo_manual` (GOL, LAT, ZAG, VOL, MEI, EXT, ATA) só onde a sugestão do ESPN estiver errada. Suas correções são preservadas.
- `docs/index.html`: o dashboard.
- Para incluir Libertadores, Sul-Americana ou Copa do Brasil, descomente a linha da competição em `LIGAS`, no início de `coletor/coletar.py`.

Dados internos do clube (bem-estar, GPS) nunca devem ser enviados para este repositório.
