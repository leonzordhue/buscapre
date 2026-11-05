"""Monitor de anúncios novos na OLX e no Facebook Marketplace, com aviso no Telegram.

Roda uma varredura e sai; o Agendador de Tarefas chama a cada 30 min.
  python monitor.py              varredura normal
  python monitor.py --teste      manda mensagem de teste no Telegram
  python monitor.py --chat-id    descobre o chat_id (mande "oi" pro bot antes)
  python monitor.py --login      abre o Facebook para logar (opcional; hoje funciona sem login)
  python monitor.py --selftest   testa o parser de preço/filtros

Chrome precisa de JANELA: em modo headless a OLX devolve 403 (Cloudflare). A janela abre fora da tela.
"""
import asyncio, json, os, re, sys, time, unicodedata, urllib.parse, urllib.request
from datetime import datetime

import truststore
truststore.inject_into_ssl()  # usa os certificados do Windows (a rede faz inspeção HTTPS com CA própria)

if sys.stdout:  # pythonw (Agendador) não tem console
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE_DIR, "config.json")
BUSCAS = os.path.join(BASE_DIR, os.environ.get("MONITOR_BUSCAS", "buscas.txt"))  # nuvem: buscas-new.txt (New 3DS/2DS a cada 5 min)
DADOS = os.environ.get("MONITOR_DADOS", BASE_DIR)  # na nuvem (GitHub Actions) o estado fica em estado/
VISTOS = os.path.join(DADOS, "vistos.json")
ANUNCIOS = os.path.join(DADOS, "anuncios.json")  # id -> dados do anúncio + msg_id no Telegram + status (ativo/vendido/removido)
CHECAR_POR_RODADA = 20  # anúncios ativos reabertos por rodada para ver se saíram do ar (os checados há mais tempo primeiro)
LOG = os.path.join(DADOS, "monitor.log")
PERFIL = os.path.join(BASE_DIR, "perfil_chrome")


def log(msg):
    linha = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(linha)
    if os.path.exists(LOG) and os.path.getsize(LOG) > 1_000_000:
        os.replace(LOG, LOG + ".old")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def carregar_json(caminho, padrao):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return padrao


def salvar_json(caminho, dados):
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
    os.replace(tmp, caminho)  # gravação atômica: não corrompe vistos.json se cair no meio


