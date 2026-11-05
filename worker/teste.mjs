// node worker/teste.mjs : assina com uma chave de teste e confere PING, /busca e assinatura falsa.
import w from "./index.js";
const { publicKey, privateKey } = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const pk = Buffer.from(await crypto.subtle.exportKey("raw", publicKey)).toString("hex");
const chamadas = []; globalThis.fetch = async (u, o) => { chamadas.push([u, JSON.parse(o.body)]); return new Response(null, { status: 204 }); };
const env = { DISCORD_PUBLIC_KEY: pk, GITHUB_TOKEN: "t" }, ctx = { waitUntil: (p) => p };
async function req(obj, falsa) {
  const corpo = JSON.stringify(obj), ts = "1";
  const sig = Buffer.from(await crypto.subtle.sign("Ed25519", privateKey, new TextEncoder().encode(ts + (falsa ? "x" : "") + corpo))).toString("hex");
  return w.fetch(new Request("https://x/", { method: "POST", body: corpo, headers: { "x-signature-ed25519": sig, "x-signature-timestamp": ts } }), env, ctx);
}
const ok = (c, m) => { if (!c) { console.error("FALHOU:", m); process.exit(1); } };
ok((await (await req({ type: 1 })).json()).type === 1, "ping");
ok((await req({ type: 1 }, true)).status === 401, "assinatura falsa");
const r = await (await req({ type: 2, data: { name: "busca", options: [{ name: "termo", value: " ps2 " }] } })).json();
ok(r.type === 4 && r.data.content.includes("**ps2**"), "resposta");
ok(chamadas[0][0].includes("leonzordhue/buscapre/actions/workflows/busca.yml/dispatches") && chamadas[0][1].inputs.termo === "ps2", "dispatch");
const env2 = { ...env, AVISO_SEGREDO: "s3", DISCORD_WEBHOOK_PROPOSTAS: "https://discord/wh" };
const av = (o) => w.fetch(new Request("https://x/aviso", { method: "POST", body: JSON.stringify(o) }), env2, ctx);
ok((await av({ segredo: "errado", texto: "oi" })).status === 401, "aviso sem segredo");
chamadas.length = 0; globalThis.fetch = async (u, o) => { chamadas.push([u, JSON.parse(o.body)]); return new Response("{}", { status: 200 }); };
ok((await av({ segredo: "s3", texto: "oi" })).status === 200 && chamadas[0][0] === "https://discord/wh?wait=true" && chamadas[0][1].content === "oi", "aviso");
const { quais } = await import("./index.js");
ok(quais(0).length === 2 && quais(5).length === 1 && quais(15).length === 2 && quais(50).length === 1, "ritmo");
chamadas.length = 0; globalThis.fetch = async (u, o) => { chamadas.push([u, JSON.parse(o.body)]); return new Response(null, { status: 204 }); };
await w.scheduled({ scheduledTime: Date.UTC(2026, 10, 5, 12, 30) }, env, { waitUntil: (p) => p });
await new Promise((r) => setTimeout(r, 10));
ok(chamadas.length === 2 && chamadas.every(([u, b]) => b.ref === "main" && !b.inputs), "cron dispara new3ds + buscapre");
console.log("teste-worker ok");
