# PENDIENTES_DEPLOY — Dorismon v3.9.76

Lista de lo que hay que **configurar y comprobar a mano** antes (y justo después) del deploy. Nada de esto se ha tocado:
no se modificó Render, Vercel ni ninguna base de producción. Ningún valor secreto aparece en este documento.

## 0. Orden recomendado
1. Backup / punto de restauración de la base de producción (PostgreSQL de Render).
2. Configurar las variables de las secciones 1 a 5 (Render y Vercel).
3. Desplegar backend (crea las tablas nuevas) y después frontend.
4. Smoke test controlado (sección 8) **sin clases reales activas**.
5. Solo entonces, retirar los recursos de PREPROD (sección 9).

## 1. Render — variables del backend (comprobar una a una)
| Variable | Valor / criterio | Por qué |
|---|---|---|
| `SECRET_KEY` | Aleatoria, **≥ 32 caracteres**, sin la palabra "cambiar". `generateValue: true` de `render.yaml` ya la genera. | Si falta o es la de ejemplo, **el servicio no arranca** (a propósito). **No rotarla sin avisar**: invalida las sesiones abiertas (tokens firmados con la anterior). |
| `TRUSTED_PROXY_COUNT` | **NO definirla en Render.** Con `RENDER=true` (lo define Render solo) el limitador de login usa exclusivamente `CF-Connecting-IP`. Esta variable queda únicamente para proxies que NO sean de Render y estén administrados explícitamente. | Si `CF-Connecting-IP` falta o no es una IP válida, la identidad de IP se considera no fiable y no se crean buckets por IP (fail-safe contra bloqueos cruzados); el log lo avisa. |
| `CORS_ORIGINS` | Lista **exacta**: `https://dorismon.com,https://www.dorismon.com` (+ el dominio del frontend de Vercel si se usa). Sin comodín, sin `localhost`. | En producción el valor por defecto ya excluye `localhost`; fijarlo explícito evita sorpresas. |
| `ALLOW_VERCEL_PREVIEWS` | **No definirla** (o `false`). | Los previews de Vercel no deben llamar a la API de producción. |
| `ALLOW_DESTRUCTIVE_MAINTENANCE` | **No definirla.** | Sin ella, `POST /admin/maintenance/clean-operational-data` con `dry_run=false` responde 403 en producción. Solo para una limpieza de lanzamiento deliberada: definirla, ejecutar, **y quitarla**. |
| `ENVIRONMENT` | Opcional (`production`). `RENDER=true` ya activa el modo producción y **no se puede anular**. | |
| `DATABASE_URL` | La inyecta Render. | |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Del proyecto LiveKit **de producción**. | Video Dorismon. |
| `RECORDINGS_R2_BUCKET`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY`, `R2_SECRET_KEY` | Bucket **PRIVADO nuevo de producción** y token Object Read & Write limitado a él. **No reutilizar** el bucket ni los tokens de PREPROD. | Grabaciones (Egress). Si falta, el servicio responde "almacenamiento no configurado" y no graba. |
| `RECORDINGS_R2_READ_ACCESS_KEY`, `RECORDINGS_R2_READ_SECRET_KEY` | Token **solo lectura** limitado al mismo bucket. | Reproducción privada (URLs firmadas de 10 min). |
| `RECORDINGS_S3_ENDPOINT`, `RECORDINGS_S3_REGION` | Opcionales (por defecto `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` y `auto`). | |

### 1.1 Cómo comprobar la IP del cliente en Render
Tras el deploy, hacer un login fallido desde fuera y revisar los logs de Render: no debe aparecer el aviso del limitador por IP no fiable.
Si aparece, `CF-Connecting-IP` no está llegando; NO se arregla con `TRUSTED_PROXY_COUNT` en Render: investigar el enrutado antes de seguir.

### 1.2 Limitaciones conocidas (aceptadas)
- **El limitador de login vive en la memoria de UNA instancia.** Con varias instancias o tras un reinicio (el plan gratuito de Render
  duerme la app tras 15 min) los contadores no se comparten ni sobreviven. Para múltiples instancias habría que moverlo a Redis.
- Un ataque distribuido (muchas IPs) contra **una** cuenta no queda limitado por cuenta: se limita por par IP+correo y por IP.

## 2. Credenciales históricas del seed — ACCIÓN MANUAL OBLIGATORIA antes de abrir al público
- **Corrección (hotfix de auditoría externa):** la entrega anterior de este cierre todavía contenía en el código (`seed.py` y
  `app/services/seguridad_arranque.py`) las contraseñas de desarrollo históricas, y el seed las imprimía. **Eso era incorrecto y ya
  está corregido en este paquete**: ninguna contraseña histórica permanece en el backend, el frontend, los tests ni la documentación
  del candidato (comprobado con un escaneo). El historial de Git **sigue conservándolas** (no se reescribió), así que deben
  considerarse **públicas y reutilizables**. Los valores antiguos no se repiten en este documento.
- El seed (`seed.py`) ahora es solo de desarrollo: exige `ALLOW_DEV_SEED=true` y que definas `SEED_ADMIN_PASSWORD`,
  `SEED_TEACHER_PASSWORD` y `SEED_STUDENT_PASSWORD`; **no trae ninguna contraseña**, no imprime ninguna y se niega a correr en producción.
- El backend **ya no puede comprobar** si una cuenta conserva una contraseña histórica (no guarda ninguna). Al arrancar en producción
  solo deja un log **CRÍTICO** si existen cuentas **activas** con los correos del seed (`admin@`, `ana@`, `luis@`, `sara@`,
  `maria.estudiante@`, `carlos.estudiante@`, `juana.estudiante@` de `@dorismon.do`). Esa lista es un aviso, no una prueba de que
  la contraseña siga siendo la antigua.
- **Antes de abrir al público (manual, obligatorio):**
  1. **Verificar en producción** cuáles de esas cuentas existen (consulta de solo lectura o el log del primer arranque).
  2. **Rotar la contraseña o desactivar** cada cuenta real que pueda conservar una contraseña histórica — empezando por la de
     Dirección/administración y cualquier profesor — y cerrar sus sesiones activas.
  3. Eliminar o desactivar las cuentas de demostración que no se usen.
  4. No revelar los valores antiguos en tickets, chats ni informes.
- No se cambió ningún secreto desplegado.

## 3. Base de datos (PostgreSQL de producción)
- `create_all()` **creará 5 tablas nuevas** al primer arranque: `video_access_blocks`, `video_recordings`, `video_recording_audience`,
  `class_session_content`, `class_session_audience` (DDL de referencia verificado en PostgreSQL 18.1 real en la entrega de PREPROD-3).
  No hay migraciones destructivas ni cambios en tablas existentes. **Backup antes de desplegar.**
- Verificado en PostgreSQL 18.1: constraint `uq_video_recording_activa` (una grabación activa por sesión), unicidad de bloqueos y
  audiencias, concurrencia de Start/Stop/Finalizar, y aprobación de pagos concurrente.
- Hallazgo preexistente **sin corregir** (decisión de producto): el borrado de **grupo** con dependencias responde ahora 409 claro;
  no se desvincula nada automáticamente.

## 4. Vercel — frontend
| Variable | Criterio |
|---|---|
| `NEXT_PUBLIC_API_URL` | **Obligatoria** en Production **y** Preview, `https://` (sin ella `next build` falla a propósito; ya no hay URL por defecto). Preview debe apuntar a una API de pruebas, **nunca** a producción. |
| `VERCEL_ENV` | Lo define Vercel. El service worker solo se activa en `production`. |
- Tras el deploy, `GET /version` debe devolver el commit desplegado y la app debe ofrecer "Hay una nueva versión…" a las pestañas abiertas
  (no durante una videoclase).
