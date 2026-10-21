// PREPROD-4 — Arnés del service worker: carga public/sw.js REAL en un `vm` de Node con caches, clients,
// fetch y notificaciones simulados y devuelve un JSON con lo que hizo. Uso: node preprod4_sw_harness.js <ruta sw.js>
// No usa red ni navegador: es determinista. Lo ejecuta tests/test_preprod4_pwa.py.
const fs = require("fs");
const vm = require("vm");

const SW = fs.readFileSync(process.argv[2], "utf8");
const ORIGEN = "https://dorismon.test";

function crearSW(url, cachesIniciales = {}) {
  const manejadores = {};
  const almacen = new Map(Object.entries(cachesIniciales).map(([n, urls]) => [n, new Map(urls.map((u) => [u, { guardado: true, url: u }]))]));
  const estado = { skipWaiting: 0, claim: 0, red: [], notificaciones: [], navegados: [], abiertos: [], enfocados: 0, clientes: [] };
  const caches = {
    keys: async () => [...almacen.keys()],
    delete: async (n) => almacen.delete(n),
    open: async (n) => {
      if (!almacen.has(n)) almacen.set(n, new Map());
      const c = almacen.get(n);
      return { match: async (req) => c.get(req.url), put: async (req, resp) => { c.set(req.url, resp); } };
    },
  };
  const clients = {
    claim: async () => { estado.claim++; },
    matchAll: async () => estado.clientes,
    openWindow: async (u) => { estado.abiertos.push(u); },
  };
  const self_ = {
    location: { href: url, origin: ORIGEN },
    addEventListener: (t, f) => { manejadores[t] = f; },
    skipWaiting: () => { estado.skipWaiting++; },
    registration: { showNotification: async (t, o) => { estado.notificaciones.push({ titulo: t, opciones: o }); } },
    clients, caches,
  };
  const contexto = vm.createContext({
    self: self_, caches, clients, URL, Promise, console,
    fetch: async (req) => {
      estado.red.push(req.url);
      const r = (globalThis.__respuestas || {})[req.url] || { status: 200, type: "basic" };
      return { ...r, url: req.url, clone() { return { ...this }; } };
    },
  });
  vm.runInContext(SW, contexto);
  return { m: manejadores, almacen, estado, self: self_ };
}

async function despachar(sw, tipo, evento) {
  const pendientes = [];
  const e = Object.assign({ waitUntil: (p) => { pendientes.push(Promise.resolve(p)); }, respondWith: (p) => { e._respuesta = Promise.resolve(p); e.respondido = true; } }, evento);
  e.respondido = false;
  sw.m[tipo] && sw.m[tipo](e);
  await Promise.all(pendientes);
  return e;
}

