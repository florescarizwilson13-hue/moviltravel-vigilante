# Vigilante MovilTravel

Revisa cada 10 minutos, desde GitHub Actions (fuera de Render, Vercel y Supabase):

| Revisión | Qué mira | Falla si |
|---|---|---|
| Backend y base | `GET /health/db` del backend en Render | no responde, o la base no contesta (503) |
| App web | la página en Vercel y su archivo principal | no carga o falta la app |

## Reglas de aviso

- Avisa con **2 fallas seguidas** (correo + WhatsApp).
- Mientras siga caído, repite **como máximo una vez por hora**.
- Cuando vuelve, avisa **VOLVIÓ** con cuánto duró la caída.
- Los **lunes desde las 09:00** (hora de Chile) manda un correo de resumen. Si no llega, el que está fallando es el vigilante: revisar la pestaña **Actions**.

## Secretos (Settings → Secrets and variables → Actions)

`SMTP_USUARIO`, `SMTP_CLAVE`, `CORREO_DESTINO`, `CALLMEBOT_TELEFONO`, `CALLMEBOT_APIKEY`. Ningún dato de acceso va en el código.

## Probar a mano

Pestaña **Actions → Vigilante MovilTravel → Run workflow**:

- `modo = prueba_canales`: manda un mensaje de prueba por los dos canales.
- `url_backend` / `url_web` con una dirección que falle: simula una caída (correr 2 veces para que avise). Después correrlo vacío para que avise que volvió.
- `modo = resumen`: fuerza el correo semanal.

`estado.json` guarda el estado entre revisiones; lo actualiza el propio vigilante.