- Dominio: confirmar que el sitio y la API **no** comparten un origen que permita al service worker cachear la API (el SW solo guarda estáticos del propio dominio).

## 5. LiveKit Cloud (producción)
- Proyecto y claves de producción en Render (sección 1). **Webhook externo** (egress_*): configurar en LiveKit Cloud la URL
  `https://<api>/video/livekit/webhook` firmada con las claves del proyecto. Quedó **sin probar en real** (LiveKit no alcanza localhost):
  `⚠️ WEBHOOK EXTERNO PENDIENTE DE SMOKE DE PRODUCCIÓN`. La reconciliación con `ListEgress` funciona aunque el webhook no llegue.
- Egress responde en `snake_case`; el código ya lo lee (`file_results` o `file`) y la salida MP4 lleva `disableManifest`.

## 6. Qué cambió en seguridad (v3.9.76 P0) — para el auditor
1. Limitador de login: claves por IP+correo y por IP; en Render usa solo `CF-Connecting-IP` (sin `TRUSTED_PROXY_COUNT`); en no-Render, IP del socket o salto de confianza configurado; memoria acotada, limpieza real.
2. `SECRET_KEY`: producción falla cerrado.
3. Seed bloqueado en producción y sin contraseñas (exige `ALLOW_DEV_SEED` + `SEED_*_PASSWORD`); README/tests sin contraseñas históricas;
   aviso crítico de cuentas con correo del seed (ya no verifica contraseñas).
