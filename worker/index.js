// Buscapre · bot do Discord (Cloudflare Worker, grátis, 24h).
// /busca <termo> -> dispara o workflow busca.yml no GitHub; o resultado chega no #preços em ~4 min.
// Segredos (wrangler secret put): DISCORD_PUBLIC_KEY, GITHUB_TOKEN (fine-grained, só Actions: write no repo buscapre).
const REPO = "leonzordhue/buscapre";

const hex = (h) => new Uint8Array(h.match(/../g).map((b) => parseInt(b, 16)));

export async function assinaturaOk(req, corpo, chavePublica) {
  const sig = req.headers.get("x-signature-ed25519"), ts = req.headers.get("x-signature-timestamp");
  if (!sig || !ts) return false;
  const chave = await crypto.subtle.importKey("raw", hex(chavePublica), { name: "Ed25519" }, false, ["verify"]);
  return crypto.subtle.verify("Ed25519", chave, hex(sig), new TextEncoder().encode(ts + corpo));
}

const json = (o) => new Response(JSON.stringify(o), { headers: { "Content-Type": "application/json" } });

export default {
  async fetch(req, env, ctx) {
    if (req.method !== "POST") return new Response("Buscapre ok");
    // /aviso: o Apps Script do Gmail (Leon Games) manda aqui; o Discord bloqueia o Google (erro 40333), o Worker repassa.
    if (new URL(req.url).pathname === "/aviso") {
      const { segredo, texto } = await req.json().catch(() => ({}));
      if (!env.AVISO_SEGREDO || segredo !== env.AVISO_SEGREDO) return new Response("nao", { status: 401 });
      const r = await fetch(env.DISCORD_WEBHOOK_PROPOSTAS + "?wait=true", {
        method: "POST", headers: { "Content-Type": "application/json", "User-Agent": "Buscapre" },
        body: JSON.stringify({ username: "Leon Games", content: String(texto).slice(0, 2000) }),
      });
      return new Response(String(r.status), { status: r.ok ? 200 : 502 });
    }
    const corpo = await req.text();
    if (!(await assinaturaOk(req, corpo, env.DISCORD_PUBLIC_KEY))) return new Response("assinatura inválida", { status: 401 });
    const i = JSON.parse(corpo);
    if (i.type === 1) return json({ type: 1 }); // PING do Discord ao salvar a URL
    if (i.type === 2 && i.data.name === "busca") {
      const termo = (i.data.options?.find((o) => o.name === "termo")?.value || "").trim().slice(0, 60);
      ctx.waitUntil(fetch(`https://api.github.com/repos/${REPO}/actions/workflows/busca.yml/dispatches`, {
        method: "POST",
        headers: { Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json", "User-Agent": "buscapre-worker" },
        body: JSON.stringify({ ref: "main", inputs: { termo } }),
      }).then(async (r) => { if (r.status !== 204) console.log("GitHub", r.status, await r.text()); }));
      return json({ type: 4, data: { content: `🔎 Buscando **${termo}** na OLX de Manaus... os 10 mais recentes chegam aqui em ~4 min.` } });
    }
    return json({ type: 4, data: { content: "Comando desconhecido.", flags: 64 } });
  },
};