def sem_acento(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def preco(txt):
    """'R$ 1.100' -> 1100.0; 'R$1.234,56' -> 1234.56; sem número -> None."""
    m = re.search(r"R\$\s?([\d.]+(?:,\d{1,2})?)", txt or "")
    return float(m.group(1).replace(".", "").replace(",", ".")) if m else None


def ler_buscas():
    """Linha: termos separados por vírgula | preço mín | preço máx | título deve conter (regex)."""
    buscas = []
    with open(BUSCAS, encoding="utf-8") as f:
        for n, linha in enumerate(f, 1):
            linha = linha.split("#", 1)[0].strip()
            if not linha:
                continue
            partes = [p.strip() for p in linha.split("|", 3)] + ["", "", ""]  # a regex (4ª coluna) pode ter '|'
            termos = [t.strip() for t in partes[0].split(",") if t.strip()]
            buscas.append({
                "nome": termos[0],
                "termos": termos,
                "min": float(partes[1]) if partes[1] else None,
                "max": float(partes[2]) if partes[2] else None,
                "contem": re.compile(partes[3], re.I) if partes[3] else None,
            })
    return buscas


OFERTAS = os.path.join(BASE_DIR, "ofertas.txt")
NOME_OFERTAS = "🔥 ofertas"  # chave em vistos.json dos anúncios já alertados como oferta
# título (sem acento) que começa com peça/acessório ou diz "para 3DS", "p/ PS3"... (pedido do Paulo, 20/10/2026)
RE_ACESSORIO = re.compile(
    r"^\W*(fontes?|carregador(es)?|capas?|case|bolsa|pelicula|caneta|cabos?|controles?|adaptador|cartucho|jogos?|fita"
    r"|cd|dvd|pecas?|carcaca|tela|bateria|suporte|cartao|skin|stylus|r4|botao|botoes|lente|cooler|hd|volante|headset"
    r"|fone|grip|dock|base|faco)\b"
    r"|\b(jogo (de|do|para|pra)|loose|so o jogo|apenas o jogo|desbloqueio)\b"  # "midia fisica" saiu: "PS5 slim mídia física" é console (05/11/2026)
    r"|\b(para|pra|p/|compativel com)\s*(o |a )?(nintendo|new|playstation|ps\s?\d|xbox|[23]\s?ds|switch|wii|game\s*boy|psp|ds\b|super)")
RE_DEFEITO = re.compile(r"^\W*compro|defeito|nao liga|nao funciona|nao le\b|para pecas|pra pecas|retirada de pecas|sucata"
                        r"|quebrad|\bbloquead")  # \b: "desbloqueado" é console bom


def ler_ofertas():
    """Linha: regex do modelo | teto R$ | nome. A regex pode ter '|', por isso corta pela direita."""
    tabela = []
    with open(OFERTAS, encoding="utf-8") as f:
        for linha in f:
            linha = linha.split("#", 1)[0].strip()
            if linha:
                rx, teto, nome = (p.strip() for p in linha.rsplit("|", 2))
                tabela.append((re.compile(rx), float(teto), nome))
    return tabela


def modelo(titulo, tabela=None):
    """Primeiro console do ofertas.txt citado no título -> (regex, teto, nome) ou None."""
    t = sem_acento(titulo)
    return next((o for o in (tabela or ler_ofertas()) if o[0].search(t)), None)


def acessorio(titulo, tabela=None):
    """Fonte, capa, jogo, controle... de console. Só vale se o título cita um console (capa de chuva passa).
    Console citado só no fim ("Super Mario Advance 4 - R$ 200 - Gameboy Advance") é jogo avulso (20/10/2026)."""
    t = sem_acento(titulo)
    o = modelo(t, tabela)
    tardio = o and o[0].search(t).start() > 25 and not re.search(r"console|aparelho|video\s?game", t)
    return bool(o) and bool(RE_ACESSORIO.search(t) or tardio)


def oferta(a, tabela=None):
    """Console inteiro com preço até o teto do ofertas.txt -> (regex, teto, nome) ou None.
    Abaixo de 25% do teto é isca/troca; título com defeito/peças fica de fora."""
    t = sem_acento(a["titulo"])
    o = modelo(t, tabela)
    if not o or a.get("preco") is None or acessorio(t, tabela) or RE_DEFEITO.search(t):
        return None
    return o if o[1] * 0.25 <= a["preco"] <= o[1] else None


def passa_filtro(b, titulo, valor):
    if acessorio(titulo):
        return False
    if b["contem"] and not b["contem"].search(sem_acento(titulo)):
        return False
    if valor is not None and b["min"] is not None and valor < b["min"]:
        return False
    if valor is not None and b["max"] is not None and valor > b["max"]:
        return False
    return True


# ---------- coleta ----------

JS_OLX = r"""() => [...document.querySelectorAll('section.olx-adcard')].map(s => {
  const a = s.querySelector('a.olx-adcard__link');
  return {url: a ? a.href.split('?')[0] : '', titulo: a ? a.innerText.trim() : '',
          linhas: s.innerText.split('\n').map(x => x.trim()).filter(Boolean),
          img: (s.querySelector('img') || {}).src || ''};
})"""

JS_FB = r"""() => { const out = {}; for (const a of document.querySelectorAll('a[href*="/marketplace/item/"]')) {
  const m = a.href.match(/\/marketplace\/item\/(\d+)/); if (!m || out[m[1]]) continue;
  out[m[1]] = {id: m[1], linhas: a.innerText.split('\n').map(x => x.trim()).filter(Boolean),
               img: (a.querySelector('img') || {}).src || ''}; }
  return Object.values(out); }"""

JS_PAGINA = r"""() => ({titulo: document.title, texto: document.body ? document.body.innerText.slice(0, 6000) : '',
  og: (document.querySelector('meta[property="og:image"]') || {}).content || '',
  og_titulo: (document.querySelector('meta[property="og:title"]') || {}).content || ''})"""

SO_PRECO = re.compile(r"^R\$\s?[\d.]+(,\d{1,2})?$")


def situacao(site, status_http, titulo, texto):
    """Página do anúncio -> 'ativo' | 'vendido' | 'removido' | None (não deu para saber; tenta de novo depois).
    Padrões vistos em 15/10/2026: OLX removido = HTTP 410 e título "Anúncio não encontrado | OLX";
    FB removido = "Este conteúdo não está disponível"; FB vendido = linha "Vendido" seguida de " · título"."""
    if site == "OLX":
        if status_http in (404, 410) or titulo.startswith("Anúncio não encontrado"):
            return "removido"
        return "ativo" if status_http == 200 and "OLX" in titulo else None
    if "Este conteúdo não está disponível" in texto or "This content isn't available" in texto:
        return "removido"
    if re.search(r"(^|\n)(Vendido|Sold)\n\s*·", texto):
        return "vendido"
    return "ativo" if "Marketplace" in titulo else None


def parse_olx(c):
    m = re.search(r"-(\d{9,})$", c["url"])
    if not m:
        return None
    precos = [l for l in c["linhas"] if SO_PRECO.match(l)]
    local = next((l for l in c["linhas"] if re.match(r"^[A-ZÀ-Ú][^,]+, .+", l) and "R$" not in l), "")
    # com desconto a OLX mostra o preço antigo antes do atual
    return {"id": "olx:" + m.group(1), "titulo": c["titulo"], "preco": preco(precos[-1]) if precos else None,
            "local": local, "url": c["url"], "img": c["img"], "site": "OLX"}


def parse_fb(c):
    linhas = c["linhas"]
    g = re.match(r"^(?:Acabou de chegar|Just listed)?(R\$\s?[\d.]+(?:,\d{1,2})?)(.+)$", linhas[0]) if len(linhas) == 1 else None
    if g:  # FB às vezes cola tudo numa linha: "Acabou de chegarR$249Nitemdo 3ds vermelhoManaus, AM" (20/10/2026)
        loc = re.match(r"^(.*[a-zà-ú0-9)])([A-ZÀ-Ú][^,]*, [A-Z]{2})$", g.group(2))
        linhas = [g.group(1), *(loc.groups() if loc else [g.group(2)])]
    linhas = [l for l in linhas if l.lower() not in ("acabou de chegar", "just listed")]
    precos = [l for l in linhas if l.startswith("R$") or l.lower() == "gratuito"]
    resto = [l for l in linhas if l not in precos]
    return {"id": "fb:" + c["id"], "titulo": resto[0] if resto else "", "preco": preco(precos[0]) if precos else None,
            "local": resto[-1] if len(resto) > 1 else "", "url": f"https://www.facebook.com/marketplace/item/{c['id']}/",
            "img": c["img"], "site": "Facebook"}


async def coletar(pagina, url, js, espera_ms, tentativas=3):
    """Na nuvem o Cloudflare da OLX barra as 1as páginas ("Attention Required") e libera depois (05/11/2026)."""
    for n in range(tentativas):
        achados = await _coletar(pagina, url, js, espera_ms)
        if achados or n == tentativas - 1:
            return achados
        await pagina.wait_for_timeout(10000)


async def _coletar(pagina, url, js, espera_ms):
    await pagina.goto(url, wait_until="domcontentloaded", timeout=60000)
    await pagina.wait_for_timeout(espera_ms)
    # OLX só preenche o card quando ele aparece na tela (20/10/2026): coleta a cada passo da rolagem e junta
    juntos = {}
    for _ in range(16):
        for c in await pagina.evaluate(js):
            k = c.get("url") or c.get("id")
            if len(c["linhas"]) > len(juntos.get(k, {}).get("linhas", [])):
                juntos[k] = c
        await pagina.evaluate("window.scrollBy(0, 700)")
        await pagina.wait_for_timeout(500)
    return list(juntos.values())


async def abrir(pg, url):
    r = await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    await pg.wait_for_timeout(4000)
    d = await pg.evaluate(JS_PAGINA)
    d["status"] = r.status if r else None
    return d


async def varrer(cfg, buscas, registro=None):
    """Busca os anúncios; com `registro`, também abre os novos (foto/título da página) e reabre os já
    avisados para ver se saíram do ar. Devolve (achados, mudancas[(id, 'vendido'|'removido')])."""
    from playwright.async_api import async_playwright
    registro = {} if registro is None else registro
    mudancas = []
    achados = {}  # nome da busca -> {id: anúncio}
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PERFIL, channel="chrome", headless=False, locale="pt-BR",
            args=["--disable-blink-features=AutomationControlled", "--window-position=-2400,-2400"])
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        fora = set(cfg.get("pular_sites", []))  # nuvem: ["Facebook"] (manda para o login); site que falhou nesta rodada (ex.: firewall bloqueando o Facebook) é pulado até a próxima
        tabela = ler_ofertas()
        # pseudo-busca: categoria Consoles da OLX inteira, sem termo; só fica o que é oferta (alerta mesmo fora das buscas)
        # ponytail: Facebook fica de fora da varredura geral (busca sem termo não existe lá); entra só pelas buscas
        varredura = [] if cfg.get("sem_ofertas") else [{"nome": NOME_OFERTAS, "termos": [""], "oferta": True}]
        for b in buscas + varredura:
            itens = achados.setdefault(b["nome"], {})
            for termo in b["termos"]:
                q = urllib.parse.quote(termo)
                fontes = [
                    ("OLX", f"https://www.olx.com.br/{cfg['olx_caminho']}?q={q}&sf=1", JS_OLX, parse_olx, 5000),
                    ("Facebook", f"https://www.facebook.com/marketplace/{cfg['fb_cidade']}/search?query={q}"
                                 f"&sortBy=creation_time_descend&exact=false", JS_FB, parse_fb, 7000),
                ]
                for site, url, js, parse, espera in fontes:
                    if site in fora or (site == "Facebook" and not termo):
                        continue
                    try:
                        brutos = await coletar(pg, url, js, espera)
                        bons = [a for a in map(parse, brutos) if a and sem_acento(cfg.get("local_contem", "")) in sem_acento(a["local"])
                                and (oferta(a, tabela) if b.get("oferta")
                                                                        else passa_filtro(b, a["titulo"], a["preco"]))]
                        for a in bons:
                            itens.setdefault(a["id"], a)
                        log(f"{site} '{termo}': {len(brutos)} lidos, {len(bons)} no filtro")
                        if not brutos:
                            log(f"  aviso: 0 anúncios em {site} (bloqueio ou página mudou) — título: {await pg.title()}")
                    except Exception as e:
                        log(f"ERRO {site} '{termo}' (pulando {site} nesta rodada): {str(e).splitlines()[0]}")
                        fora.add(site)
                        # página de erro do Chrome continua navegando e derruba a próxima busca; troca a aba
                        await pg.close()
                        pg = await ctx.new_page()

        # anúncio novo: foto principal e título vêm da página dele (no card a foto às vezes ainda não carregou)
        for itens in achados.values():
            for a in itens.values():
                if a["id"] in registro or a["site"] in fora:
                    continue
                try:
                    d = await abrir(pg, a["url"])
                    if d["og"]:
                        a["img"] = d["og"]
                    if d["og_titulo"] and (not a["titulo"] or "R$" in a["titulo"]):
                        a["titulo"] = d["og_titulo"]  # FB às vezes cola tudo numa linha: "Acabou de chegarR$90..."
                except Exception as e:
                    log(f"  página de {a['id']} não abriu (vai com a foto do card): {str(e).splitlines()[0]}")

        # anúncios já avisados: ainda estão no ar?
        ativos = sorted((r for r in registro.values() if r.get("status") == "ativo" and r["site"] not in fora),
                        key=lambda r: r.get("checado_em", ""))[:CHECAR_POR_RODADA]
        for r in ativos:
            try:
                d = await abrir(pg, r["url"])
            except Exception as e:
                log(f"ERRO checando {r['id']} (pulando {r['site']} nesta rodada): {str(e).splitlines()[0]}")
                fora.add(r["site"])
                await pg.close()
                pg = await ctx.new_page()
                continue
            s = situacao(r["site"], d["status"], d["titulo"], d["texto"])
            r["checado_em"] = f"{datetime.now():%Y-%m-%d %H:%M}"
            if s in ("vendido", "removido"):
                mudancas.append((r["id"], s))
            elif s is None:
                log(f"  {r['id']}: situação indefinida (HTTP {d['status']}, título '{d['titulo'][:60]}')")
        log(f"checagem: {len(ativos)} anúncio(s) reaberto(s), {len(mudancas)} saíram do ar")
        await ctx.close()
    return achados, mudancas


