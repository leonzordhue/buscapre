"""Bot do Telegram que atende o comando /busca (no grupo ou no privado).

Quem digita "/busca game boy" recebe NO PRIVADO os botões Amazonas / Curitiba; ao tocar num deles,
chega o resultado da OLX e do Facebook Marketplace daquela região.
O Telegram só deixa o bot escrever no privado de quem já tocou em "Iniciar" no bot;
se a pessoa ainda não tocou, o bot avisa no grupo.

Fica rodando direto (tarefa agendada no logon). Usa navegador próprio, sem perfil,
para não brigar com o monitor.py pelo perfil_chrome.
"""
import asyncio, json, os, re, sys, time, urllib.parse

import monitor as m  # reaproveita config, parsers, Telegram e o truststore

BASE_DIR = m.BASE_DIR
MAX_RESULTADOS = 10
GRUPOS_VISTOS = set()
PENDENTES = {}  # (chat privado, message_id dos botões) -> termo; some se o bot reiniciar
# região -> (nome, caminho OLX, cidade FB). Amazonas vem do config.json (ver main)
REGIOES = {"cwb": ("Curitiba", "estado-pr/regiao-de-curitiba-e-paranagua", "curitiba")}


def tg(metodo, dados):
    return m.telegram(CFG, metodo, dados)


def _tg_longo(metodo, dados, timeout):
    import urllib.request
    url = f"https://api.telegram.org/bot{CFG['telegram_token']}/{metodo}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(dados).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


async def pesquisar(termo, regiao, sites=("OLX", "Facebook")):
    from playwright.async_api import async_playwright
    q = urllib.parse.quote(termo)
    _, olx, fb = REGIOES[regiao]
    tabela = m.ler_ofertas()
    mod = m.modelo(termo, tabela)  # termo é console? então só a categoria Consoles da OLX, título do modelo, sem acessório
    if mod:
        olx = "games/consoles-de-video-game/" + olx

    def serve(a):
        return a and a["titulo"] and (not mod or (mod[0].search(m.sem_acento(a["titulo"]))
                                                  and not m.acessorio(a["titulo"], tabela)))

    fontes = [
        ("OLX", f"https://www.olx.com.br/{olx}?q={q}&sf=1", m.JS_OLX, m.parse_olx, 5000),
        ("Facebook", f"https://www.facebook.com/marketplace/{fb}/search?query={q}"
                     f"&sortBy=creation_time_descend&exact=false", m.JS_FB, m.parse_fb, 7000),
    ]
    saida = {}
    async with async_playwright() as p:
        # com janela (fora da tela): headless leva 403 da OLX
        nav = await p.chromium.launch(channel="chrome", headless=False,
                                      args=["--disable-blink-features=AutomationControlled", "--window-position=-2400,-2400"])
        for site, url, js, parse, espera in [f for f in fontes if f[0] in sites]:
            pg = await nav.new_page(locale="pt-BR")
            try:
                ads = [a for a in map(parse, await m.coletar(pg, url, js, espera)) if serve(a)]
                saida[site] = ads[:MAX_RESULTADOS]
            except Exception as e:
                saida[site] = None
                m.log(f"BOT erro {site} '{termo}': {str(e).splitlines()[0]}")
            await pg.close()
        await nav.close()
    return saida


def formatar(site, termo, ads, lugar=""):
    if ads is None:
        return f"⚠️ {site}: não consegui acessar agora (a rede da SEINFRA às vezes bloqueia o Facebook)."
    onde = f" em {lugar}" if lugar else ""
    if not ads:
        return f"{site}: nenhum anúncio para \"{termo}\"{onde}."
    linhas = [f"🔎 {site} · \"{termo}\"{onde} · {len(ads)} mais recentes"]
    tabela = m.ler_ofertas()
    for a in ads:
        valor = f"R$ {a['preco']:,.0f}".replace(",", ".") if a["preco"] is not None else "s/ preço"
        fogo = "🔥 OFERTA · " if m.oferta(a, tabela) else ""
        linhas.append(f"\n• {fogo}{a['titulo']}\n  💰 {valor} · 📍 {a['local']}\n  {a['url']}")
    return "\n".join(linhas)[:4000]  # limite do Telegram é 4096


def responder(msg):
    texto = (msg.get("text") or "").strip()
    cmd = re.match(r"^/(busca|start|ajuda)(@\w+)?\s*(.*)$", texto, re.I | re.S)
    if not cmd:
        return
    acao, termo = cmd.group(1).lower(), cmd.group(3).strip()
    chat, quem = msg["chat"], msg["from"]
    privado = quem["id"]
    if acao in ("start", "ajuda") or not termo:
        if chat["type"] == "private" or acao == "busca":
            tg("sendMessage", {"chat_id": chat["id"], "text":
               "Digite /busca seguido do que procura. Ex.: /busca game boy advance\n"
               "Depois escolha Amazonas ou Curitiba; o resultado (OLX + Facebook) chega aqui no privado."})
        return
    botoes = [[{"text": nome, "callback_data": k} for k, (nome, _, _) in REGIOES.items()]]
    try:
        r = tg("sendMessage", {"chat_id": privado, "text": f"📍 Onde buscar \"{termo}\"?",
                               "reply_markup": json.dumps({"inline_keyboard": botoes})})
    except Exception:
        # Telegram recusa (403) se a pessoa nunca iniciou conversa com o bot
        tg("sendMessage", {"chat_id": chat["id"], "reply_to_message_id": msg["message_id"], "text":
           f"{quem.get('first_name', '')}, abra o @{CFG.get('bot_usuario', 'Buscapre_bot')} no privado, "
           "toque em Iniciar e mande o /busca de novo."})
        return
    PENDENTES[(privado, r["result"]["message_id"])] = termo