4. Aprobar/rechazar pagos: reclamo atómico del estado (una sola aprobación efectiva); el email se envía **después** del commit
   (best-effort: si falla, la decisión confirmada no se revierte).
5. Borrado operativo (`clean-operational-data`): deshabilitado en producción por defecto (la simulación sigue disponible).
6. CORS: sin `localhost` por defecto en producción; sin comodín; previews cerrados.
7. Frontend: una caída de la API **ya no cierra la sesión** (solo 401/403/404 reales); texto de instalación sin la promesa de "offline".
8. Hotfix de auditoría externa: Next.js 14.2.15 → **15.5.28** (React/ReactDOM 19, tipos 19); FastAPI 0.115.0 → **0.139.2** con
   **Starlette 1.9.0** fijada; python-multipart 0.0.9 → **0.0.32**. `localStorage.api_base` y `window.__API_BASE__` eliminados (el Bearer
   solo viaja a `NEXT_PUBLIC_API_URL`); el borrado de cachés (logout y mensaje `CLEAR_CACHE` del service worker) se limita a `dorismon-*`;
   `.env.example` del frontend apunta a la API local.

## 6.1 Dependencias — qué comprobar en el deploy
- **Python:** el candidato se probó con **Python 3.11.0**; Render usa `runtime.txt` = **3.12.7**. Comprobar que `pip install -r requirements.txt`
  termina bien en Render y que `GET /health` responde antes de dar por bueno el deploy (no hay otro Python 3.12 disponible en la
  máquina de pruebas).
- **Node (Vercel):** Next.js 15.5 exige Node ≥ 18.18; usar el Node 20 o 22 del proyecto en Vercel. Compilado y probado con Node 22.20.
- **Auditoría de dependencias** (`npm audit`, `pip-audit`): sin vulnerabilidades en Next.js (runtime), FastAPI, Starlette ni python-multipart.
  Quedan avisos que **no se actualizaron** por orden expresa de no tocar dependencias no relacionadas; se valoraron como no aplicables al
  uso de Dorismon, pero conviene un ciclo de actualización posterior al deploy:
  - **PyJWT 2.9.0** (varios avisos, 2 de gravedad alta): afectan a `PyJWKClient`/JWK/claves asimétricas/cabecera `crit`/revocación por token
    crudo. Dorismon solo usa `jwt.encode`/`jwt.decode` con **HS256 y `algorithms` explícito** y claves de servidor; no usa JWKS ni revocación
    por token. Recomendado: subir a la versión corregida más reciente y repasar los tests de auth.
  - **Pillow 10.4.0** (varios avisos de decodificación): Dorismon solo decodifica con Pillow el **logo del instituto que sube un administrador**
    (`/catalog/…icon`); no se decodifican archivos de estudiantes (las entregas van directo a Cloudinary). Recomendado: subir Pillow.
  - **npm (12 avisos, 9 altos, 0 críticos):** todos en herramientas de **compilación** (tailwindcss/postcss/browserslist/glob/nanoid), que
    procesan código propio y de confianza y no se ejecutan en producción. Solucionarlos requiere saltos mayores (Tailwind 4 / Next 16): fuera de alcance.
  - `pip` y `setuptools` del entorno de pruebas son herramientas locales, no se despliegan.

## 7. Pendientes de PRODUCTO (no bloquean el deploy)
- Borrar un **grupo/serie** con matrículas, quizzes, tareas, materiales o asignaciones responde 409 (no se desvincula ni borra nada).
- `localStorage.api_base` permite cambiar la base de la API desde el navegador en `components/ui.tsx` (revisar si es necesario).
- El service worker no ofrece modo offline: si se quiere, es una funcionalidad nueva (no se promete en la interfaz).