# ---------- Telegram ----------

def discord(cfg, metodo, dados):
    """Mesma interface do telegram(), postando no webhook do Discord (nuvem, 05/11/2026). Resposta a mensagem vira avulsa."""
    u = cfg["discord_webhook"]
    h = {"Content-Type": "application/json", "User-Agent": "Buscapre"}  # sem User-Agent o Discord recusa
    if metodo.startswith("edit"):
        corpo, u, verbo = {"content": (dados.get("caption") or dados.get("text"))[:2000]}, f"{u}/messages/{dados['message_id']}", "PATCH"
    else:
        corpo, u, verbo = {"username": "Buscapre", "content": (dados.get("text") or dados.get("caption") or "")[:2000]}, u + "?wait=true", "POST"
        if dados.get("photo"):
            corpo["embeds"] = [{"image": {"url": dados["photo"]}}]
    req = urllib.request.Request(u, data=json.dumps(corpo).encode(), headers=h, method=verbo)
    with urllib.request.urlopen(req, timeout=30) as r:
        return {"ok": True, "result": {"message_id": json.load(r)["id"]}}


def telegram(cfg, metodo, dados):
    if cfg.get("discord_webhook"):
        return discord(cfg, metodo, dados)
    url = f"https://api.telegram.org/bot{cfg['telegram_token']}/{metodo}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(dados).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def telegram_foto(cfg, dados, foto):
    """sendPhoto enviando o arquivo (multipart): o Telegram recusa algumas URLs do FB/OLX quando busca sozinho."""
    b = f"----monitor{int(time.time() * 1000)}"
    corpo = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in dados.items())
    corpo += f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="foto.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode()
    corpo += foto + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{cfg['telegram_token']}/sendPhoto", data=corpo,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def baixar(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def valor_txt(a):
    return f"R$ {a['preco']:,.0f}".replace(",", ".") if a.get("preco") is not None else "preço não informado"


