#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Aplicativo local unificado para Historias Clínicas (Proinsalud).

Endpoints expuestos por el aplicativo:
  GET  /health
  GET  /buscar?documento=123456&tipo=CC
  GET  /buscar_async?documento=123456&tipo=CC
  GET  /resultado?job_id=...
  GET  /config
  POST /config
  GET  /shutdown
  GET  / (y /app/*) -> Interfaz web Dashboard SPA

Diseño v5.4.0:
- Servidor HTTP con hilos independientes y tareas asincrónicas.
- Motor de búsqueda con caché en memoria y reconexión automática.
- Gestión de CORS completo para llamadas locales y externas.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

try:
    import intranet_motor as INTRANET_MOTOR
    INTRANET_MOTOR_IMPORT_ERROR = ""
except Exception as exc:
    INTRANET_MOTOR = None
    INTRANET_MOTOR_IMPORT_ERROR = str(exc)

APP_VERSION = "5.4.0-extractor-fiel-v416-entidad-fecha"
DEFAULT_PORT = int(os.getenv("HCLINICAS_API_PORT", "8765"))
DEFAULT_HOST = os.getenv("HCLINICAS_API_HOST", "127.0.0.1")

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=int(os.getenv("HCLINICAS_MAX_WORKERS", "4")))
SEARCH_CACHE: Dict[str, Dict[str, Any]] = {}
SEARCH_CACHE_LOCK = threading.Lock()
SEARCH_CACHE_TTL = int(os.getenv("HCLINICAS_CACHE_SECONDS", "300"))


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", app_dir())).resolve()


def resource_path(*parts: str) -> Path:
    primary = bundle_dir().joinpath(*parts)
    if primary.exists():
        return primary
    fallback = app_dir().joinpath(*parts)
    if fallback.exists():
        return fallback
    return primary


CONFIG_PATH = app_dir() / "config_servidor.json"
APP_CONFIG_PATH = app_dir() / "config_aplicativo.json"
DEBUG_DIR = app_dir() / "debug"

DEFAULT_CONFIG: Dict[str, Any] = {
    "host": DEFAULT_HOST,
    "port": DEFAULT_PORT,
    "timeout_seconds": 45,
    "search_urls": [
        "http://intraweb.local/intraweb/intranet/depura_historia/busca_paciente.php",
        "http://intraweb.local/intraweb/intranet/depura_historia/index.php",
    ],
    "param_names": ["cedula", "documento", "serie", "identificacion", "q"],
    "extra_params": {},
    "headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HClinicasUnificado/5.4.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    },
}

DEFAULT_APP_CONFIG: Dict[str, Any] = {
    "web_app_url": "https://script.google.com/macros/s/AKfycbzPj94LltysT7OO1s61RlA-vxTDNWvVyYYvJHEA3n_gWxBOAMHwfaOggDOusZ0l_laD/exec",
    "drive_url": "https://docs.google.com/spreadsheets/d/1bbheqGmH1qyfjxLPokK1LKU4VYpFML2t7LqhuWtQ9Dc/edit?gid=0#gid=0",
    "open_browser_on_start": True,
}


def ensure_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        loaded = {}
    cfg = dict(DEFAULT_CONFIG)
    for key, value in loaded.items():
        if key == "headers":
            h = dict(DEFAULT_CONFIG["headers"])
            h.update(value or {})
            cfg[key] = h
        else:
            cfg[key] = value
    cfg["port"] = int(os.getenv("HCLINICAS_API_PORT", str(cfg.get("port") or DEFAULT_PORT)))
    cfg["host"] = os.getenv("HCLINICAS_API_HOST", str(cfg.get("host") or DEFAULT_HOST))
    return cfg


def save_config(new_config: Dict[str, Any]) -> None:
    current = ensure_config()
    current.update(new_config)
    CONFIG_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_app_config() -> Dict[str, Any]:
    if not APP_CONFIG_PATH.exists():
        APP_CONFIG_PATH.write_text(json.dumps(DEFAULT_APP_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        loaded = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        loaded = {}
    cfg = dict(DEFAULT_APP_CONFIG)
    cfg.update({k: v for k, v in (loaded or {}).items() if v is not None})
    return cfg


def save_app_config(new_config: Dict[str, Any]) -> None:
    current = ensure_app_config()
    current.update(new_config)
    APP_CONFIG_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def index_file_path() -> Path:
    candidates = [
        resource_path("app", "index.html"),
        resource_path("index.html"),
        app_dir() / "app" / "index.html",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def render_index_html(api_origin: str = "http://127.0.0.1:8765") -> bytes:
    path = index_file_path()
    if not path.exists():
        return b"<html><body><h1>Servidor HClinicas Activo</h1><p>No se encontro index.html</p></body></html>"
    html_text = path.read_text(encoding="utf-8", errors="replace")
    app_cfg = ensure_app_config()
    web_url = str(app_cfg.get("web_app_url") or DEFAULT_APP_CONFIG["web_app_url"]).strip()
    drive_url = str(app_cfg.get("drive_url") or DEFAULT_APP_CONFIG["drive_url"]).strip()
    html_text = html_text.replace("{{API_ORIGIN}}", api_origin.rstrip("/"))
    html_text = html_text.replace("{{WEB_APP_URL}}", web_url)
    html_text = html_text.replace("{{DRIVE_URL}}", drive_url)
    return html_text.encode("utf-8")


def debug_write(prefix: str, content: str) -> str:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    path = DEBUG_DIR / name
    path.write_text(content, encoding="utf-8", errors="replace")
    return str(path)


def cors_headers() -> Dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Access-Control-Request-Private-Network",
        "Access-Control-Allow-Private-Network": "true",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Cache-Control": "no-store",
    }


def buscar_documento_cached(documento: str, tipo_doc_hint: str = "") -> Dict[str, Any]:
    if INTRANET_MOTOR is None:
        raise RuntimeError(f"Motor intranet no disponible. Error: {INTRANET_MOTOR_IMPORT_ERROR}")

    doc, tipo = INTRANET_MOTOR.parse_document_input(documento, tipo_doc_hint)
    cache_key = f"{APP_VERSION}:{tipo}:{doc}"
    now = time.time()

    if SEARCH_CACHE_TTL > 0:
        with SEARCH_CACHE_LOCK:
            cached = SEARCH_CACHE.get(cache_key)
            if cached and now - float(cached.get("ts", 0)) <= SEARCH_CACHE_TTL:
                data = dict(cached.get("data") or {})
                data["cache_hit"] = True
                return data

    cfg = ensure_config()
    data = INTRANET_MOTOR.buscar_documento(
        documento,
        tipo_doc_hint=tipo_doc_hint,
        log=lambda m: print(f"[INTRANET] {m}", flush=True),
    )

    meaningful = any(str((data or {}).get(key) or "").strip() for key in (
        "nombre", "contrato", "estado", "ult_consulta"
    ))
    if not meaningful:
        raise LookupError("no encontrado")

    if SEARCH_CACHE_TTL > 0:
        with SEARCH_CACHE_LOCK:
            SEARCH_CACHE[cache_key] = {"ts": now, "data": dict(data or {})}

    return data


def cleanup_jobs() -> None:
    ttl = 900
    now = time.time()
    with JOBS_LOCK:
        for job_id in list(JOBS.keys()):
            created = JOBS[job_id].get("created_ts") or now
            if now - float(created) > ttl:
                JOBS.pop(job_id, None)


def create_job(documento: str, tipo: str) -> str:
    cleanup_jobs()
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "pending",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "created_ts": time.time(),
        }

    def run() -> None:
        try:
            data = buscar_documento_cached(documento, tipo)
            with JOBS_LOCK:
                JOBS[job_id].update({
                    "status": "done",
                    "data": data,
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                })
        except LookupError:
            with JOBS_LOCK:
                JOBS[job_id].update({
                    "status": "not_found",
                    "message": "no encontrado",
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                })
        except Exception as exc:
            debug_path = debug_write("error_busqueda", traceback.format_exc())
            with JOBS_LOCK:
                JOBS[job_id].update({
                    "status": "error",
                    "message": str(exc),
                    "debug_error": debug_path,
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                })

    EXECUTOR.submit(run)
    return job_id


class RequestHandler(BaseHTTPRequestHandler):
    server_version = f"HClinicasAPI/{APP_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def local_origin(self) -> str:
        host, port = self.server.server_address[:2]
        if host in ("0.0.0.0", ""):
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.end_headers()

    def json_response(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def response_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                return self.response_bytes(render_index_html(self.local_origin()), "text/html; charset=utf-8")

            if path.startswith("/app/") or path.startswith("/assets/"):
                rel_parts = [p for p in path.split("/") if p and p not in (".", "..")]
                file_path = resource_path(*rel_parts)
                if file_path.exists() and file_path.is_file():
                    mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
                    return self.response_bytes(file_path.read_bytes(), mime)
                return self.json_response({"success": False, "message": "Archivo no encontrado."}, 404)

            if path == "/health":
                cfg = ensure_config()
                app_cfg = ensure_app_config()
                host, port = self.server.server_address[:2]
                if host in ("0.0.0.0", ""):
                    host = "127.0.0.1"
                motor_status = INTRANET_MOTOR.dependency_status() if INTRANET_MOTOR else {"available": False}
                return self.json_response({
                    "success": True,
                    "message": "Servidor local Historias Clinicas activo",
                    "version": APP_VERSION,
                    "host": host,
                    "port": port,
                    "ui_url": f"http://{host}:{port}/",
                    "web_app_url": app_cfg.get("web_app_url"),
                    "drive_url": app_cfg.get("drive_url"),
                    "python_bits": 64 if sys.maxsize > 2 ** 32 else 32,
                    "config_path": str(CONFIG_PATH),
                    "app_config_path": str(APP_CONFIG_PATH),
                    "motor_intranet": motor_status,
                })

            if path == "/config":
                return self.json_response({
                    "success": True,
                    "config": ensure_config(),
                    "config_path": str(CONFIG_PATH),
                    "app_config": ensure_app_config(),
                    "app_config_path": str(APP_CONFIG_PATH),
                })

            if path == "/shutdown":
                self.json_response({"success": True, "message": "Cerrando servidor local..."})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

            if path == "/buscar":
                documento = (params.get("documento") or params.get("cedula") or [""])[0]
                tipo = (params.get("tipo") or params.get("tipo_doc") or [""])[0]
                data = buscar_documento_cached(documento, tipo)
                return self.json_response({"success": True, "data": data})

            if path == "/buscar_async":
                documento = (params.get("documento") or params.get("cedula") or [""])[0]
                tipo = (params.get("tipo") or params.get("tipo_doc") or [""])[0]
                doc, _ = INTRANET_MOTOR.parse_document_input(documento, tipo)
                if not doc:
                    return self.json_response({"success": False, "message": "Digite un número de documento válido."}, 400)
                job_id = create_job(documento, tipo)
                return self.json_response({"success": True, "job_id": job_id, "status": "pending"})

            if path == "/resultado":
                job_id = (params.get("job_id") or [""])[0]
                with JOBS_LOCK:
                    job = dict(JOBS.get(job_id) or {})
                if not job:
                    return self.json_response({"success": False, "status": "not_found", "message": "No se encontró la búsqueda."}, 404)
                if job.get("status") == "done":
                    return self.json_response({"success": True, "status": "done", "data": job.get("data") or {}})
                if job.get("status") == "not_found":
                    return self.json_response({"success": False, "status": "not_found", "message": "no encontrado"}, 404)
                if job.get("status") == "error":
                    return self.json_response({"success": False, "status": "error", "message": job.get("message") or "Error en búsqueda", "debug_error": job.get("debug_error")}, 500)
                return self.json_response({"success": True, "status": "pending"})

            return self.json_response({"success": False, "message": "Ruta no encontrada."}, 404)
        except LookupError:
            return self.json_response({"success": False, "status": "not_found", "message": "no encontrado"}, 404)
        except Exception as exc:
            debug_path = debug_write("error_api", traceback.format_exc())
            return self.json_response({"success": False, "message": str(exc), "debug_error": debug_path}, 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length) if length > 0 else b"{}"

        try:
            payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
        except Exception:
            payload = {}

        if path == "/config":
            if "server_config" in payload:
                save_config(payload["server_config"])
            if "app_config" in payload:
                save_app_config(payload["app_config"])
            return self.json_response({
                "success": True,
                "message": "Configuración actualizada correctamente.",
                "config": ensure_config(),
                "app_config": ensure_app_config(),
            })

        return self.json_response({"success": False, "message": "Endpoint POST no encontrado."}, 404)


def self_test() -> None:
    print("Iniciando pruebas internas (Self-Test)...")
    doc, tipo = INTRANET_MOTOR.parse_document_input("CC 123456")
    assert doc == "123456", f"Falló parse_document_input: {doc}"
    assert tipo == "CC", f"Falló tipo: {tipo}"

    sample_html = """
    <table>
      <tr><th>Tipo Doc</th><th>Documento</th><th>Nombre y Apellidos</th><th>Entidad</th><th>Estado</th><th>Ultima Atencion</th></tr>
      <tr><td>CC</td><td>123456</td><td>JUAN PEREZ SANCHEZ</td><td>PROINSALUD EPS</td><td>ACTIVO</td><td>2026-05-14</td></tr>
    </table>
    """
    rows = INTRANET_MOTOR.extract_table_rows(sample_html)
    header_data = INTRANET_MOTOR.extract_from_header_rows(rows, "123456")
    patient = INTRANET_MOTOR.normalize_patient_data(header_data, "123456", "http://test.local")
    assert patient["cedula"] == "123456"
    assert patient["nombre"] == "JUAN PEREZ SANCHEZ"
    assert patient["contrato"] == "PROINSALUD EPS"
    assert patient["estado"] == "ACTIVO"

    print("SELF TEST OK - Motor de extracción y servidores funcionando correctamente.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Servidor local Historias Clínicas Proinsalud")
    parser.add_argument("--self-test", action="store_true", help="Ejecuta autodiagnóstico interno")
    parser.add_argument("--host", default=None, help="Host binding (ej. 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Puerto binding (ej. 8765)")
    parser.add_argument("--no-browser", action="store_true", help="No abrir navegador automáticamente")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    cfg = ensure_config()
    ensure_app_config()
    host = args.host or str(cfg.get("host") or DEFAULT_HOST)
    port = int(args.port or cfg.get("port") or DEFAULT_PORT)
    ui_url = f"http://{host}:{port}/"

    try:
        server = ThreadingHTTPServer((host, port), RequestHandler)
    except OSError as exc:
        print(f"ERROR: No se pudo iniciar el servidor en {host}:{port}. Detalle: {exc}")
        return

    print("=" * 75)
    print("Servidor Local Historias Clínicas Proinsalud - Activo")
    print(f"Versión: {APP_VERSION}")
    print(f"Python: {sys.version.split()[0]} ({'64 bits' if sys.maxsize > 2**32 else '32 bits'})")
    print(f"Dashboard Web: {ui_url}")
    print(f"API Health:    {ui_url}health")
    print("Presione Ctrl+C para detener.")
    print("=" * 75)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(ui_url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido por el usuario.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
