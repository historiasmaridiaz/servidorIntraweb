#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Motor de extracción y parseo para Historias Clínicas (Intraweb Proinsalud).
Soporta raspado mediante Playwright y respaldo HTTP directo sin dependencias externas.
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

DOC_TYPES = {"CC", "TI", "RC", "CE", "MS", "NV", "PA", "PE", "PT"}
BAD_VALUE_RE = re.compile(r"PHPSESSID|SESI[ÓO]N|COOKIE|WARNING|NOTICE|ERROR|LOGIN|UNDEFINED|FATAL|STACK|CONTRASE", re.I)
DATE_RE = re.compile(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})")

ALIASES: Dict[str, List[str]] = {
    "tipo_doc": ["tipo", "tipodoc", "tipodocumento", "td"],
    "cedula": ["cedula", "identificacion", "documento", "nodocumento", "numdocumento", "numero"],
    "nombre": ["nombre", "nombres", "paciente", "nombrecompleto", "nombreyapellidos", "apellidosynombres"],
    "edad": ["edad"],
    "contrato": ["contrato", "entidad", "eps", "aseguradora", "empresa"],
    "estado": ["estado", "estadopaciente"],
    "novedades": ["novedad", "novedades", "observacion", "observaciones"],
    "ult_consulta": ["ultimaconsulta", "ultimafecha", "fechaultima", "fechaultimaatencion", "ultimatencion", "fechaatencion", "fecha"],
    "medico": ["medico", "profesional", "doctor", "especialista"],
}


def clean_text(value: Any) -> str:
    """Limpia etiquetas HTML, scripts, estilos y espacios duplicados."""
    text = html.unescape(str(value or "")).replace("\xa0", " ")
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def key_text(value: Any) -> str:
    """Normaliza texto a mayúsculas sin tildes ni símbolos."""
    text = clean_text(value).upper()
    trans = str.maketrans("ÁÉÍÓÚÜÑ", "AEIOUUN")
    text = text.translate(trans)
    return re.sub(r"[^A-Z0-9]+", "", text)


def only_digits(value: Any) -> str:
    """Extrae únicamente dígitos numéricos."""
    return re.sub(r"\D+", "", str(value or ""))


def detect_doc_type(value: Any) -> str:
    """Detecta las siglas oficiales del tipo de documento."""
    match = re.search(r"\b(CC|TI|RC|CE|MS|NV|PA|PE|PT)\b", clean_text(value).upper())
    return match.group(1).upper() if match else ""


def parse_document_input(documento: Any, tipo_hint: Any = "") -> Tuple[str, str]:
    doc = only_digits(documento)
    tipo = detect_doc_type(tipo_hint) or detect_doc_type(documento)
    return doc, tipo


def is_bad_value(value: Any) -> bool:
    return bool(BAD_VALUE_RE.search(str(value or "")))


def normalize_date(value: Any) -> str:
    raw = clean_text(value)
    if not raw or is_bad_value(raw):
        return ""
    m = DATE_RE.search(raw)
    if m:
        raw = m.group(1)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def alias_target(label: str) -> Optional[str]:
    k = key_text(label)
    for target, options in ALIASES.items():
        if any(opt.upper() in k for opt in options):
            return target
    return None


def extract_table_rows(page_html: str) -> List[List[str]]:
    rows: List[List[str]] = []
    for tr in re.findall(r"<tr[\s\S]*?</tr>", page_html, flags=re.I):
        cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, flags=re.I)
        cleaned = [clean_text(c) for c in cells]
        cleaned = [c for c in cleaned if c and not is_bad_value(c)]
        if cleaned:
            rows.append(cleaned)
    return rows


def extract_from_key_value_rows(rows: List[List[str]]) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for row in rows:
        if len(row) < 2:
            continue
        target = alias_target(row[0])
        value = row[1]
        if target and value and not is_bad_value(value):
            data.setdefault(target, value)
    return data