def avisar(cfg, a, busca, chat=None, texto=None):
    """Manda o anúncio novo, com foto sempre que der. Devolve (resposta, texto, tem_foto)."""
    texto = texto or cfg.get("prefixo", "") + f"🆕 {a['site']} · {busca}\n{a['titulo']}\n💰 {valor_txt(a)}\n📍 {a['local']}\n{a['url']}"
    base = {"chat_id": chat or cfg["telegram_chat_id"]}
    if a["img"]:
        try:
            if cfg.get("discord_webhook"):
                raise RuntimeError("Discord: foto vai pela URL")
            return telegram_foto(cfg, {**base, "caption": texto}, baixar(a["img"])), texto, True
        except Exception as e:
            log(f"  foto por arquivo falhou ({e}); tentando pela URL")
        try:
            return telegram(cfg, "sendPhoto", {**base, "photo": a["img"], "caption": texto}), texto, True
        except Exception:
            pass  # foto do FB às vezes expira/recusa; cai para texto
    return telegram(cfg, "sendMessage", {**base, "text": texto}), texto, False


def avisar_saida(cfg, r, s):
    """Anúncio vendido/removido: marca a mensagem original e responde a ela (ou manda avulso, se não houver)."""
    rotulo = "🔴 VENDIDO" if s == "vendido" else "❌ SAIU DO AR"
    chat = r.get("chat") or cfg["telegram_chat_id"]
    if r.get("msg_id") and r.get("texto"):
        try:  # mensagem antiga pode não ser mais editável; o aviso em resposta sai de qualquer jeito
            campo, metodo = ("caption", "editMessageCaption") if r.get("foto") else ("text", "editMessageText")
            telegram(cfg, metodo, {"chat_id": chat, "message_id": r["msg_id"], campo: f"{rotulo}\n{r['texto']}"[:1024]})
        except Exception as e:
            log(f"  não editou a mensagem {r['msg_id']}: {e}")
    desde = (r.get("desde") or "")[:10]
    texto = (f"{rotulo} · {r['site']}\n{r['titulo']}\n💰 {valor_txt(r)}\n"
             f"Visto no ar desde {desde[8:10]}/{desde[5:7]}; saiu em {datetime.now():%d/%m às %H:%M}\n{r['url']}")
    dados = {"chat_id": chat, "text": texto}
    if r.get("msg_id"):
        dados.update(reply_to_message_id=r["msg_id"], allow_sending_without_reply="true")
    return telegram(cfg, "sendMessage", dados)