def escolheu(cb):
    """Toque num botão de região: troca os botões por "Buscando..." e manda o resultado."""
    regiao, msg = cb.get("data"), cb.get("message") or {}
    chave = (msg.get("chat", {}).get("id"), msg.get("message_id"))
    termo = PENDENTES.pop(chave, None)
    tg("answerCallbackQuery", {"callback_query_id": cb["id"]})
    if regiao not in REGIOES or termo is None:  # bot reiniciou ou botão já usado
        tg("sendMessage", {"chat_id": chave[0], "text": "Essa busca expirou. Mande o /busca de novo."})
        return
    lugar = REGIOES[regiao][0]
    tg("editMessageText", {"chat_id": chave[0], "message_id": chave[1],
                           "text": f"⏳ Buscando \"{termo}\" em {lugar} na OLX e no Facebook..."})
    m.log(f"BOT /busca '{termo}' [{lugar}] de {cb['from'].get('first_name')} ({chave[0]})")
    res = asyncio.run(pesquisar(termo, regiao))
    for site, ads in res.items():
        tg("sendMessage", {"chat_id": chave[0], "text": formatar(site, termo, ads, lugar),
                           "disable_web_page_preview": "true"})


def main():
    global CFG
    CFG = m.carregar_json(m.CONFIG, {})
    REGIOES.update({"am": ("Amazonas", CFG.get("olx_caminho_busca", "estado-am/regiao-de-manaus"), CFG["fb_cidade"])})
    REGIOES["cwb"] = REGIOES.pop("cwb")  # Amazonas primeiro nos botões
    tg("setMyCommands", {"commands": json.dumps([
        {"command": "busca", "description": "Buscar na OLX e Facebook (Amazonas ou Curitiba). Ex.: /busca game boy"}])})
    # descarta mensagens antigas acumuladas enquanto o bot estava desligado
    pend = tg("getUpdates", {"offset": -1}).get("result", [])
    offset = pend[-1]["update_id"] + 1 if pend else 0
    m.log("BOT no ar")
    while True:
        try:
            r = _tg_longo("getUpdates", {"offset": offset, "timeout": 50,
                                         "allowed_updates": '["message","my_chat_member","callback_query"]'}, 70)
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                c = (u.get("my_chat_member") or u.get("message") or {}).get("chat", {})
                if c.get("type") in ("group", "supergroup") and c["id"] not in GRUPOS_VISTOS:
                    GRUPOS_VISTOS.add(c["id"])  # o bot consome o getUpdates; o ID do grupo fica no log
                    m.log(f"BOT grupo: {c.get('title')} chat_id={c['id']}")
                if u.get("message", {}).get("text", "").startswith("/") or u.get("callback_query"):  # diagnóstico 20/10/2026
                    x = u.get("message") or u["callback_query"]
                    m.log(f"BOT recebeu de {x['from'].get('first_name')} em {c.get('type') or 'botão'}: "
                          f"{x.get('text') or x.get('data')}")
                try:
                    if u.get("message"):
                        responder(u["message"])
                    elif u.get("callback_query"):
                        escolheu(u["callback_query"])
                except Exception as e:
                    corpo = e.read().decode(errors="replace")[:300] if hasattr(e, "read") else ""  # motivo do 400
                    m.log(f"BOT erro ao responder: {type(e).__name__}: {e} {corpo}")
        except Exception as e:
            m.log(f"BOT erro no getUpdates: {type(e).__name__}: {e}")
            time.sleep(15)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        CFG = {}
        assert "nenhum" in formatar("OLX", "x", [])
        f = formatar("OLX", "gb", [{"titulo": "Game Boy", "preco": 300.0, "local": "Manaus", "url": "u"}])
        assert "R$ 300" in f and "Game Boy" in f
        assert "🔥 OFERTA" in formatar("OLX", "ps3", [{"titulo": "PS3 Slim", "preco": 280.0, "local": "", "url": "u"}])
        assert "🔥" not in formatar("OLX", "ps3", [{"titulo": "PS3 Slim", "preco": 900.0, "local": "", "url": "u"}])
        assert "em Curitiba" in formatar("OLX", "x", [], "Curitiba")
        enviados = []
        tg = lambda metodo, dados: enviados.append((metodo, dados)) or {"result": {"message_id": 7}}
        responder({"text": "/busca gb", "chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "message_id": 1})
        assert PENDENTES[(1, 7)] == "gb" and "Curitiba" in enviados[-1][1]["reply_markup"]
        escolheu({"id": "c", "data": "xx", "message": {"chat": {"id": 1}, "message_id": 7}, "from": {}})
        assert "expirou" in enviados[-1][1]["text"]  # região inválida não dispara busca
        print("selftest ok")
    else:
        import traceback
        while True:  # pythonw não tem stderr: sem isto o bot morria calado (18/10/2026)
            try:
                main()
            except BaseException as e:
                m.log("BOT caiu: " + traceback.format_exc().replace("\n", " | ")[-800:])
                if isinstance(e, KeyboardInterrupt):
                    raise
                time.sleep(60)