(async () => {
  const out = {};
  // ── Versión y nombre de la caché
  let sw = crearSW(`${ORIGEN}/sw.js?v=abc123`);
  await despachar(sw, "install", {});
  out.install_salta_espera = sw.estado.skipWaiting === 1;
  out.install_sin_precache = sw.almacen.size === 0;
  const req = (ruta, metodo = "GET", origen = ORIGEN) => ({ url: origen + ruta, method: metodo });

  // ── Qué se intercepta y qué pasa directo a la red (sin guardar)
  const casos = {
    "/_next/static/chunks/app.js": true, "/icons/icon-192.png": true, "/logo-dorismon.svg": true, "/favicon.ico": true, "/screenshots/a.webp": true,
    "/dashboard": false, "/dashboard/student/calendar": false, "/login": false, "/register": false, "/checkout": false, "/checkout/ok": false,
    "/verify-email": false, "/reset-password": false, "/forgot-password": false,
    "/version": false, "/api/x": false, "/": false, "/manifest.webmanifest": false, "/sw.js": false, "/clase/123": false,
    // imágenes bajo rutas PRIVADAS: casan con el patrón de imágenes y SOLO la exclusión explícita las deja pasar a la red.
    // (/login y /register se excluyen por igualdad exacta —son páginas públicas—, así que una imagen estática
    //  bajo ellas se guarda como cualquier otra imagen pública: no es un dato de usuario y no se prueba aquí.)
    "/dashboard/student/avatar.png": false, "/checkout/qr.png": false, "/verify-email/a.svg": false,
    "/reset-password/a.gif": false, "/forgot-password/a.ico": false, "/api/foto.png": false,
  };
  out.intercepta = {};
  for (const [ruta, debe] of Object.entries(casos)) {
    const e = await despachar(sw, "fetch", { request: req(ruta) });
    out.intercepta[ruta] = { esperado: debe, real: e.respondido };
  }
  const ajenos = ["https://api-dorismon.onrender.invalid/auth/login", "https://res.cloudinary.com/x/foto.png", "https://dorismon-4fc3t25f.livekit.cloud/rtc",
                  "https://fonts.googleapis.com/css", "https://cdn.otro.com/_next/static/x.js"];
  out.ajenos = {};
  for (const u of ajenos) {
    const e = await despachar(sw, "fetch", { request: { url: u, method: "GET" } });
    out.ajenos[u] = e.respondido;
  }
  out.metodos = {};
  for (const m of ["POST", "PUT", "PATCH", "DELETE"]) {
    const e = await despachar(sw, "fetch", { request: req("/_next/static/chunks/app.js", m) });
    out.metodos[m] = e.respondido;
  }

  // ── Qué queda guardado y dónde
  sw = crearSW(`${ORIGEN}/sw.js?v=abc123`);
  const rutaStatic = "/_next/static/chunks/hash123.js";
  let e = await despachar(sw, "fetch", { request: req(rutaStatic) });
  await e._respuesta;
  out.static_primera = { red: sw.estado.red.length, guardado: sw.almacen.get("dorismon-abc123")?.has(ORIGEN + rutaStatic) };
  e = await despachar(sw, "fetch", { request: req(rutaStatic) });
  await e._respuesta;
  out.static_segunda_desde_cache = sw.estado.red.length === 1;
  globalThis.__respuestas = { [ORIGEN + "/icons/x.png"]: { status: 404, type: "basic" }, [ORIGEN + "/icons/op.png"]: { status: 200, type: "opaque" } };
  for (const r of ["/icons/x.png", "/icons/op.png"]) { const ev = await despachar(sw, "fetch", { request: req(r) }); await ev._respuesta; }
  out.no_guarda_404_ni_opaco = !sw.almacen.get("dorismon-abc123").has(ORIGEN + "/icons/x.png") && !sw.almacen.get("dorismon-abc123").has(ORIGEN + "/icons/op.png");
  globalThis.__respuestas = {};
  // sin hash: sirve lo guardado y renueva en segundo plano
  const logo = "/logo-dorismon.svg";
  sw.almacen.get("dorismon-abc123").set(ORIGEN + logo, { viejo: true, url: ORIGEN + logo });
  const antes = sw.estado.red.length;
  e = await despachar(sw, "fetch", { request: req(logo) });
  const servido = await e._respuesta;
  out.sin_hash_sirve_guardado_y_renueva = servido.viejo === true && sw.estado.red.length === antes + 1;
  const ningunaDeLaApi = [...sw.almacen.values()].every((c) => [...c.keys()].every((k) => k.startsWith(ORIGEN + "/_next/static/") || k.startsWith(ORIGEN + "/icons/") || /\.(svg|png|jpe?g|webp|gif|ico)$/i.test(k)));
  out.solo_estaticos_guardados = ningunaDeLaApi;

  // ── Limpieza de versiones anteriores al activar (no toca cachés ajenas)
  sw = crearSW(`${ORIGEN}/sw.js?v=NUEVA`, { "dorismon-v2.8-static": ["a"], "dorismon-vieja": ["b"], "dorismon-NUEVA": ["c"], "otra-app-cache": ["d"], "workbox-precache": ["e"] });
  await despachar(sw, "activate", {});
  out.activate = { quedan: [...sw.almacen.keys()].sort(), claim: sw.estado.claim };
  sw = crearSW(`${ORIGEN}/sw.js`, { "dorismon-x": ["a"] });
  await despachar(sw, "activate", {});
  out.sin_version = { quedan: [...sw.almacen.keys()] };
  // ── Mensajes
  sw = crearSW(`${ORIGEN}/sw.js?v=m`, { "dorismon-m": ["a"], "otra": ["b"] });
  await despachar(sw, "message", { data: { type: "SKIP_WAITING" } });
  out.message_skip = sw.estado.skipWaiting === 1;
  await despachar(sw, "message", { data: { type: "CLEAR_CACHE" } });
  await new Promise((r) => setTimeout(r, 20));
  out.message_clear = [...sw.almacen.keys()];
  await despachar(sw, "message", { data: null });
  await despachar(sw, "message", { data: { type: "otro" } });
  out.message_ignora_basura = true;

  // ── Push: notificación con título, cuerpo, icono, etiqueta y destino
  sw = crearSW(`${ORIGEN}/sw.js?v=p`);
  const push = async (datos) => { await despachar(sw, "push", { data: datos }); return sw.estado.notificaciones.at(-1); };
  let n = await push({ json: () => ({ title: "Clase en 10 min", body: "Inglés B1", url: "/clase/42", tag: "clase-42" }), text: () => "" });
  out.push_json = { titulo: n.titulo, cuerpo: n.opciones.body, icono: n.opciones.icon, badge: n.opciones.badge, tag: n.opciones.tag, url: n.opciones.data.url, renotify: n.opciones.renotify };
  n = await push({ json: () => { throw new Error("no es json"); }, text: () => "texto plano" });
  out.push_texto_plano = { titulo: n.titulo, cuerpo: n.opciones.body, url: n.opciones.data.url };
  n = await push(null);
  out.push_vacio = { titulo: n.titulo, url: n.opciones.data.url, tag: n.opciones.tag };
  n = await push({ json: () => ({}), text: () => "" });
  out.push_objeto_vacio = { titulo: n.titulo, url: n.opciones.data.url };

  // ── Clic en la notificación: enfoca la ventana abierta (sin abrir otra) o abre una
  sw = crearSW(`${ORIGEN}/sw.js?v=c`);
  let cerrada = 0;
  const notificacion = (url) => ({ close: () => { cerrada++; }, data: url === undefined ? undefined : { url } });
  sw.estado.clientes = [{ focus: async () => { sw.estado.enfocados++; return "ok"; }, navigate: async (u) => { sw.estado.navegados.push(u); } }];
  await despachar(sw, "notificationclick", { notification: notificacion("/clase/42") });
  out.click_con_ventana = { cerrada, navegados: sw.estado.navegados, enfocados: sw.estado.enfocados, abiertos: sw.estado.abiertos.length };
  sw.estado.clientes = [];
  await despachar(sw, "notificationclick", { notification: notificacion("/dashboard/student") });
  out.click_sin_ventana = { abiertos: sw.estado.abiertos.slice() };
  await despachar(sw, "notificationclick", { notification: notificacion(undefined) });
  out.click_sin_datos = { abiertos: sw.estado.abiertos.slice(-1) };

  // ── El SW nunca recarga ni fuerza controllerchange
  // (solo el CÓDIGO: los comentarios explican justamente que no existe "controllerchange -> reload")
  const codigo = SW.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  out.nunca_recarga = !/controllerchange|location\.reload|\.reload\(/.test(codigo);
  console.log("JSON" + JSON.stringify(out));
})().catch((err) => { console.log("ERROR " + (err && err.stack || err)); process.exit(2); });