def texto_baixa(r, antes):
    queda = round(100 * (antes - r["preco"]) / antes)
    de = f"R$ {antes:,.0f}".replace(",", ".")
    return f"📉 BAIXOU O PREÇO · {r['site']}\n{r['titulo']}\n💰 de {de} por {valor_txt(r)} (-{queda}%)\n📍 {r['local']}\n{r['url']}"


def avisar_baixa(cfg, r, antes):
    """Preço caiu num anúncio acompanhado: responde à mensagem original, se houver; senão manda avulso (05/11/2026)."""
    dados = {"chat_id": r.get("chat") or cfg["telegram_chat_id"], "text": texto_baixa(r, antes)}
    if r.get("msg_id"):
        dados.update(reply_to_message_id=r["msg_id"], allow_sending_without_reply="true")
    return telegram(cfg, "sendMessage", dados)


def telegram_ok(cfg):
    return bool(cfg.get("discord_webhook") or (cfg.get("telegram_token") and cfg.get("telegram_chat_id")))


# ---------- principal ----------

RE_AVISADO = re.compile(r"^(\S+ \S+) AVISADO(?: com foto| SEM foto)?: (OLX|Facebook) (.*) (\S+) (https://\S+)$")


def semear_do_log(registro, vistos):
    """Anúncios avisados antes do anuncios.json existir (14-15/10/2026) e que já sumiram da busca: tira URL,
    título e preço das linhas AVISADO do monitor.log, para também serem checados (sem msg_id: aviso vai avulso)."""
    faltam = {i for ids in vistos.values() for i in ids} - set(registro)
    if not faltam:
        return
    busca_de = {i: nome for nome, ids in vistos.items() for i in ids}
    for arq in (LOG + ".old", LOG):
        if not os.path.exists(arq):
            continue
        for linha in open(arq, encoding="utf-8", errors="replace"):
            m = RE_AVISADO.match(linha.strip())
            if not m:
                continue
            quando, site, titulo, valor, url = m.groups()
            num = re.search(r"(\d{9,})/?$", url)
            i = (("olx:" if site == "OLX" else "fb:") + num.group(1)) if num else None
            if i in faltam:
                registro[i] = {"id": i, "site": site, "titulo": titulo, "preco": None if valor == "None" else float(valor),
                               "local": "", "url": url, "img": "", "busca": busca_de[i], "status": "ativo", "desde": quando[:16]}
    log(f"registro: {len(faltam)} anúncio(s) antigo(s) sem dados; {sum(1 for i in faltam if i in registro)} recuperado(s) do log")

