"""/busca do Discord (nuvem): o Worker dispara o workflow busca.yml com o termo; aqui busca na OLX de Manaus
e posta os 10 mais recentes no #preços pelo webhook. Facebook fica de fora: de servidor ele cai no login.
  python busca.py "ps2"
"""
import asyncio, os, sys
import monitor as m
import bot as b


def pedacos(texto, limite=1900):  # Discord aceita até 2000 caracteres por mensagem
    atual = ""
    for bloco in texto.split("\n\n"):
        if atual and len(atual) + len(bloco) + 2 > limite:
            yield atual
            atual = ""
        atual = f"{atual}\n\n{bloco}" if atual else bloco[:limite]
    if atual:
        yield atual


def main():
    termo = " ".join(sys.argv[1:]).strip()[:60]
    cfg = m.carregar_json(m.CONFIG, {})
    b.REGIOES["am"] = ("Manaus", cfg.get("olx_caminho_busca", "estado-am/regiao-de-manaus"), "manaus")
    saida = asyncio.run(b.pesquisar(termo, "am", sites=("OLX",)))
    texto = b.formatar("OLX", termo, saida.get("OLX"), "Manaus").replace("\n•", "\n\n•")
    for p in pedacos(texto):
        m.discord(cfg, "sendMessage", {"text": p})


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        t = "\n\n".join(f"• item {i} " + "x" * 300 for i in range(20))
        ps = list(pedacos(t))
        assert all(len(p) <= 1900 for p in ps) and "".join(ps).count("• item") == 20, [len(p) for p in ps]
        print("selftest ok")
    else:
        main()
