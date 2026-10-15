// Edge Function Supabase « scanlame » : réception des prises de la page de capture.
//
// À coller dans le dashboard Supabase : Edge Functions > Deploy a new function > via Editor,
// nom « scanlame », DÉSACTIVER « Enforce JWT verification » (la page s'authentifie par X-Token),
// puis Secrets de la fonction : SCANLAME_TOKEN = le jeton saisi dans la page.
//
// Routes (URL de base : https://<projet>.supabase.co/functions/v1/scanlame) :
//   POST /sign    {series, name, contentType}  -> {url} : URL signée où la page envoie le fichier en PUT
//   POST /finish  {series, ...meta}            -> écrit <series>/serie.json et réveille GitHub (secret GH_TOKEN)
//   GET  /        -> état
import { createClient } from "npm:@supabase/supabase-js@2";

const BUCKET = "series";
const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type, x-token, authorization, apikey",
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...cors, "Content-Type": "application/json" } });

const supabase = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
const safe = (s: string) => (s || "").replace(/[^A-Za-z0-9_.-]/g, "_");

async function ensureBucket() {
  const { data } = await supabase.storage.getBucket(BUCKET);
  if (!data) await supabase.storage.createBucket(BUCKET, { public: false, fileSizeLimit: 52428800 });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  const route = new URL(req.url).pathname.replace(/^.*\/scanlame/, "") || "/";
  if (req.method === "GET") {
    // etat + espace utilise par le bucket (somme des tailles, dossier par dossier)
    let used = 0, series = 0;
    try {
      const { data: dirs } = await supabase.storage.from(BUCKET).list("", { limit: 1000 });
      for (const d of dirs || []) {
        if (d.id) { used += d.metadata?.size || 0; continue; }
        series++;
        for (const sub of ["", "chunks", "photos"]) {
          const { data: files } = await supabase.storage.from(BUCKET).list(sub ? `${d.name}/${sub}` : d.name, { limit: 1000 });
          for (const f of files || []) used += f.metadata?.size || 0;
        }
      }
    } catch (_) { /* bucket absent ou vide */ }
    return json({ ok: true, service: "scanlame", bucket: BUCKET, used_bytes: used, series, limit_bytes: 1073741824 });
  }

  const token = Deno.env.get("SCANLAME_TOKEN") || "";
  if (!token || req.headers.get("x-token") !== token) return json({ error: "jeton invalide" }, 401);

  let body: Record<string, unknown>;
  try { body = await req.json(); } catch { return json({ error: "JSON attendu" }, 400); }
  const series = safe(String(body.series || ""));
  if (!series) return json({ error: "series manquant" }, 400);
  await ensureBucket();

  if (route === "/sign") {
    const name = safe(String(body.name || ""));
    const sub = String(body.kind || "") === "photo" ? "photos" : "chunks";
    const path = `${series}/${sub}/${name}`;
    const { data, error } = await supabase.storage.from(BUCKET).createSignedUploadUrl(path, { upsert: true });
    if (error || !data) return json({ error: error?.message || "signature impossible" }, 500);
    return json({ url: data.signedUrl, path });
  }
  if (route === "/finish") {
    const { error } = await supabase.storage.from(BUCKET)
      .upload(`${series}/serie.json`, new Blob([JSON.stringify(body)], { type: "application/json" }), { upsert: true, contentType: "application/json" });
    if (error) return json({ error: error.message }, 500);
    // wake the GitHub workflow right away (optional: needs the GH_TOKEN secret on this function)
    let dispatched = false;
    const gh = Deno.env.get("GH_TOKEN"), repo = Deno.env.get("GH_REPO") || "ashrin45/1_scanlame";
    if (gh) {
      try {
        const r = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
          method: "POST",
          headers: { Authorization: `Bearer ${gh}`, Accept: "application/vnd.github+json", "Content-Type": "application/json", "User-Agent": "scanlame" },
          body: JSON.stringify({ event_type: "serie", client_payload: { series } }),
        });
        dispatched = r.status === 204;
      } catch (_) { dispatched = false; }
    }
    return json({ ok: true, queued: true, dispatched });
  }
  return json({ error: "route inconnue" }, 404);
});