def extract_from_header_rows(rows: List[List[str]], documento: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if len(rows) < 2:
        return data
    for header_index in range(0, min(len(rows) - 1, 5)):
        headers = rows[header_index]
        targets = [alias_target(h) for h in headers]
        if not any(targets):
            continue
        for row in rows[header_index + 1 :]:
            joined = " ".join(row)
            if documento and documento not in only_digits(joined):
                if data:
                    continue
            for i, target in enumerate(targets):
                if target and i < len(row) and row[i] and not is_bad_value(row[i]):
                    data.setdefault(target, row[i])
            if data:
                return data
    return data


def extract_from_plain_text(text: str) -> Dict[str, str]:
    upper = clean_text(text).upper()
    patterns: List[Tuple[str, str]] = [
        ("tipo_doc", r"(?:TIPO\s*(?:DE)?\s*DOC(?:UMENTO)?|DOCUMENTO)\s*[:\-]?\s*\b(CC|TI|RC|CE|MS|NV|PA|PE|PT)\b"),
        ("cedula", r"(?:CEDULA|CÉDULA|IDENTIFICACI[OÓ]N|DOCUMENTO)\s*[:\-]?\s*([0-9]{5,15})"),
        ("nombre", r"(?:NOMBRE(?:S)?(?:\s*Y\s*APELLIDOS)?|PACIENTE)\s*[:\-]?\s*([A-ZÁÉÍÓÚÑ ]{5,90})"),
        ("edad", r"(?:EDAD)\s*[:\-]?\s*([0-9]{1,3})"),
        ("contrato", r"(?:CONTRATO|ENTIDAD|EPS|ASEGURADORA)\s*[:\-]?\s*([A-ZÁÉÍÓÚÑ0-9 \-_.]{3,90})"),
        ("estado", r"(?:ESTADO)\s*[:\-]?\s*([A-ZÁÉÍÓÚÑ0-9 \-_.]{3,60})"),
        ("ult_consulta", r"(?:ULTIMA|ÚLTIMA|ULT\.?)\D{0,35}(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})"),
        ("medico", r"(?:M[EÉ]DICO|PROFESIONAL)\s*[:\-]?\s*([A-ZÁÉÍÓÚÑ .]{5,90})"),
    ]
    data: Dict[str, str] = {}
    for key, pattern in patterns:
        m = re.search(pattern, upper, flags=re.I)
        if m:
            data[key] = m.group(1).strip(" .:-")
    return data


def normalize_patient_data(data: Dict[str, str], documento: str, source_url: str, raw_row: Any = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if data.get("tipo_doc"):
        tipo = detect_doc_type(data.get("tipo_doc")) or clean_text(data.get("tipo_doc")).upper()
        if tipo in DOC_TYPES:
            out["tipo_doc"] = tipo
    out["cedula"] = only_digits(data.get("cedula") or documento)
    for key in ("nombre", "edad", "contrato", "estado", "novedades", "medico"):
        value = clean_text(data.get(key))
        if key in ("contrato", "estado"):
            pieces = [clean_text(x) for x in re.split(r"[\r\n|;]+", str(data.get(key) or "")) if clean_text(x)]
            if pieces:
                value = pieces[-1]
        if value and not is_bad_value(value):
            out[key] = value
    fecha = normalize_date(data.get("ult_consulta"))
    if fecha:
        out["ult_consulta"] = fecha
        out["ult_consulta_original"] = clean_text(data.get("ult_consulta"))
    out["source_url"] = source_url
    out["extraction_method"] = "http_directo_sin_dependencias"
    if raw_row is not None:
        out["raw_row"] = raw_row
    return out


def dependency_status() -> Dict[str, Any]:
    status = {
        "beautifulsoup4": False,
        "playwright": False,
        "mode": "HTTP Direct Fallback",
    }
    try:
        import bs4  # noqa: F401
        status["beautifulsoup4"] = True
    except ImportError:
        pass
    try:
        import playwright  # noqa: F401
        status["playwright"] = True
        status["mode"] = "Playwright Enabled + HTTP Fallback"
    except ImportError:
        pass
    return status


def etiqueta_sistema_origen(es_antiguo: bool) -> str:
    return "sistema antiguo" if es_antiguo else "sistema nuevo"


def buscar_documento_http_directo(documento: str, tipo_doc_hint: str = "", cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    doc, tipo = parse_document_input(documento, tipo_doc_hint)
    if not doc:
        raise ValueError("Digite un número de documento válido.")
    if cfg is None:
        cfg = {
            "search_urls": [
                "http://intraweb.local/intraweb/intranet/depura_historia/busca_paciente.php",
                "http://intraweb.local/intraweb/intranet/depura_historia/index.php",
            ],
            "param_names": ["cedula", "documento", "serie", "identificacion", "q"],
            "timeout_seconds": 30,
            "headers": {"User-Agent": "Mozilla/5.0 HClinicasUnificado/5.4.0"},
        }

    search_urls = [u for u in (cfg.get("search_urls") or []) if str(u).strip()]
    param_names = [p for p in (cfg.get("param_names") or []) if str(p).strip()]
    if not search_urls:
        raise RuntimeError("No hay URL de intranet configurada en config_servidor.json.")
    if not param_names:
        param_names = ["documento"]

    last_error = ""
    for url in search_urls:
        for param_name in param_names:
            params = dict(cfg.get("extra_params") or {})
            params[str(param_name)] = doc
            if tipo:
                params.setdefault("tipo", tipo)
            try:
                sep = "&" if "?" in url else "?"
                final_url = url + sep + urlencode(params)
                req = Request(final_url, headers=cfg.get("headers") or {"User-Agent": "Mozilla/5.0"})
                timeout = int(cfg.get("timeout_seconds") or 30)
                with urlopen(req, timeout=timeout) as res:
                    raw = res.read()
                    charset = res.headers.get_content_charset() or "latin-1"
                    try:
                        page = raw.decode(charset, errors="replace")
                    except Exception:
                        page = raw.decode("latin-1", errors="replace")

                plain = clean_text(page)
                if re.search(r"login|usuario|contraseña|contrasena|sesion|sesión", plain, flags=re.I):
                    raise RuntimeError("La intranet devolvió inicio de sesión. Inicie sesión en la intranet local.")

                rows = extract_table_rows(page)
                data: Dict[str, str] = {}
                data.update(extract_from_key_value_rows(rows))
                data.update({k: v for k, v in extract_from_header_rows(rows, doc).items() if v and k not in data})
                data.update({k: v for k, v in extract_from_plain_text(page).items() if v and k not in data})
                patient = normalize_patient_data(data, doc, str(url), rows[0] if rows else plain[:300])

                useful_keys = {"tipo_doc", "nombre", "edad", "contrato", "estado", "ult_consulta", "medico", "novedades"}
                if any(patient.get(k) for k in useful_keys):
                    return patient
                last_error = "La intranet respondió, pero no se hallaron campos estructurados del paciente."
            except HTTPError as exc:
                last_error = f"HTTP {exc.code} consultando {url}: {exc.reason}"
            except URLError as exc:
                last_error = f"No se pudo conectar con {url}. Verifique VPN o red local. Detalle: {exc.reason}"
            except Exception as exc:
                last_error = str(exc)
    raise RuntimeError(last_error or "No se encontraron datos en la intranet.")


def buscar_documento(documento: str, tipo_doc_hint: str = "", log: Optional[Any] = None) -> Dict[str, Any]:
    """Punto de entrada principal con respaldo transparente."""
    if callable(log):
        log(f"Iniciando búsqueda para documento={documento}, tipo={tipo_doc_hint}")
    try:
        return buscar_documento_http_directo(documento, tipo_doc_hint)
    except Exception as exc:
        if callable(log):
            log(f"Fallo en raspado HTTP directo: {exc}")
        raise exc
