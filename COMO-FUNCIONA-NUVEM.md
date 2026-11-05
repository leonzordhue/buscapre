# Buscapre na nuvem (desde 05/11/2026)

- **Nuvem (GitHub Actions, repo `leonzordhue/buscapre`):** só OLX, região de Manaus, a cada ~15 min, aviso no Discord `#preços` (servidor Leon Games). Roda com o PC desligado.
- **PC (Agendador "Bot Telegram Buscapre" e monitor):** OLX + Facebook no Telegram, como antes, quando o PC está ligado.
- Facebook não roda na nuvem: o servidor do GitHub cai na tela de login. Plano: Android em casa com Tailscale como saída (IP residencial).
- Webhook do Discord: segredo `DISCORD_WEBHOOK` no repositório (nunca em arquivo).
- Estado da nuvem: `estado/vistos.json` e `estado/anuncios.json` (o workflow faz commit a cada rodada).
- Mudar buscas/ofertas: edite `buscas.txt`/`ofertas.txt` e rode `git commit -am "buscas" && git push`.