def main():
    cfg = carregar_json(CONFIG, {})
    if "--selftest" in sys.argv:
        assert preco("R$ 1.100") == 1100 and preco("R$1.234,56") == 1234.56 and preco("Gratuito") is None
        b = {"contem": re.compile(r"3\s?ds", re.I), "min": 300, "max": None}
        assert passa_filtro(b, "Nintendo 3DS XL", 800) and not passa_filtro(b, "Nintendo 3DS XL", 90)
        assert not passa_filtro(b, "PS Vita", 800) and passa_filtro(b, "New 3ds", None)
        assert not passa_filtro(b, "Fonte carregador original Nintendo 3DS", 350)
        t = ler_ofertas()
        assert modelo("Nintendo New 3DS XL", t)[2] == "New 3DS XL" and modelo("new 3ds", t)[2] == "New 3DS" and modelo("Nintendo 3ds", t)[2] == "3DS"
        assert modelo("Bicicleta aro 29", t) is None
        for x in ("Fonte carregador Nintendo 3DS", "Capa para 3ds xl", "Jogo Pokemon X 3DS", "Carregador p/ 3DS",
                  "Controle PS3 original", "Case Nintendo Switch", "Star Fox 3D - jogo de 3DS", "PES 2013 - 3DS - loose",
                  "Faço D3SBL0Q3IO Nintendo 3DS/2DS", "Resident Evil: Mercenaries 3DS",
                  "Super Mario Bros. 3 Super Mario Advance 4 - R$: 200 - 98122 - Gameboy Advance"):
            assert acessorio(x, t), x
        for x in ("Nintendo 3DS XL com carregador e jogos", "Console PS3 slim + 2 controles", "Capa de chuva",
                  "Nintendo 3ds desbloqueado", "3DS XL com jogos de brinde", "Vendo ou troco Nintendo switch oled",
                  "Kit completo com 2 controles e console PS3"):
            assert not acessorio(x, t), x
        assert oferta({"titulo": "PlayStation 3 Slim 250GB", "preco": 300}, t)[2] == "PS3"
        assert not oferta({"titulo": "PS3 Slim", "preco": 450}, t)
        assert not oferta({"titulo": "PS3 com defeito", "preco": 150}, t)
        assert not oferta({"titulo": "PS3 troco", "preco": 10}, t) and not oferta({"titulo": "Controle PS3", "preco": 50}, t)
        assert not oferta({"titulo": "PS3", "preco": None}, t)
        x = texto_baixa({"site": "OLX", "titulo": "PSP", "preco": 1200.0, "local": "Manaus", "url": "u"}, 1600.0)
        assert "de R$ 1.600 por R$ 1.200 (-25%)" in x, x
        assert oferta({"titulo": "PS3 SUPER SLIM DESBLOQUEADO", "preco": 290}, t)
        assert not oferta({"titulo": "Compro PS3", "preco": 200}, t)
        assert modelo("Nintendo swith lite", t)[2] == "Switch Lite" and modelo("XBOX SERIE S 512GB", t)[2] == "Xbox Series S"
        a = parse_olx({"url": "https://am.olx.com.br/x/nintendo-3ds-1234567890", "titulo": "3DS",
                       "linhas": ["3DS", "R$ 90", "R$ 80", "em até 1x de R$ 80,00", "Manaus, Coroado"], "img": ""})
        assert a["preco"] == 80 and a["local"] == "Manaus, Coroado", a
        f = parse_fb({"id": "1", "linhas": ["Acabou de chegar", "R$800", "R$850", "Nintendo 3ds", "Manaus, AM"], "img": ""})
        assert f["preco"] == 800 and f["titulo"] == "Nintendo 3ds" and f["local"] == "Manaus, AM", f
        f = parse_fb({"id": "2", "linhas": ["Acabou de chegarR$249Nitemdo 3ds vermelho tudo funcionandoManaus, AM"], "img": ""})
        assert f["preco"] == 249 and f["titulo"] == "Nitemdo 3ds vermelho tudo funcionando" and f["local"] == "Manaus, AM", f
        f = parse_fb({"id": "3", "linhas": ["R$300Playstation 3 slimRio Preto da Eva, AM"], "img": ""})
        assert f["titulo"] == "Playstation 3 slim" and f["local"] == "Rio Preto da Eva, AM", f
        # páginas reais capturadas em 15/10/2026
        assert situacao("OLX", 410, "Anúncio não encontrado | OLX", "A página não foi encontrada...") == "removido"
        assert situacao("OLX", 200, "NINTENDO 3DS - Consoles de Vídeo Game - Manaus 1544420890 | OLX", "") == "ativo"
        assert situacao("OLX", 403, "Just a moment...", "") is None  # Cloudflare: não decide
        assert situacao("Facebook", 200, "Facebook", "Entrar\nEste conteúdo não está disponível no momento\n") == "removido"
        assert situacao("Facebook", 200, "Nintendo 3ds | Facebook Marketplace", "Vestuário\nVendido\n\xa0\n · Nintendo 3ds\nR$800") == "vendido"
        assert situacao("Facebook", 200, "Nintendo 3ds | Facebook Marketplace", "Categorias\nVendido por\nR$800") == "ativo"
        print("selftest ok")
        return
    if "--chat-id" in sys.argv:
        r = telegram(cfg, "getUpdates", {})
        chats = {}  # inclui grupo: entrada do bot no grupo vem como my_chat_member
        for u in r.get("result", []):
            for k in ("message", "my_chat_member", "channel_post"):
                c = (u.get(k) or {}).get("chat")
                if c:
                    chats[c["id"]] = c.get("title") or c.get("first_name") or ""
        print("chat_id encontrado(s):", chats or "nenhum — mande uma mensagem pro bot e rode de novo")
        return
    if "--teste" in sys.argv:
        print(telegram(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"], "text": "✅ Monitor OLX/Facebook ligado."}))
        return
    if "--login" in sys.argv:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(PERFIL, channel="chrome", headless=False)
            ctx.pages[0].goto("https://www.facebook.com/")
            input("Faça login na janela e depois aperte ENTER aqui...")
            ctx.close()
        return

    buscas = ler_buscas()
    vistos = carregar_json(VISTOS, {})
    registro = carregar_json(ANUNCIOS, {})
    semear_do_log(registro, vistos)
    achados, mudancas = asyncio.run(varrer(cfg, buscas, registro))
    agora = f"{datetime.now():%Y-%m-%d %H:%M}"
    novos_total = 0
    for nome, itens in achados.items():
        ja = set(vistos.get(nome, []))
        for a in itens.values():  # anúncio já avisado antes do registro existir entra aqui na 1ª vez que reaparece (sem msg_id)
            r = registro.setdefault(a["id"], {**a, "busca": nome, "status": "ativo", "desde": agora})
            r["visto_em"] = agora
            antes = r.get("preco")
            if a["preco"] is not None and antes and a["preco"] < antes:  # baixou o preço (pedido do Paulo, 20/10/2026)
                r["preco"] = a["preco"]
                if telegram_ok(cfg):  # Paulo: avisa qualquer queda, não só de anúncio já avisado (05/11/2026)
                    try:
                        avisar_baixa(cfg, r, antes)
                        novos_total += 1
                        log(f"BAIXOU {antes:.0f} -> {a['preco']:.0f}: {r['site']} {r['titulo']} {r['url']}")
                        time.sleep(1)
                    except Exception as e:
                        r["preco"] = antes  # tenta avisar de novo na próxima rodada
                        log(f"ERRO Telegram (baixa): {e}")
        if nome == NOME_OFERTAS:
            continue  # alerta de oferta sai no bloco de baixo, com destaque
        novos = [a for i, a in itens.items() if i not in ja]
        # sem "1ª varredura silenciosa": o Paulo quer receber também o que já está no ar
        for a in novos:
            if not telegram_ok(cfg):
                log(f"NOVO (Telegram não configurado): {a['site']} {a['titulo']} {a['preco']} {a['url']}")
                continue  # não marca como visto: avisa quando o Telegram for configurado
            try:
                resp, texto, foto = avisar(cfg, a, nome)
                registro[a["id"]].update(msg_id=resp["result"]["message_id"], chat=cfg["telegram_chat_id"], texto=texto, foto=foto)
                ja.add(a["id"])
                novos_total += 1
                log(f"AVISADO{' com foto' if foto else ' SEM foto'}: {a['site']} {a['titulo']} {a['preco']} {a['url']}")
                time.sleep(1)
            except Exception as e:
                log(f"ERRO Telegram: {e}")
        vistos[nome] = list(ja)
    # oferta boa (de qualquer busca ou da varredura geral): alerta em destaque no grupo e no privado do Paulo
    ja = set(vistos.get(NOME_OFERTAS, []))
    tabela = ler_ofertas()
    todos = {i: a for itens in achados.values() for i, a in itens.items()}
    for a in todos.values():
        o = oferta(a, tabela)
        if not o or a["id"] in ja or not telegram_ok(cfg):
            continue
        teto = f"R$ {o[1]:,.0f}".replace(",", ".")
        texto = (f"🔥🔥🔥 OFERTA BOA · {o[2]} 🔥🔥🔥\n(teto da tabela: {teto})\n{a['site']} · {a['titulo']}\n"
                 f"💰 {valor_txt(a)}\n📍 {a['local']}\n{a['url']}")
        chats = [cfg["telegram_chat_id"]] + [c for c in [cfg.get("telegram_chat_id_paulo")] if c and c != cfg["telegram_chat_id"]]
        try:
            for chat in chats:
                resp, _, foto = avisar(cfg, a, None, chat=chat, texto=texto)
                if chat == cfg["telegram_chat_id"] and not registro[a["id"]].get("msg_id"):
                    registro[a["id"]].update(msg_id=resp["result"]["message_id"], chat=chat, texto=texto, foto=foto)
                time.sleep(1)
            ja.add(a["id"])
            novos_total += 1
            log(f"OFERTA ({o[2]} <= {o[1]:.0f}): {a['site']} {a['titulo']} {a['preco']} {a['url']}")
        except Exception as e:
            log(f"ERRO Telegram (oferta): {e}")
    vistos[NOME_OFERTAS] = list(ja)
    for i, s in mudancas:
        r = registro[i]
        try:
            if telegram_ok(cfg):
                avisar_saida(cfg, r, s)
            r.update(status=s, saiu_em=agora)
            log(f"SAIU DO AR ({s}): {r['site']} {r['titulo']} {r['url']}")
            time.sleep(1)
        except Exception as e:
            log(f"ERRO Telegram (saída): {e}")  # não muda o status: tenta avisar de novo na próxima rodada
    # esquece vendidos/removidos depois de 30 dias
    limite = f"{datetime.fromtimestamp(time.time() - 30 * 86400):%Y-%m-%d}"
    for i in [i for i, r in registro.items() if r.get("status") != "ativo" and r.get("saiu_em", "9") < limite]:
        del registro[i]
    salvar_json(VISTOS, vistos)
    salvar_json(ANUNCIOS, registro)
    log(f"fim: {novos_total} aviso(s) enviado(s), {len(mudancas)} saída(s) do ar")


if __name__ == "__main__":
    main()
