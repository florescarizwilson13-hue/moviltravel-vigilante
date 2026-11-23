"""Reglas de aviso del vigilante, con revisiones y envios simulados."""

from datetime import datetime, timedelta, timezone

import pytest

import vigilante as v

T0 = datetime(2026, 11, 23, 3, 3, tzinfo=timezone.utc)  # 00:03 en Chile


def _ronda(estado, ahora, backend=True, web=True):
    r = {
        "backend": v.Resultado(backend, "ok" if backend else "HTTP 503: no responde"),
        "web": v.Resultado(web, "ok" if web else "la pagina responde HTTP 404"),
    }
    avisos = v.procesar(estado, r, ahora)
    for a in avisos:  # como main() cuando el envio funciona
        if a["tipo"] == "caido":
            estado["chequeos"][a["clave"]]["avisado"] = True
            estado["chequeos"][a["clave"]]["ultimo_aviso"] = ahora.isoformat()
    return avisos


def test_una_falla_sola_no_avisa():
    e = {}
    assert _ronda(e, T0, backend=False) == []
    assert _ronda(e, T0 + timedelta(minutes=10)) == []
    assert e["historial"][0]["avisado"] is False


def test_dos_fallas_seguidas_avisan_una_vez():
    e = {}
    _ronda(e, T0, backend=False)
    (aviso,) = _ronda(e, T0 + timedelta(minutes=10), backend=False)
    assert aviso["tipo"] == "caido" and aviso["clave"] == "backend"
    assert aviso["desde"] == T0.isoformat()


def test_mientras_sigue_caido_repite_maximo_cada_hora():
    e = {}
    enviados = []
    for i in range(14):  # 2 h 10 min caido, revisando cada 10 min
        enviados += _ronda(e, T0 + timedelta(minutes=10 * i), backend=False)
    horas = [a for a in enviados if a["tipo"] == "caido"]
    assert len(horas) == 3  # a los 10 min, a 1 h 10 y a 2 h 10
    assert [a["repeticion"] for a in horas] == [False, True, True]


def test_al_volver_avisa_con_la_duracion():
    e = {}
    _ronda(e, T0, backend=False)
    _ronda(e, T0 + timedelta(minutes=10), backend=False)
    (aviso,) = _ronda(e, T0 + timedelta(minutes=95))
    assert aviso["tipo"] == "volvio"
    assert aviso["duracion"] == "1 h 35 min"
    assert e["chequeos"]["backend"] == {"fallas_seguidas": 0}


def test_las_dos_revisiones_van_en_un_solo_mensaje():
    e = {}
    _ronda(e, T0, backend=False, web=False)
    avisos = _ronda(e, T0 + timedelta(minutes=10), backend=False, web=False)
    asunto, texto, whatsapp = v.redactar(avisos, T0 + timedelta(minutes=10))
    assert asunto.startswith("[CAIDO]")
    assert "Backend y base de datos" in texto and "App web" in texto
    assert "moviltravel-mvp" in texto  # dice que revisar
    assert whatsapp.count("*CAIDO*") == 2


def test_resumen_semanal_lunes_desde_las_9_una_vez():
    lunes_0859 = datetime(2026, 11, 30, 11, 59, tzinfo=timezone.utc)  # 08:59 Chile
    lunes_0903 = lunes_0859 + timedelta(minutes=4)
    e = {}
    assert not v.toca_resumen(e, lunes_0859)
    assert v.toca_resumen(e, lunes_0903)
    e["ultimo_resumen"] = "2026-11-30"
    assert not v.toca_resumen(e, lunes_0903 + timedelta(hours=3))
    assert not v.toca_resumen({}, lunes_0903 + timedelta(days=1))  # martes


def test_si_ningun_canal_funciona_no_marca_avisado(monkeypatch, tmp_path):
    monkeypatch.setattr(v, "ARCHIVO_ESTADO", tmp_path / "estado.json")
    monkeypatch.setattr(v, "revisar_backend", lambda url: v.Resultado(False, "HTTP 503"))
    monkeypatch.setattr(v, "revisar_web", lambda url: v.Resultado(True, "ok"))
    monkeypatch.setattr(v, "toca_resumen", lambda e, a: False)

    def falla(*a):
        raise OSError("sin red")

    monkeypatch.setattr(v, "enviar_correo", falla)
    monkeypatch.setattr(v, "enviar_whatsapp", falla)
    assert v.main() == 0  # primera falla: todavia no avisa
    assert v.main() == 1  # segunda: intenta avisar, falla todo -> job en rojo
    import json

    estado = json.loads((tmp_path / "estado.json").read_text())
    assert "avisado" not in estado["chequeos"]["backend"]


@pytest.mark.parametrize(
    "codigo,cuerpo,ok",
    [
        (200, b'{"status":"ok","db":"ok"}', True),
        (503, b'{"status":"error","db":"no responde"}', False),
        (200, b'{"status":"ok"}', False),  # /health sin base no basta
    ],
)
def test_revisar_backend(monkeypatch, codigo, cuerpo, ok):
    monkeypatch.setattr(v, "_pedir", lambda *a, **k: (codigo, {}, cuerpo))
    assert v.revisar_backend("x").ok is ok