## 8. Smoke test controlado de PRODUCCIÓN — `PENDIENTE — SMOKE CONTROLADO DE PRODUCCIÓN`
Requisitos obligatorios (ZERO DATA LOSS): backup/punto de restauración antes; **sin clases reales activas** (comprobar con ListRooms);
usuarios sintéticos de prueba; una `ClassSession` sintética claramente identificada; **ningún estudiante real**; no tocar notas, pagos,
matrícula, progreso ni expedientes reales; no reutilizar una clase real; no borrar ni modificar Rooms ajenas; limpiar **solo** lo
sintético; rollback preparado.
A verificar con hardware real (no verificado en preproducción): cámara, micrófono, altavoz/audio (una sola vez, sin eco ni voz doble),
screen share físico, permisos reales del dispositivo, corte/cambio real de Wi-Fi (~5 s) y recuperación de pistas, comprobación
audiovisual humana, `RoomAudioRenderer` sin duplicar (inspección directa), instalación PWA y notificaciones push en un teléfono real,
webhook externo de LiveKit.

## 9. Recursos de PREPROD a retirar DESPUÉS del smoke (no se tocaron)
- Bucket R2 `dorismon-preprod-recordings` y sus 2 tokens (caducan solos a los 7 días de su creación).
- Sesión de `wrangler` (OAuth de Cloudflare) y autorización del CLI de LiveKit (`lk cloud auth --revoke`).
- Carpetas `pp2` y `pp3` del área de trabajo, el data directory del PostgreSQL temporal y los respaldos de `dorismon-respaldos`.

## 10. Preparación de producción (estado a 2026-10-21) — sin aplicar todavía
- **Backup verificado** de `dorismon-db` antes de cualquier cambio (fuera de los repos): dump `-Fc` + esquema, SHA-256, `pg_restore --list` y
  restauración en un PostgreSQL 18 local aislado con todos los recuentos coincidentes (56 tablas). Rollback documentado en la carpeta del backup.
- **Producción actual:** backend `e0ac188` (v3.9.70), frontend `152858a`. **El merge a `main` dispara el deploy de Render (auto-deploy ON) y el de
  Production en Vercel (Git conectado).** La rama `candidate/v3.9.76` no despliega Production en ninguno de los dos.
- **Arranque de v3.9.76 sobre la base real:** solo `create_all` de 5 tablas vacías; sin DROP/DELETE nuevos, sin seed, sin reescritura de datos.
- **R2 producción:** bucket privado `dorismon-recordings-prod` (sin `r2.dev`, sin dominio público), distinto de PREPROD. Dos tokens limitados a ese bucket:
  Object Read & Write (`R2_ACCESS_KEY`/`R2_SECRET_KEY`) y Object Read-only (`RECORDINGS_R2_READ_ACCESS_KEY`/`RECORDINGS_R2_READ_SECRET_KEY`).
  Variables a poner en Render DENTRO de la ventana de deploy (cambiar el Environment puede reiniciar el servicio): `R2_ACCOUNT_ID`,
  `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `RECORDINGS_R2_BUCKET=dorismon-recordings-prod`, `RECORDINGS_R2_READ_ACCESS_KEY`, `RECORDINGS_R2_READ_SECRET_KEY`.
- **Vercel — Node:** el proyecto usa 24.x y el candidato se probó con Node 22.20. `package.json` fija `engines.node = 22.x` para que el
  deploy use Node 22 sin tocar Settings. Comprobar la versión en el log del primer build.
- **Vercel — Preview:** `NEXT_PUBLIC_API_URL` es una sola variable compartida por Production y Preview, así que Preview apunta a la API REAL.
  Hasta tener un backend de pruebas: **no usar deployments Preview para pruebas con escritura** y no compartir sus URLs. Cuando exista staging,
  separar la variable (Production → API real, Preview → API de pruebas).
- **Cuentas históricas del seed:** se revisaron en producción con consultas de solo lectura. La rotación o desactivación de las que sigan activas requiere autorización explícita y se hace DESPUÉS del backup. Los repositorios son públicos y el historial conserva las contraseñas antiguas: considerarlas comprometidas. (El detalle por cuenta no se publica en este archivo.)
- **Comprobar que no hay clases activas justo antes del merge:** `check_clases_activas.py` (solo lectura: clases programadas en curso ±15 min,
  clases en las próximas 3 h y presencia de video en los últimos 5 min) + LiveKit Cloud (proyecto de producción) con ListRooms = 0 y sin egress activos.
- **Webhook de LiveKit (crear SOLO después de que v3.9.76 esté live):** URL `https://dorismon-api.onrender.com/video/livekit/webhook`, eventos de
  egress (`egress_started`, `egress_updated`, `egress_ended`), firmado con la API key/secret del proyecto de producción. La reconciliación con
  `ListEgress` funciona aunque el webhook no llegue. `⚠️ WEBHOOK EXTERNO PENDIENTE DE SMOKE DE PRODUCCIÓN`.
