"""Vigilante externo de MovilTravel.

Corre en GitHub Actions cada 10 minutos, fuera de Render, Vercel y Supabase:
si alguno se cae, el aviso igual sale.

Revisa:
  backend  /health/db del backend en Render (que a su vez lee la base Supabase).
  web      que la app web en Vercel cargue (pagina + archivo principal).

Reglas de aviso (por cada revision):
  - Avisa con 2 fallas seguidas.
  - Mientras siga caido, repite como maximo una vez por hora.
  - Cuando vuelve, avisa "VOLVIO" con cuanto duro la caida.
  - Los lunes desde las 09:00 (hora de Chile) manda un correo "sigo vigilando".

Canales: correo (SMTP) y WhatsApp (CallMeBot). Datos de acceso SOLO por
variables de entorno (secretos de GitHub), nunca en este repo.

Solo usa la biblioteca estandar de Python.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

CHILE = ZoneInfo("America/Santiago")
ARCHIVO_ESTADO = Path(__file__).with_name("estado.json")

URL_BACKEND = "https://moviltravel-api-staging.onrender.com/health/db"
URL_WEB = "https://web-umber-phi.vercel.app/"

FALLAS_PARA_AVISAR = 2
REPETIR_AVISO_CADA = timedelta(hours=1)
HORA_RESUMEN_LUNES = 9
MAX_HISTORIAL = 50
# El backend en Render puede tardar en despertar: se le da tiempo.
TIMEOUT_BACKEND = 60
TIMEOUT_WEB = 30
TAMANO_MINIMO_APP = 1_000_000  # main.dart.js pesa ~3,9 MB

NOMBRES = {
    "backend": "Backend y base de datos (Render + Supabase)",
    "web": "App web (Vercel)",
}
QUE_REVISAR = {
    "backend": (
        "1) supabase.com: que el proyecto moviltravel-mvp no este pausado (facturacion).\n"
        "2) dashboard.render.com: el servicio moviltravel-api-staging y sus Logs."
    ),
    "web": "vercel.com: el proyecto web y su ultimo despliegue.",
}


@dataclass
class Resultado:
    ok: bool
    detalle: str


# --- revisiones -------------------------------------------------------------


def _pedir(url: str, timeout: int, metodo: str = "GET") -> tuple[int, dict, bytes]:
    pedido = urllib.request.Request(
        url, method=metodo, headers={"User-Agent": "moviltravel-vigilante/1.0"}
    )
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as resp:
            cuerpo = resp.read(200_000) if metodo == "GET" else b""
            return resp.status, dict(resp.headers), cuerpo
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read(5_000)


def revisar_backend(url: str) -> Resultado:
    try:
        codigo, _, cuerpo = _pedir(url, TIMEOUT_BACKEND)
    except Exception as exc:  # red, DNS, tiempo agotado
        return Resultado(False, f"sin respuesta ({type(exc).__name__}: {exc})")
    try:
        datos = json.loads(cuerpo or b"{}")
    except ValueError:
        datos = {}
    if codigo == 200 and datos.get("status") == "ok" and datos.get("db") == "ok":
        return Resultado(True, "ok")
    motivo = datos.get("db") or datos.get("detail") or cuerpo[:120].decode("utf-8", "replace")
    return Resultado(False, f"HTTP {codigo}: {motivo}")


def revisar_web(url: str) -> Resultado:
    try:
        codigo, _, cuerpo = _pedir(url, TIMEOUT_WEB)
    except Exception as exc:
        return Resultado(False, f"sin respuesta ({type(exc).__name__}: {exc})")
    if codigo != 200:
        return Resultado(False, f"la pagina responde HTTP {codigo}")
    if b"flutter_bootstrap.js" not in cuerpo:
        return Resultado(False, "la pagina carga pero no trae la app (falta flutter_bootstrap.js)")
    # Solo la cabecera del archivo principal: no se descargan 3,9 MB cada vez.
    principal = urllib.parse.urljoin(url, "main.dart.js")
    try:
        codigo, cabeceras, _ = _pedir(principal, TIMEOUT_WEB, metodo="HEAD")
    except Exception as exc:
        return Resultado(False, f"main.dart.js sin respuesta ({type(exc).__name__})")
    tamano = int(cabeceras.get("Content-Length") or cabeceras.get("content-length") or 0)
    if codigo != 200 or tamano < TAMANO_MINIMO_APP:
        return Resultado(False, f"main.dart.js responde HTTP {codigo} ({tamano} bytes)")
    return Resultado(True, "ok")


# --- avisos -----------------------------------------------------------------


def enviar_correo(asunto: str, texto: str) -> None:
    usuario = os.environ["SMTP_USUARIO"]
    mensaje = EmailMessage()
    mensaje["From"] = f"Vigilante MovilTravel <{usuario}>"
    mensaje["To"] = os.environ["CORREO_DESTINO"]
    mensaje["Subject"] = asunto
    mensaje.set_content(texto)
    host = os.environ.get("SMTP_HOST", "mail.moviltravel.cl")
    puerto = int(os.environ.get("SMTP_PUERTO", "465"))
    with smtplib.SMTP_SSL(host, puerto, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(usuario, os.environ["SMTP_CLAVE"])
        smtp.send_message(mensaje)


def enviar_whatsapp(texto: str) -> None:
    consulta = urllib.parse.urlencode(
        {
            "phone": os.environ["CALLMEBOT_TELEFONO"],
            "text": texto,
            "apikey": os.environ["CALLMEBOT_APIKEY"],
        }
    )
    codigo, _, cuerpo = _pedir(f"https://api.callmebot.com/whatsapp.php?{consulta}", 30)
    respuesta = cuerpo.decode("utf-8", "replace").lower()
    if codigo != 200 or "error" in respuesta or "apikey is invalid" in respuesta:
        raise RuntimeError(f"CallMeBot respondio HTTP {codigo}: {respuesta[:200]}")


def avisar(asunto: str, texto: str, whatsapp: str) -> list[str]:
    """Manda por los dos canales. Devuelve los canales que funcionaron."""
    enviados = []
    for canal, envio in (
        ("correo", lambda: enviar_correo(asunto, texto)),
        ("whatsapp", lambda: enviar_whatsapp(whatsapp)),
    ):
        try:
            envio()
            enviados.append(canal)
            print(f"Aviso enviado por {canal}.")
        except Exception as exc:
            print(f"ERROR enviando por {canal}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return enviados


# --- estado y reglas --------------------------------------------------------


def _hora(iso: str | None) -> str:
    if not iso:
        return "?"
    return datetime.fromisoformat(iso).astimezone(CHILE).strftime("%d-%m %H:%M")


def _duracion(desde: str, hasta: datetime) -> str:
    minutos = int((hasta - datetime.fromisoformat(desde)).total_seconds() // 60)
    horas, minutos = divmod(max(minutos, 0), 60)
    return f"{horas} h {minutos} min" if horas else f"{minutos} min"


def cargar_estado() -> dict:
    if ARCHIVO_ESTADO.exists():
        return json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8"))
    return {}


def procesar(estado: dict, resultados: dict[str, Resultado], ahora: datetime) -> list[dict]:
    """Aplica las reglas y devuelve los avisos a mandar (sin mandarlos)."""
    chequeos = estado.setdefault("chequeos", {})
    avisos = []
    for clave, res in resultados.items():
        c = chequeos.setdefault(clave, {"fallas_seguidas": 0})
        if res.ok:
            if c.get("avisado"):
                avisos.append(
                    {
                        "tipo": "volvio",
                        "clave": clave,
                        "desde": c["caido_desde"],
                        "duracion": _duracion(c["caido_desde"], ahora),
                    }
                )
            if c.get("fallas_seguidas"):
                estado.setdefault("historial", []).append(
                    {
                        "clave": clave,
                        "caido_desde": c.get("caido_desde"),
                        "volvio": ahora.isoformat(),
                        "avisado": bool(c.get("avisado")),
                    }
                )
            chequeos[clave] = {"fallas_seguidas": 0}
            continue
        c["fallas_seguidas"] = c.get("fallas_seguidas", 0) + 1
        c.setdefault("caido_desde", ahora.isoformat())
        c["ultimo_error"] = res.detalle
        if c["fallas_seguidas"] < FALLAS_PARA_AVISAR:
            continue
        ultimo = c.get("ultimo_aviso")
        if ultimo and ahora - datetime.fromisoformat(ultimo) < REPETIR_AVISO_CADA:
            continue
        avisos.append(
            {
                "tipo": "caido",
                "clave": clave,
                "desde": c["caido_desde"],
                "fallas": c["fallas_seguidas"],
                "error": res.detalle,
                "repeticion": bool(c.get("avisado")),
            }
        )
    estado["historial"] = estado.get("historial", [])[-MAX_HISTORIAL:]
    return avisos


def redactar(avisos: list[dict], ahora: datetime) -> tuple[str, str, str]:
    caidos = [a for a in avisos if a["tipo"] == "caido"]
    volvieron = [a for a in avisos if a["tipo"] == "volvio"]
    partes, cortos = [], []
    for a in caidos:
        nombre = NOMBRES[a["clave"]]
        partes.append(
            f"CAIDO: {nombre}\n"
            f"Falla desde: {_hora(a['desde'])} ({a['fallas']} revisiones seguidas"
            f"{', sigue caido' if a['repeticion'] else ''})\n"
            f"Error: {a['error']}\n"
            f"Que revisar:\n{QUE_REVISAR[a['clave']]}\n"
            "Si sigue caido, te vuelvo a avisar en 1 hora."
        )
        cortos.append(f"*CAIDO* {nombre} desde {_hora(a['desde'])}. Error: {a['error']}")
    for a in volvieron:
        nombre = NOMBRES[a["clave"]]
        partes.append(
            f"VOLVIO: {nombre}\n"
            f"Estuvo caido {a['duracion']} (desde {_hora(a['desde'])} hasta {_hora(ahora.isoformat())})."
        )
        cortos.append(f"*VOLVIO* {nombre}. Estuvo caido {a['duracion']}.")
    if caidos:
        asunto = "[CAIDO] MovilTravel: " + " y ".join(NOMBRES[a["clave"]] for a in caidos)
    else:
        asunto = "[VOLVIO] MovilTravel: " + " y ".join(NOMBRES[a["clave"]] for a in volvieron)
    pie = f"\n\nRevision: {ahora.astimezone(CHILE):%d-%m-%Y %H:%M} (hora de Chile). Vigilante MovilTravel."
    texto = "\n\n".join(partes) + pie
    whatsapp = "MovilTravel vigilante\n" + "\n".join(cortos)
    return asunto, texto, whatsapp


def toca_resumen(estado: dict, ahora: datetime) -> bool:
    local = ahora.astimezone(CHILE)
    if local.weekday() != 0 or local.hour < HORA_RESUMEN_LUNES:
        return False
    return estado.get("ultimo_resumen") != local.date().isoformat()


def redactar_resumen(estado: dict, resultados: dict[str, Resultado], ahora: datetime) -> tuple[str, str]:
    hace_una_semana = ahora - timedelta(days=7)
    caidas = [
        h for h in estado.get("historial", []) if datetime.fromisoformat(h["volvio"]) >= hace_una_semana
    ]
    lineas = [
        f"- {NOMBRES[k]}: {'funciona' if r.ok else 'FALLANDO: ' + r.detalle}" for k, r in resultados.items()
    ]
    detalle = "\n".join(
        f"- {NOMBRES[h['clave']]}: {_hora(h['caido_desde'])} a {_hora(h['volvio'])}"
        f"{'' if h['avisado'] else ' (falla aislada, sin aviso)'}"
        for h in caidas
    )
    texto = (
        "El vigilante de MovilTravel sigue funcionando (revisa cada 10 minutos).\n\n"
        "Estado ahora:\n" + "\n".join(lineas) + "\n\n"
        f"Caidas en los ultimos 7 dias: {len(caidas)}\n" + (detalle or "- ninguna") + "\n\n"
        "Si un lunes no te llega este correo, el que se cayo es el vigilante: "
        "revisa la pestania Actions del repo moviltravel-vigilante."
    )
    return f"[OK] Vigilante MovilTravel: resumen semanal {ahora.astimezone(CHILE):%d-%m}", texto


# --- principal --------------------------------------------------------------


def main() -> int:
    modo = os.environ.get("MODO", "normal")
    ahora = datetime.now(timezone.utc)
    if modo == "prueba_canales":
        enviados = avisar(
            "[PRUEBA] Vigilante MovilTravel: prueba de canales",
            "Mensaje de prueba del vigilante. Si te llega, el correo funciona.",
            "MovilTravel vigilante: mensaje de prueba. Si te llega, WhatsApp funciona.",
        )
        return 0 if len(enviados) == 2 else 1

    url_backend = os.environ.get("URL_BACKEND") or URL_BACKEND
    url_web = os.environ.get("URL_WEB") or URL_WEB
    resultados = {"backend": revisar_backend(url_backend), "web": revisar_web(url_web)}
    for clave, res in resultados.items():
        print(f"{clave}: {'OK' if res.ok else 'FALLA'} - {res.detalle}")

    estado = cargar_estado()
    avisos = procesar(estado, resultados, ahora)
    salida = 0
    if avisos:
        asunto, texto, whatsapp = redactar(avisos, ahora)
        if avisar(asunto, texto, whatsapp):
            for a in avisos:
                if a["tipo"] == "caido":
                    estado["chequeos"][a["clave"]]["avisado"] = True
                    estado["chequeos"][a["clave"]]["ultimo_aviso"] = ahora.isoformat()
        else:
            # Ningun canal funciono: no se marca como avisado (se reintenta en
            # la proxima revision) y el job falla para que GitHub avise.
            salida = 1
    if modo == "resumen" or toca_resumen(estado, ahora):
        asunto, texto = redactar_resumen(estado, resultados, ahora)
        try:
            enviar_correo(asunto, texto)  # el resumen semanal va solo por correo
            print("Resumen semanal enviado por correo.")
            estado["ultimo_resumen"] = ahora.astimezone(CHILE).date().isoformat()
        except Exception as exc:
            print(f"ERROR enviando resumen: {type(exc).__name__}: {exc}", file=sys.stderr)
            salida = 1

    nuevo = json.dumps(estado, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if not ARCHIVO_ESTADO.exists() or ARCHIVO_ESTADO.read_text(encoding="utf-8") != nuevo:
        ARCHIVO_ESTADO.write_text(nuevo, encoding="utf-8")
        print("Estado actualizado.")
    return salida


if __name__ == "__main__":
    sys.exit(main())
