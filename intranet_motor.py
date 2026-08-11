#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Motor de búsqueda de Historias Clínicas para Intraweb Proinsalud.

Extracción FIEL al original intranet_autofill_api.py v5.4.0:
  - extract_tables_from_html  → maneja rowspan/colspan igual que el original
  - patient_from_tables       → busca fila exacta por cédula con lógica v4.16
  - patient_from_text         → respaldo por texto visible con regex robustas
  - buscar_documento          → flujo completo Playwright + servidor2 + fecha

Compatibilidad:
  - Win8/10/11: Playwright automático con Chrome/Edge/Chromium (motor completo).
  - Win7+:      HTTP directo como respaldo si Playwright no está disponible.
"""

from __future__ import annotations

import csv
import html as html_lib
import json
import os
import re
import socket
import sys
import time
import traceback
import uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

# ────────────────────────────────────────────────────────────────
# Detección de dependencias opcionales
# ────────────────────────────────────────────────────────────────
try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False

try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False

PLAYWRIGHT_AVAILABLE = _PLAYWRIGHT_OK and _BS4_OK

APP_VERSION = "5.4.0-extractor-fiel-v416-entidad-fecha"

# ────────────────────────────────────────────────────────────────
# Constantes globales (idénticas al original)
# ────────────────────────────────────────────────────────────────
DOC_TYPES = {"CC", "TI", "RC", "CE", "MS", "NV", "PA", "PE", "PT"}
BAD_VALUE_RE = re.compile(
    r"PHPSESSID|SESI[ÓO]N|COOKIE|WARNING|NOTICE|ERROR|LOGIN|UNDEFINED|FATAL|STACK|CONTRASE",
    re.I,
)
DOC_TYPE_RE = re.compile(r"\b(CC|TI|RC|CE|MS|NV|PA|PE|PT)\b", re.I)
DEATH_STATE_RE = re.compile(
    r"\b(FALLECID[OA]S?|MUERT[OA]S?|DEFUNCI[OÓ]N|DEFUNCION|[OÓ]BITO|OBITO)\b", re.I
)
STATE_RE = re.compile(
    r"\b(ACTIVO|INACTIVO|RETIRADO|FALLECID[OA]S?|MUERT[OA]S?|SIN\s+REGISTRO)\b", re.I
)

JOB_EXECUTOR = ThreadPoolExecutor(max_workers=int(os.getenv("INTRANET_MAX_WORKERS", "2")))
JOBS: Dict[str, Dict[str, Any]] = {}
import threading
JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = int(os.getenv("INTRANET_JOB_TTL_SECONDS", "900"))

# ────────────────────────────────────────────────────────────────
# Configuración por defecto Playwright
# ────────────────────────────────────────────────────────────────
DEFAULT_CONFIG: Dict[str, Any] = {
    "url": os.getenv(
        "INTRANET_SEARCH_URL",
        "http://intraweb.local/intraweb/intranet/depura_historia/busca_paciente.php",
    ),
    "url_alternativa": os.getenv(
        "INTRANET_ALT_URL",
        "http://intraweb.local/intraweb/intranet/depura_historia/index.php",
    ),
    "url_servidor2_fecha": os.getenv(
        "INTRANET_URL_SERVIDOR2_FECHA",
        "http://192.168.3.90/intraweb/intranet/depura_historia/busca_paciente.php",
    ),
    "headless": os.getenv("INTRANET_HEADLESS", "0").strip().lower() in {"1", "true", "si", "sí", "yes"},
    "timeout_ms": int(os.getenv("INTRANET_TIMEOUT_MS", "45000")),
    "viewport": {"width": 1365, "height": 911},
    "selectors": {
        "input_cedula": [
            "input[name*='ced' i]",
            "input[id*='ced' i]",
            "input[name*='serie' i]",
            "input[id*='serie' i]",
            "table:nth-of-type(2) td:nth-of-type(1) > input",
            "xpath=/html/body/form/form/table[2]/tbody/tr[2]/td[1]/input",
            "input[type='text']",
            "input:not([type])",
        ],
        "boton_buscar": [
            "input[value*='buscar' i]",
            "button:has-text('buscar')",
            "button:has-text('Buscar')",
            "td:nth-of-type(2) > input",
            "xpath=/html/body/form/form/table[2]/tbody/tr[2]/td[2]/input",
            "input[type='submit']",
            "input[type='button']",
            "button",
            "text=buscar",
            "text=Buscar",
            "text=BUSCAR",
        ],
        "boton_historico": [
            "a[href*='ver_historico.php' i]",
            "*[onclick*='ver_historico.php' i]",
            "img[onclick*='ver_historico.php' i]",
            "a[href*='hist' i]",
            "img[alt*='hist' i]",
        ],
    },
}


# ────────────────────────────────────────────────────────────────
# Utilidades de texto (idénticas al original)
# ────────────────────────────────────────────────────────────────

def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = app_dir() / "config_servidor.json"


def deep_merge(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in (updates or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_intranet_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
    try:
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            search_urls = raw.get("search_urls") or []
            if isinstance(search_urls, list) and search_urls:
                cfg["url"] = str(search_urls[0]).strip() or cfg["url"]
                if len(search_urls) > 1 and str(search_urls[1]).strip():
                    cfg["url_alternativa"] = str(search_urls[1]).strip()
            if raw.get("url"):
                cfg["url"] = str(raw.get("url")).strip()
            if raw.get("url_alternativa"):
                cfg["url_alternativa"] = str(raw.get("url_alternativa")).strip()
            for _secondary_key in ("url_servidor2_fecha", "url_fecha_secundaria", "url_secundaria_fecha"):
                if raw.get(_secondary_key):
                    cfg["url_servidor2_fecha"] = str(raw.get(_secondary_key)).strip()
                    break
            if raw.get("timeout_ms"):
                cfg["timeout_ms"] = int(raw.get("timeout_ms"))
            elif raw.get("timeout_seconds"):
                cfg["timeout_ms"] = int(raw.get("timeout_seconds")) * 1000
            if "headless" in raw:
                cfg["headless"] = bool(raw.get("headless"))
            if isinstance(raw.get("viewport"), dict):
                cfg["viewport"] = deep_merge(cfg.get("viewport", {}), raw.get("viewport") or {})
            if isinstance(raw.get("selectors"), dict):
                cfg["selectors"] = deep_merge(cfg.get("selectors", {}), raw.get("selectors") or {})
    except Exception as exc:
        print(f"[AVISO] No se pudo leer config_servidor.json: {exc}", flush=True)
    return cfg


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html_lib.unescape(str(value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def clean_cell_text(cell: Any) -> str:
    """Limpia una celda BeautifulSoup conservando saltos útiles."""
    raw = html_lib.unescape(cell.get_text("\n")).replace("\xa0", " ")
    lines = [clean_text(line) for line in raw.splitlines()]
    lines = [line for line in lines if line and not is_bad_value(line)]
    return "\n".join(lines)


def only_digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def detect_doc_type(value: Any) -> str:
    text = clean_text(value).upper()
    match = DOC_TYPE_RE.search(text)
    return match.group(1).upper() if match else ""


def parse_document_input(documento: Any, tipo_hint: Any = "") -> Tuple[str, str]:
    doc_type = detect_doc_type(tipo_hint) or detect_doc_type(documento)
    return only_digits(documento), doc_type


def etiqueta_sistema_origen(fecha_desde_servidor2: bool) -> str:
    return "sistema antiguo" if bool(fecha_desde_servidor2) else "sistema nuevo"


def normalize_doc_type(value: Any) -> str:
    doc_type = detect_doc_type(value) or clean_text(value).upper()
    return doc_type if doc_type in DOC_TYPES else ""


def is_bad_value(value: Any) -> bool:
    return bool(BAD_VALUE_RE.search(str(value or "")))


def normalize_date(value: Any) -> str:
    value = clean_text(value)
    if not value or is_bad_value(value):
        return ""
    m = re.search(r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})", value)
    if m:
        value = m.group(1)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
                "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return value


def parse_date_candidate(value: Any) -> Optional[datetime]:
    text = clean_text(value)
    if not text:
        return None
    match = re.search(r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})", text)
    if not match:
        return None
    raw = match.group(1)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
                "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
                "%d/%m/%y", "%d-%m-%y", "%d.%m.%y"):
        try:
            dt = datetime.strptime(raw, fmt)
            if 1900 <= dt.year <= datetime.now().year + 1:
                return dt
        except ValueError:
            pass
    return None


def date_context_is_noise(context: str) -> bool:
    ctx = key_text(context)
    noise = [
        "FECHALIMITE", "ARCHIVOCENTRAL", "LIMITEPARAARCHIVO",
        "NACIMIENTO", "FECHANAC", "FECNAC", "EXPEDICION", "VENCIMIENTO",
        "COPYRIGHT", "SESSION", "SESION",
    ]
    return any(token in ctx for token in noise)


def collect_date_candidates_from_text(text: str, prefer_context: bool = False) -> List[Tuple[datetime, str, str]]:
    candidates: List[Tuple[datetime, str, str]] = []
    if not text:
        return candidates
    date_re = re.compile(r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})")
    for match in date_re.finditer(text):
        start = max(0, match.start() - 120)
        end = min(len(text), match.end() + 120)
        context = clean_text(text[start:end])
        if date_context_is_noise(context):
            continue
        if prefer_context:
            ctx = key_text(context)
            clinical_tokens = ["FECHA", "ATENCION", "CONSULTA", "SERVICIO", "EVOLUCION",
                               "INGRESO", "EGRESO", "HISTORICO", "HISTORIA"]
            if not any(token in ctx for token in clinical_tokens):
                continue
        dt = parse_date_candidate(match.group(1))
        if dt:
            candidates.append((dt, normalize_date(match.group(1)), context))
    return candidates


def key_text(value: Any) -> str:
    text = clean_text(value).upper()
    trans = str.maketrans("ÁÉÍÓÚÜÑ", "AEIOUUN")
    text = text.translate(trans)
    return re.sub(r"[^A-Z0-9]+", "", text)


def infer_estado_from_text(text: Any, current: str = "") -> str:
    source = clean_text(text)
    if DEATH_STATE_RE.search(source):
        return "FALLECIDO"
    matches = list(STATE_RE.finditer(source))
    if matches:
        value = clean_text(matches[-1].group(1)).upper()
        if value.startswith("FALLECID") or value.startswith("MUERT"):
            return "FALLECIDO"
        return value
    return clean_text(current).upper()


def split_cell_values(value: Any) -> List[str]:
    if value is None:
        return []
    raw = html_lib.unescape(str(value)).replace("\xa0", " ")
    parts: List[str] = []
    for piece in re.split(r"[\r\n|;]+", raw):
        piece = clean_text(piece).strip(" :-=.")
        if piece and not is_bad_value(piece):
            parts.append(piece)
    if not parts:
        one = clean_text(raw).strip(" :-=.")
        if one and not is_bad_value(one):
            parts.append(one)
    return parts


def pick_last_value(value: Any) -> str:
    parts = split_cell_values(value)
    return parts[-1] if parts else ""


def pair_contract_state_values(contrato_raw: Any, estado_raw: Any) -> List[Tuple[str, str, int]]:
    contratos = split_cell_values(contrato_raw)
    estados = split_cell_values(estado_raw)
    if not contratos:
        return [("", estados[-1] if estados else "", 0)]
    result: List[Tuple[str, str, int]] = []
    for i, contrato in enumerate(contratos):
        estado = ""
        if len(estados) == len(contratos):
            estado = estados[i]
        elif len(estados) > 1:
            j = i + (len(estados) - len(contratos))
            if 0 <= j < len(estados):
                estado = estados[j]
        elif len(estados) == 1 and i == len(contratos) - 1:
            estado = estados[0]
        result.append((contrato, estado, i))
    return result


def get_raw_by_idx(row: List[str], idx: Optional[int]) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return str(row[idx] or "")


def last_nonempty(values: List[Any]) -> str:
    for value in reversed(values or []):
        cleaned = clean_text(value)
        if cleaned and not is_bad_value(cleaned):
            return cleaned
    return ""


def unique_nonempty(values: List[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values or []:
        cleaned = clean_text(value)
        key = cleaned.upper()
        if cleaned and not is_bad_value(cleaned) and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


# ────────────────────────────────────────────────────────────────
# Extracción de tablas con rowspan/colspan (IDÉNTICA al original)
# ────────────────────────────────────────────────────────────────

def table_score(headers: List[str]) -> int:
    joined = " ".join(key_text(h) for h in headers)
    wanted = [
        "TIPODOC", "TIPODOCUMENTO", "IDENTIFICACION", "CEDULA", "DOCUMENTO",
        "NOMBRE", "PACIENTE", "EDAD", "CONTRATO", "ENTIDAD", "EPS",
        "ASEGURADORA", "ESTADO", "AFILIACION", "ULTCONSULTA", "ULTIMAATENCION",
        "FECHAATENCION", "MEDICO", "PROFESIONAL", "NOVEDAD",
    ]
    return sum(1 for w in wanted if w in joined)


def make_unique_headers(headers: List[str]) -> List[str]:
    result: List[str] = []
    used: Dict[str, int] = {}
    for idx, h in enumerate(headers, start=1):
        name = clean_text(h) or f"Columna_{idx}"
        if name in used:
            used[name] += 1
            name = f"{name}_{used[name]}"
        else:
            used[name] = 1
        result.append(name)
    return result


def rows_to_records(rows: List[List[str]]) -> Dict[str, Any]:
    if not rows:
        return {"headers": [], "records": [], "rows": []}
    max_cols = max(len(r) for r in rows)
    norm_rows = [r + [""] * (max_cols - len(r)) for r in rows]
    first = norm_rows[0]
    if len(norm_rows) > 1 and any(first):
        headers = make_unique_headers(first)
        data_rows = norm_rows[1:]
    else:
        headers = make_unique_headers([f"Columna_{i+1}" for i in range(max_cols)])
        data_rows = norm_rows
    records = []
    for row in data_rows:
        if not any(clean_text(x) for x in row):
            continue
        records.append({headers[i]: clean_text(row[i]) for i in range(len(headers))})
    return {"headers": headers, "records": records, "rows": norm_rows}


def extract_tables_from_html(html: str) -> List[Dict[str, Any]]:
    """Extrae tablas con rowspan/colspan expandido — IDÉNTICA al original."""
    if not _BS4_OK:
        return _extract_tables_fallback(html)
    soup = BeautifulSoup(html, "html.parser")
    tables: List[Dict[str, Any]] = []
    for table_index, table in enumerate(soup.find_all("table"), start=1):
        rows: List[List[str]] = []
        pending_rowspans: Dict[int, Tuple[str, int]] = {}
        for tr in table.find_all("tr"):
            row: List[str] = []
            col = 0

            def fill_pending() -> None:
                nonlocal col
                while col in pending_rowspans:
                    text, remaining = pending_rowspans[col]
                    row.append(text)
                    if remaining > 1:
                        pending_rowspans[col] = (text, remaining - 1)
                    else:
                        pending_rowspans.pop(col, None)
                    col += 1

            fill_pending()
            for cell in tr.find_all(["th", "td"]):
                fill_pending()
                text = clean_cell_text(cell)
                try:
                    rowspan = max(1, int(cell.get("rowspan", 1) or 1))
                except Exception:
                    rowspan = 1
                try:
                    colspan = max(1, int(cell.get("colspan", 1) or 1))
                except Exception:
                    colspan = 1
                for _ in range(colspan):
                    row.append(text)
                    if rowspan > 1:
                        pending_rowspans[col] = (text, rowspan - 1)
                    col += 1
            fill_pending()
            if any(clean_text(c) for c in row):
                rows.append(row)
        if not rows:
            continue
        header_index = 0
        best_header_score = -1
        for idx, possible_header in enumerate(rows[: min(8, len(rows))]):
            score = table_score(possible_header)
            if score > best_header_score:
                header_index = idx
                best_header_score = score
        selected_rows = rows[header_index:] if best_header_score >= 2 else rows
        parsed = rows_to_records(selected_rows)
        parsed["numero"] = table_index
        parsed["filas"] = len(parsed["records"])
        parsed["columnas"] = parsed["headers"]
        parsed["header_row_index"] = header_index
        parsed["header_score"] = best_header_score
        tables.append(parsed)
    return tables


def _extract_tables_fallback(html: str) -> List[Dict[str, Any]]:
    """Fallback sin BeautifulSoup (Win7 sin dependencias)."""
    tables: List[Dict[str, Any]] = []
    for i, table_m in enumerate(re.finditer(r"<table[\s\S]*?</table>", html, flags=re.I), start=1):
        table_html = table_m.group(0)
        rows: List[List[str]] = []
        for tr in re.finditer(r"<tr[\s\S]*?</tr>", table_html, flags=re.I):
            cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr.group(0), flags=re.I)
            cleaned = []
            for c in cells:
                t = re.sub(r"<[^>]+>", " ", c)
                t = html_lib.unescape(t).replace("\xa0", " ")
                t = re.sub(r"\s+", " ", t).strip()
                if t and not is_bad_value(t):
                    cleaned.append(t)
            if cleaned:
                rows.append(cleaned)
        if not rows:
            continue
        header_index = 0
        best_score = -1
        for idx, possible_header in enumerate(rows[:min(8, len(rows))]):
            s = table_score(possible_header)
            if s > best_score:
                header_index = idx
                best_score = s
        selected_rows = rows[header_index:] if best_score >= 2 else rows
        parsed = rows_to_records(selected_rows)
        parsed["numero"] = i
        parsed["filas"] = len(parsed["records"])
        parsed["columnas"] = parsed["headers"]
        parsed["header_score"] = best_score
        tables.append(parsed)
    return tables


def extract_visible_text_from_html(html: str) -> str:
    if _BS4_OK:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return clean_text(soup.get_text("\n"))
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


# ────────────────────────────────────────────────────────────────
# Alias e índices de columna (IDÉNTICO al original)
# ────────────────────────────────────────────────────────────────

def alias_index(headers: List[str], aliases: List[str]) -> Optional[int]:
    keys = [key_text(h) for h in headers]
    alias_keys = [key_text(a) for a in aliases]
    for i, k in enumerate(keys):
        if k in alias_keys:
            return i
    for i, k in enumerate(keys):
        if any(a and a in k for a in alias_keys):
            return i
    return None


def get_by_idx(row: List[str], idx: Optional[int]) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return clean_text(row[idx])


# ────────────────────────────────────────────────────────────────
# Extracción por texto visible (IDÉNTICA al original)
# ────────────────────────────────────────────────────────────────

def extract_all_values_after_labels(text: str, labels: List[str], max_len: int = 160) -> List[str]:
    values: List[str] = []
    if not text:
        return values
    boundary = r"(?=\s+(?:TIPO\s*DOC|CEDULA|C[ÉE]DULA|DOCUMENTO|NOMBRE|PACIENTE|EDAD|CONTRATO|ENTIDAD|EPS|ASEGURADORA|ESTADO|NOVEDAD(?:ES)?|ULT(?:IMA|\.)?\s*(?:CONSULTA|ATENCI[ÓO]N)|FECHA\s*(?:ULTIMA|ÚLTIMA)?\s*ATENCI[ÓO]N|M[ÉE]DICO|PROFESIONAL)\b|$)"
    for label in labels:
        pattern = re.compile(rf"{re.escape(label)}\s*[:=.-]*\s*(.+?){boundary}", re.I)
        for match in pattern.finditer(text):
            value = clean_text(match.group(1))[:max_len].strip(" :-=.")
            if value and not is_bad_value(value):
                values.append(value)
    return values


def patient_from_text(text: str, documento: str, source_url: str, tipo_doc_hint: str = "") -> Dict[str, Any]:
    doc, hint = parse_document_input(documento, tipo_doc_hint)
    text = clean_text(text)
    if is_bad_value(text):
        raise RuntimeError("La intranet devolvió una pantalla de sesión/error, no datos del paciente.")

    tipo_values = extract_all_values_after_labels(text, ["TIPO DOC.", "TIPO DOC", "TIPO DOCUMENTO", "TIPO IDENTIFICACION", "TIPO IDENTIFICACIÓN", "TIPO"])
    cedula_values = extract_all_values_after_labels(text, ["CEDULA", "CÉDULA", "NO DOCUMENTO", "NUMERO DOCUMENTO", "NÚMERO DOCUMENTO", "DOCUMENTO", "IDENTIFICACION", "IDENTIFICACIÓN"])
    nombre_values = extract_all_values_after_labels(text, ["NOMBRE Y APELLIDOS", "NOMBRE COMPLETO", "NOMBRE DEL PACIENTE", "NOMBRE", "PACIENTE"])
    contrato_values = extract_all_values_after_labels(text, ["ULTIMA ENTIDAD", "ÚLTIMA ENTIDAD", "CONTRATO ACTUAL", "CONTRATO", "ENTIDAD", "EPS", "ASEGURADORA"])
    estado_values = extract_all_values_after_labels(text, ["ESTADO AFILIACION", "ESTADO AFILIACIÓN", "ESTADO PACIENTE", "ESTADO"])
    novedad_values = extract_all_values_after_labels(text, ["NOVEDADES", "NOVEDAD"])
    medico_values = extract_all_values_after_labels(text, ["MEDICO", "MÉDICO", "PROFESIONAL"])
    edad_values = extract_all_values_after_labels(text, ["EDAD"], max_len=30)

    tipo_doc = normalize_doc_type(last_nonempty(tipo_values)) or hint
    cedula = only_digits(last_nonempty(cedula_values)) or doc
    nombre = last_nonempty(nombre_values).upper()
    contrato = pick_last_value(last_nonempty(contrato_values)).upper()
    estado = infer_estado_from_text(text, pick_last_value(last_nonempty(estado_values)).upper())
    novedades = last_nonempty(novedad_values)
    medico = last_nonempty(medico_values).upper()
    edad = last_nonempty(edad_values)

    date_candidates = collect_date_candidates_from_text(text, prefer_context=True)
    if date_candidates:
        date_candidates.sort(key=lambda item: item[0])
        ult_original = date_candidates[-1][1]
    else:
        ult_original = last_nonempty(extract_all_values_after_labels(text, [
            "FECHA DE ULTIMA ATENCION", "FECHA DE ÚLTIMA ATENCIÓN",
            "ULTIMA ATENCION", "ÚLTIMA ATENCIÓN",
            "ULT CONSULTA", "ULT. CONSULTA", "ULTIMA CONSULTA", "ÚLTIMA CONSULTA",
        ]))

    if doc and doc not in only_digits(text):
        raise RuntimeError("La página cargó, pero el documento digitado no apareció en los datos visibles.")
    if not any([nombre, contrato, estado, ult_original]):
        raise RuntimeError("No pude extraer datos útiles del texto visible de la intranet.")

    data = {
        "tipo_doc": tipo_doc,
        "cedula": cedula or doc,
        "nombre": nombre,
        "edad": edad,
        "contrato": contrato,
        "ultima_entidad": contrato,
        "estado": estado,
        "estado_vigente": estado,
        "novedades": novedades,
        "ult_consulta_original": ult_original,
        "ult_consulta": normalize_date(ult_original),
        "fecha_ultima_atencion": normalize_date(ult_original),
        "medico": medico,
        "source_url": source_url,
        "raw_text": text[:5000],
        "raw_row": [],
        "raw_headers": [],
        "table_number": None,
        "contratos_detectados": unique_nonempty(contrato_values),
        "estados_detectados": unique_nonempty(estado_values),
    }
    expected = ["tipo_doc", "cedula", "nombre", "contrato", "estado", "ult_consulta"]
    data["campos_extraidos"] = [k for k in expected if clean_text(data.get(k))]
    data["campos_faltantes"] = [k for k in expected if not clean_text(data.get(k))]
    data["extraccion_completa"] = not data["campos_faltantes"]
    return data


# ────────────────────────────────────────────────────────────────
# Extracción por tablas (IDÉNTICA al original v4.16)
# ────────────────────────────────────────────────────────────────

def patient_from_tables(tables: List[Dict[str, Any]], documento: str, source_url: str, tipo_doc_hint: str = "") -> Dict[str, Any]:
    doc, hint_type = parse_document_input(documento, tipo_doc_hint)
    exact_candidates: List[Dict[str, Any]] = []
    anchored_candidates: List[Dict[str, Any]] = []
    global_order = 0

    for table in tables:
        headers = table.get("headers", []) or []
        rows = table.get("rows", []) or []
        score = table_score(headers)
        if score < 2:
            continue
        indexes = {
            "tipo_doc": alias_index(headers, ["TIPO DOC", "TIPO DOCUMENTO", "TIPO IDENTIFICACION", "TIPO IDENTIFICACIÓN", "TIPO"]),
            "cedula": alias_index(headers, ["CEDULA", "CÉDULA", "NO DOCUMENTO", "NUMERO DOCUMENTO", "NÚMERO DOCUMENTO", "DOCUMENTO", "IDENTIFICACION", "IDENTIFICACIÓN", "NUMERO", "SERIE"]),
            "nombre": alias_index(headers, ["NOMBRE", "NOMBRE Y APELLIDOS", "NOMBRE COMPLETO", "NOMBRE DEL PACIENTE", "PACIENTE", "USUARIO"]),
            "edad": alias_index(headers, ["EDAD"]),
            "contrato": alias_index(headers, ["ULTIMA ENTIDAD", "ÚLTIMA ENTIDAD", "ENTIDAD ACTUAL", "CONTRATO ACTUAL", "CONTRATO", "ENTIDAD", "EPS", "ASEGURADORA", "EMPRESA", "CONVENIO"]),
            "estado": alias_index(headers, ["ESTADO AFILIACION", "ESTADO AFILIACIÓN", "ESTADO PACIENTE", "ESTADO", "SITUACION", "SITUACIÓN", "VIGENCIA"]),
            "novedades": alias_index(headers, ["NOVEDADES", "NOVEDAD", "OBSERVACION", "OBSERVACIÓN"]),
            "ult_consulta": alias_index(headers, ["FECHA DE ULTIMA ATENCION", "FECHA DE ÚLTIMA ATENCIÓN", "ULTIMA ATENCION", "ÚLTIMA ATENCIÓN", "ULT CONSULTA", "ULT. CONSULTA", "ULTIMA CONSULTA", "ÚLTIMA CONSULTA", "ULT CONSULT", "FECHA CONSULTA", "FECHA ATENCION", "FECHA ATENCIÓN"]),
            "medico": alias_index(headers, ["MEDICO", "MÉDICO", "PROFESIONAL", "ESPECIALISTA"]),
        }

        candidate_rows = rows[1:] if len(rows) > 1 else rows
        carried = {"tipo_doc": "", "cedula": "", "nombre": "", "edad": ""}
        anchored = False

        for row_position, row in enumerate(candidate_rows):
            global_order += 1
            row_text = " | ".join(clean_text(x) for x in row)
            if not row_text or is_bad_value(row_text):
                continue

            explicit_doc = only_digits(get_by_idx(row, indexes["cedula"]))
            row_has_doc = bool(doc and doc in only_digits(row_text))
            exact_match = bool(doc and (explicit_doc == doc or row_has_doc))

            if explicit_doc:
                anchored = explicit_doc == doc
            elif exact_match:
                anchored = True

            raw_identity = {
                "tipo_doc": normalize_doc_type(get_by_idx(row, indexes["tipo_doc"])),
                "cedula": explicit_doc,
                "nombre": get_by_idx(row, indexes["nombre"]).upper(),
                "edad": get_by_idx(row, indexes["edad"]),
            }
            if exact_match or anchored:
                for key, value in raw_identity.items():
                    if clean_text(value):
                        carried[key] = clean_text(value)

            if not exact_match and not anchored:
                continue

            contrato_raw = get_raw_by_idx(row, indexes["contrato"])
            estado_raw = get_raw_by_idx(row, indexes["estado"])
            fecha_raw = pick_last_value(get_raw_by_idx(row, indexes["ult_consulta"]))
            contract_state_pairs = pair_contract_state_values(contrato_raw, estado_raw)

            for contrato_piece, estado_piece, pair_position in contract_state_pairs:
                contrato = clean_text(contrato_piece).upper()
                if clean_text(estado_piece):
                    estado = infer_estado_from_text(estado_piece, clean_text(estado_piece).upper())
                elif len(contract_state_pairs) == 1:
                    estado = infer_estado_from_text(row_text, "")
                else:
                    estado = ""

                candidate = {
                    "tipo_doc": carried["tipo_doc"] or hint_type,
                    "cedula": carried["cedula"] or doc,
                    "nombre": carried["nombre"],
                    "edad": carried["edad"],
                    "contrato": contrato,
                    "estado": estado,
                    "novedades": pick_last_value(get_raw_by_idx(row, indexes["novedades"])),
                    "ult_consulta_original": fecha_raw,
                    "ult_consulta": normalize_date(fecha_raw),
                    "fecha_obj": parse_date_candidate(fecha_raw),
                    "medico": pick_last_value(get_raw_by_idx(row, indexes["medico"])).upper(),
                    "row_order": global_order * 1000 + pair_position,
                    "visual_row_order": global_order,
                    "contract_pair_position": pair_position,
                    "row_position": row_position,
                    "table_number": table.get("numero"),
                    "table_score": score,
                    "raw_row": row,
                    "raw_headers": headers,
                    "contrato_original": clean_text(contrato_raw),
                    "estado_original": clean_text(estado_raw),
                    "row_text": row_text,
                    "exact_document_match": exact_match,
                }
                useful = any(clean_text(candidate.get(k)) for k in ["nombre", "contrato", "estado", "novedades", "ult_consulta", "medico"])
                if useful:
                    anchored_candidates.append(candidate)
                    if exact_match:
                        exact_candidates.append(candidate)

    candidates = exact_candidates or anchored_candidates
    if not candidates:
        raise RuntimeError("No encontré una tabla válida del paciente después de buscar la cédula.")

    contract_rows = [c for c in candidates if clean_text(c.get("contrato"))]
    selected = max(contract_rows or candidates, key=lambda c: c["row_order"])

    def first_field(field: str) -> str:
        for c in candidates:
            value = clean_text(c.get(field))
            if value:
                return value
        return ""

    def last_field(field: str) -> str:
        return last_nonempty([c.get(field) for c in candidates])

    contrato = clean_text(selected.get("contrato")) or last_field("contrato")
    estado = clean_text(selected.get("estado"))
    if estado:
        estado = infer_estado_from_text(estado, estado)
    elif not contract_rows:
        estado = last_field("estado")

    dated_rows = [c for c in candidates if c.get("fecha_obj")]
    latest_date_row = max(dated_rows, key=lambda c: (c["fecha_obj"], c["row_order"])) if dated_rows else None
    ult_original = clean_text((latest_date_row or {}).get("ult_consulta_original"))

    data = {
        "tipo_doc": normalize_doc_type(first_field("tipo_doc") or last_field("tipo_doc")) or hint_type,
        "cedula": only_digits(first_field("cedula") or last_field("cedula")) or doc,
        "nombre": (first_field("nombre") or last_field("nombre")).upper(),
        "edad": first_field("edad") or last_field("edad"),
        "contrato": contrato.upper(),
        "ultima_entidad": contrato.upper(),
        "entidad": contrato.upper(),
        "estado": estado.upper(),
        "estado_vigente": estado.upper(),
        "novedades": clean_text(selected.get("novedades")) or last_field("novedades"),
        "ult_consulta_original": ult_original,
        "ult_consulta": normalize_date(ult_original),
        "fecha_ultima_atencion": normalize_date(ult_original),
        "medico": clean_text((latest_date_row or selected).get("medico")) or last_field("medico"),
        "source_url": source_url,
        "raw_row": selected.get("raw_row", []),
        "raw_headers": selected.get("raw_headers", []),
        "table_number": selected.get("table_number"),
        "selected_row_position": selected.get("row_position"),
        "contrato_original": selected.get("contrato_original", ""),
        "estado_original": selected.get("estado_original", ""),
        "contratos_detectados": unique_nonempty([c.get("contrato") for c in candidates]),
        "estados_detectados": unique_nonempty([c.get("estado") for c in candidates]),
        "filas_paciente_detectadas": len(candidates),
        "filas_documento_exacto": len(exact_candidates),
        "seleccion_ultimo_contrato": "ultima_fila_visual_estado_misma_fila_v55",
        "estado_misma_fila_contrato": True,
    }

    for k, v in list(data.items()):
        if isinstance(v, str) and is_bad_value(v):
            data[k] = ""
    if not data.get("cedula"):
        data["cedula"] = doc
    if not any([data.get("nombre"), data.get("contrato"), data.get("estado")]):
        raise RuntimeError("No encontrado")

    expected = ["tipo_doc", "cedula", "nombre", "contrato", "estado", "ult_consulta"]
    data["campos_extraidos"] = [k for k in expected if clean_text(data.get(k))]
    data["campos_faltantes"] = [k for k in expected if not clean_text(data.get(k))]
    data["extraccion_completa"] = not data["campos_faltantes"]
    return data


# ────────────────────────────────────────────────────────────────
# Ver Histórico – extraer fecha última atención (IDÉNTICO)
# ────────────────────────────────────────────────────────────────

def save_debug_file(name: str, content: str) -> str:
    debug_dir = app_dir() / "debug_intranet"
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / name
    path.write_text(content, encoding="utf-8", errors="replace")
    return str(path)


def check_host(url: str) -> Tuple[bool, str]:
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
    try:
        ip = socket.gethostbyname(host)
        return True, f"DNS OK: {host} -> {ip}"
    except Exception as exc:
        return False, f"No resuelve {host}. Verifique red interna/VPN. Detalle: {exc}"


def normalizar_historico_url(raw_url: Any, base_url: str) -> str:
    raw = html_lib.unescape(str(raw_url or "")).strip()
    raw = raw.replace("\\/", "/").replace("\\u0026", "&")
    raw = raw.strip("'\"` ;()[]{}").replace("&amp;", "&")
    if not raw:
        return ""
    return urljoin(base_url, raw)


def extraer_urls_historico_desde_html(html: str, base_url: str, documento: str = "", nombre: str = "") -> List[str]:
    urls: List[str] = []
    seen: set = set()

    def add(value: Any) -> None:
        if value is None:
            return
        blob = html_lib.unescape(str(value)).replace("&amp;", "&")
        pattern = re.compile(r"((?:https?://|/|\.\.?/)?[^'\"\<\>\s)]*ver_historico\.php\?[^'\"\<\>\s)]*)", re.I)
        for match in pattern.finditer(blob):
            candidate = normalizar_historico_url(match.group(1), base_url)
            if candidate and candidate not in seen:
                seen.add(candidate)
                urls.append(candidate)

    if _BS4_OK:
        try:
            soup = BeautifulSoup(html or "", "html.parser")
            for tag in soup.find_all(True):
                if tag.get("href"):
                    add(tag.get("href"))
                for _key, value in (tag.attrs or {}).items():
                    if isinstance(value, (list, tuple)):
                        value = " ".join(str(v) for v in value)
                    if "ver_historico.php" in str(value).lower() or "historico" in str(value).lower():
                        add(value)
        except Exception:
            pass
    add(html or "")

    if not urls:
        return []

    doc_digits = only_digits(documento)
    nombre_key = key_text(nombre or "")

    def score(url: str) -> int:
        uk = key_text(url)
        s = 0
        if doc_digits and doc_digits in only_digits(url):
            s += 100
        if "CEDULA" in uk:
            s += 20
        if "GCODI" in uk:
            s += 15
        if nombre_key and nombre_key[:12] and nombre_key[:12] in uk:
            s += 10
        return s

    urls.sort(key=score, reverse=True)
    return urls


def extract_latest_date_from_historico_html(html: str, visible_text: str = "") -> Dict[str, Any]:
    candidates: List[Tuple[datetime, str, str]] = []
    try:
        tables = extract_tables_from_html(html)
        for table in tables:
            headers = table.get("headers", []) or []
            rows = table.get("rows", []) or []
            date_indexes: List[int] = []
            for i, header in enumerate(headers):
                h = key_text(header)
                if any(token in h for token in ["FECHA", "FEC", "ATENCION", "CONSULTA", "SERVICIO", "INGRESO", "EGRESO", "EVOLUCION"]):
                    date_indexes.append(i)
            for row in (rows[1:] if len(rows) > 1 else rows):
                if date_indexes:
                    for idx in date_indexes:
                        value = get_raw_by_idx(row, idx)
                        around = " | ".join(clean_text(x) for x in row)
                        if date_context_is_noise(around):
                            continue
                        for candidate in collect_date_candidates_from_text(value, prefer_context=False):
                            candidates.append((candidate[0], candidate[1], around[:500]))
                else:
                    row_text = " | ".join(clean_text(x) for x in row)
                    candidates.extend(collect_date_candidates_from_text(row_text, prefer_context=True))
    except Exception:
        pass

    visible_blob = visible_text or extract_visible_text_from_html(html)
    candidates.extend(collect_date_candidates_from_text(visible_blob, prefer_context=True))
    if not candidates and "VERHISTORICO" in key_text(html):
        candidates.extend(collect_date_candidates_from_text(visible_blob, prefer_context=False))

    if not candidates:
        return {"success": False, "message": "No se encontraron fechas clínicas en Ver Histórico."}

    candidates.sort(key=lambda item: item[0])
    latest_dt, latest_value, latest_context = candidates[-1]
    return {
        "success": True,
        "ult_consulta": latest_dt.strftime("%Y-%m-%d"),
        "ult_consulta_original": latest_value,
        "historico_contexto": latest_context,
        "historico_fechas_detectadas": sorted({c[0].strftime("%Y-%m-%d") for c in candidates}),
    }


def abrir_historico_por_url(context: Any, cfg: Dict[str, Any], historico_url: str, documento: str, log: Callable) -> Dict[str, Any]:
    timeout_ms = int(cfg.get("timeout_ms") or 45000)
    target_page = None
    try:
        log(f"[INFO] Abriendo Ver Histórico por URL directa: {historico_url}")
        target_page = context.new_page()
        target_page.goto(historico_url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            target_page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        try:
            hist_html = target_page.content()
        except Exception:
            hist_html = ""
        try:
            hist_text = clean_text(target_page.locator("body").inner_text(timeout=5000))
        except Exception:
            hist_text = extract_visible_text_from_html(hist_html)
        result = extract_latest_date_from_historico_html(hist_html, hist_text)
        result["historico_url"] = target_page.url or historico_url
        result["historico_selector"] = "url_directa_ver_historico"
        return result
    except Exception as exc:
        return {"success": False, "message": f"No se pudo abrir Ver Histórico por URL directa: {exc}", "historico_url": historico_url}
    finally:
        try:
            if target_page is not None:
                target_page.close()
        except Exception:
            pass


def extraer_ultima_atencion_desde_historico(page: Any, context: Any, cfg: Dict[str, Any], documento: str, log: Callable) -> Dict[str, Any]:
    try:
        current_html = page.content()
    except Exception:
        current_html = ""
    urls_historico = extraer_urls_historico_desde_html(current_html, page.url, documento)

    for hist_url in urls_historico[:10]:
        direct_result = abrir_historico_por_url(context, cfg, hist_url, documento, log)
        if direct_result.get("success") and direct_result.get("ult_consulta"):
            return direct_result
        log(f"[AVISO] URL Ver Histórico no entregó fecha: {direct_result.get('message')}")

    selectors = ((cfg.get("selectors") or {}).get("boton_historico") or [])
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0:
                try:
                    loc.wait_for(state="visible", timeout=1500)
                    before_pages = list(context.pages)
                    before_url = page.url
                    try:
                        before_text = clean_text(page.locator("body").inner_text(timeout=2000))
                    except Exception:
                        before_text = ""
                    log("[INFO] Abriendo Ver Histórico para extraer Fecha de Última Atención...")
                    loc.click(timeout=5000)
                    page.wait_for_timeout(1200)
                    target_page = page
                    try:
                        current_pages = list(context.pages)
                        new_pages = [p for p in current_pages if p not in before_pages]
                        if new_pages:
                            target_page = new_pages[-1]
                            target_page.wait_for_load_state("domcontentloaded", timeout=min(10000, int(cfg.get("timeout_ms") or 45000)))
                    except Exception:
                        pass
                    try:
                        target_page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass
                    try:
                        hist_html = target_page.content()
                    except Exception:
                        hist_html = ""
                    try:
                        hist_text = clean_text(target_page.locator("body").inner_text(timeout=5000))
                    except Exception:
                        hist_text = extract_visible_text_from_html(hist_html)
                    if target_page is page and target_page.url == before_url and clean_text(hist_text) == before_text:
                        continue
                    result = extract_latest_date_from_historico_html(hist_html, hist_text)
                    result["historico_url"] = target_page.url
                    result["historico_selector"] = selector
                    return result
                except Exception:
                    continue
        except Exception:
            continue
    return {"success": False, "message": "No se encontró URL ni botón Ver Histórico."}


# ────────────────────────────────────────────────────────────────
# Launch browser con fallback Chrome → Edge → Chromium (IDÉNTICO)
# ────────────────────────────────────────────────────────────────

def launch_browser_with_fallback(p: Any, headless: bool, log: Callable) -> Any:
    errores: List[str] = []
    for channel, label in [("chrome", "Google Chrome"), ("msedge", "Microsoft Edge")]:
        try:
            log(f"[INFO] Intentando abrir {label} instalado...")
            return p.chromium.launch(channel=channel, headless=headless)
        except Exception as exc:
            errores.append(f"{label}: {exc}")
            log(f"[AVISO] No se pudo abrir {label}.")
    try:
        log("[INFO] Intentando abrir Chromium de Playwright...")
        return p.chromium.launch(headless=headless)
    except Exception as exc:
        errores.append(f"Chromium Playwright: {exc}")
    raise RuntimeError(
        "No pude abrir ningún navegador. Probé Chrome, Edge y Chromium de Playwright. "
        "Detalle: " + " | ".join(errores[-3:])
    )


def first_visible(page: Any, selectors: List[str], label: str, log: Callable) -> Any:
    errores = []
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0:
                try:
                    loc.wait_for(state="visible", timeout=3000)
                    log(f"[OK] {label}: {selector}")
                    return loc
                except Exception:
                    errores.append(f"{selector} existe pero no visible")
            else:
                errores.append(f"{selector} no existe")
        except Exception as exc:
            errores.append(f"{selector}: {exc}")
    raise RuntimeError(f"No encontré {label}. Selectores probados: " + " | ".join(errores[-8:]))


def click_or_press_enter(page: Any, input_cedula: Any, selectors: Dict[str, List[str]], timeout_ms: int, log: Callable) -> None:
    boton = None
    try:
        boton = first_visible(page, selectors["boton_buscar"], "botón Buscar", log)
    except Exception as exc:
        log(f"[AVISO] No encontré botón Buscar visible. Intentaré con ENTER. Detalle: {exc}")
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=timeout_ms):
            if boton is not None:
                boton.click()
            else:
                input_cedula.press("Enter")
    except PlaywrightTimeoutError:
        log("[AVISO] No se detectó navegación. Puede ser normal.")
        page.wait_for_timeout(3000)
    except Exception as exc:
        log(f"[AVISO] Primer intento de ejecución falló: {exc}")
        if boton is not None:
            try:
                boton.click()
            except Exception:
                input_cedula.press("Enter")
        else:
            input_cedula.press("Enter")
        page.wait_for_timeout(3000)


# ────────────────────────────────────────────────────────────────
# Servidor 2 para fecha (IDÉNTICO al original)
# ────────────────────────────────────────────────────────────────

def buscar_fecha_historico_en_servidor_url(documento: str, tipo_doc_hint: str, cfg: Dict[str, Any], url: str, log: Callable) -> Dict[str, Any]:
    doc, _detected_type = parse_document_input(documento, tipo_doc_hint)
    if not doc or not url:
        return {"success": False, "message": "Servidor 2 sin documento o URL configurada."}
    timeout_ms = int(cfg.get("timeout_ms") or 45000)
    viewport = cfg.get("viewport") or {"width": 1365, "height": 911}
    selectors = cfg.get("selectors") or DEFAULT_CONFIG["selectors"]
    headless = bool(cfg.get("headless"))
    ok_host, host_msg = check_host(url)
    log(f"[INFO] Servidor 2 fecha: {host_msg}")
    if not ok_host:
        log("[AVISO] Continuaré con servidor 2. Si no carga, revise red interna/VPN.")
    with sync_playwright() as p:
        browser = launch_browser_with_fallback(p, headless, log)
        context = browser.new_context(viewport=viewport)
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            log(f"[INFO] Servidor 2: abriendo página: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            input_cedula = first_visible(page, selectors["input_cedula"], "campo CÉDULA / SERIE en servidor 2", log)
            input_cedula.click()
            input_cedula.fill(doc)
            click_or_press_enter(page, input_cedula, selectors, timeout_ms, log)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            result = extraer_ultima_atencion_desde_historico(page, context, cfg, doc, log)
            if result.get("success") and result.get("ult_consulta"):
                result["historico_selector"] = str(result.get("historico_selector") or "") + "|servidor2"
                result["historico_servidor"] = url
                return result
            return {"success": False, "message": result.get("message") or "Servidor 2 no encontró Fecha de Última Atención.", "historico_servidor": url}
        except Exception as exc:
            return {"success": False, "message": f"Servidor 2 falló: {exc}", "historico_servidor": url}
        finally:
            try:
                browser.close()
            except Exception:
                pass


# ────────────────────────────────────────────────────────────────
# Motor HTTP directo (Win7 compatible, sin dependencias)
# ────────────────────────────────────────────────────────────────

def _http_buscar(documento: str, tipo_doc_hint: str = "", log: Optional[Callable] = None) -> Dict[str, Any]:
    def _log(msg: str) -> None:
        if callable(log):
            log(msg)

    cfg = load_intranet_config()
    doc, tipo = parse_document_input(documento, tipo_doc_hint)
    if not doc:
        raise ValueError("Digite un número de documento válido.")

    # Leer URLs y parámetros desde config
    raw_cfg: Dict[str, Any] = {}
    try:
        if CONFIG_PATH.exists():
            raw_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass

    search_urls = [str(u).strip() for u in (raw_cfg.get("search_urls") or [cfg["url"], cfg.get("url_alternativa", "")]) if str(u).strip()]
    param_names = [str(p).strip() for p in (raw_cfg.get("param_names") or ["cedula", "documento", "serie"]) if str(p).strip()]
    timeout = int(raw_cfg.get("timeout_seconds") or 45)
    headers_http = dict(raw_cfg.get("headers") or {"User-Agent": "Mozilla/5.0 HClinicas/5.4.0"})
    last_error = ""

    for url in search_urls:
        ok, dns_msg = check_host(url)
        _log(f"[INFO] DNS {dns_msg}")
        if not ok:
            last_error = dns_msg
            continue
        for param_name in param_names:
            params: Dict[str, str] = {}
            params[param_name] = doc
            if tipo:
                params.setdefault("tipo", tipo)
            try:
                sep = "&" if "?" in url else "?"
                final_url = url + sep + urlencode(params)
                req = Request(final_url, headers=headers_http)
                with urlopen(req, timeout=timeout) as res:
                    raw = res.read()
                    charset = res.headers.get_content_charset() or "latin-1"
                    try:
                        page = raw.decode(charset, errors="replace")
                    except Exception:
                        page = raw.decode("latin-1", errors="replace")

                plain = extract_visible_text_from_html(page)
                if re.search(r"login|usuario|contraseña|contrasena|sesion|sesión", plain, flags=re.I):
                    raise RuntimeError("La intranet requiere inicio de sesión. Abra sesión antes de buscar.")

                tables = extract_tables_from_html(page)
                try:
                    data = patient_from_tables(tables, doc, str(url), tipo)
                    data["extraction_method"] = "http_directo_tabla"
                    _log(f"[OK] Datos encontrados via HTTP directo (tabla) desde {url}")
                    return data
                except Exception:
                    pass

                try:
                    data = patient_from_text(plain, doc, str(url), tipo)
                    data["extraction_method"] = "http_directo_texto"
                    _log(f"[OK] Datos encontrados via HTTP directo (texto) desde {url}")
                    return data
                except Exception as e:
                    last_error = str(e)
            except HTTPError as exc:
                last_error = f"HTTP {exc.code} en {url}: {exc.reason}"
                _log(f"[ERROR] {last_error}")
            except URLError as exc:
                last_error = f"No se pudo conectar con {url}. Detalle: {exc.reason}"
                _log(f"[ERROR] {last_error}")
            except Exception as exc:
                last_error = str(exc)
                _log(f"[ERROR] {last_error}")

    raise RuntimeError(last_error or "No se encontraron datos en la intranet.")


# ────────────────────────────────────────────────────────────────
# buscar_documento — IDÉNTICO AL ORIGINAL (flujo completo)
# ────────────────────────────────────────────────────────────────

def buscar_documento(documento: str, tipo_doc_hint: str = "", log: Callable = print) -> Dict[str, Any]:
    """
    Punto de entrada principal — IDÉNTICO al original intranet_autofill_api.py.

    Flujo:
      1. Si Playwright disponible: automatiza Chrome/Edge/Chromium (igual que .bat)
         - Llena formulario, parsea tablas con rowspan/colspan, abre Ver Histórico,
           consulta Servidor 2 para fecha si no la encontró en el principal.
      2. Fallback: HTTP directo sin dependencias (Win7 compatible).
    """
    if PLAYWRIGHT_AVAILABLE:
        try:
            return _playwright_buscar_documento(documento, tipo_doc_hint, log)
        except Exception as exc:
            log(f"[AVISO] Playwright falló ({exc}), usando HTTP directo como respaldo...")

    return _http_buscar(documento, tipo_doc_hint, log)


def _playwright_buscar_documento(documento: str, tipo_doc_hint: str, log: Callable) -> Dict[str, Any]:
    """Motor Playwright completo, idéntico al original buscar_documento."""
    documento, detected_type = parse_document_input(documento, tipo_doc_hint)
    if not documento:
        raise ValueError("Debe digitar un número de documento válido.")

    cfg = load_intranet_config()
    url = cfg["url"]
    timeout_ms = int(cfg["timeout_ms"])
    viewport = cfg["viewport"]
    selectors = cfg["selectors"]
    headless = bool(cfg["headless"])

    ok_host, host_msg = check_host(url)
    log(f"[INFO] {host_msg}")
    if not ok_host:
        log("[AVISO] Continuaré. Si no carga, revise VPN/red interna.")

    with sync_playwright() as p:
        browser = launch_browser_with_fallback(p, headless, log)
        context = browser.new_context(viewport=viewport)
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        try:
            log(f"[INFO] Abriendo página: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            alt = cfg.get("url_alternativa")
            log(f"[AVISO] Falló URL principal. Probando alternativa: {alt}")
            page.goto(alt, wait_until="domcontentloaded", timeout=timeout_ms)

        if os.getenv("INTRANET_DEBUG", "0") in {"1", "true", "si", "sí"}:
            save_debug_file(f"antes_{documento}_{now_id()}.html", page.content())

        input_cedula = first_visible(page, selectors["input_cedula"], "campo CÉDULA / SERIE", log)
        input_cedula.click()
        input_cedula.fill(documento)

        click_or_press_enter(page, input_cedula, selectors, timeout_ms, log)

        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        html = page.content()
        final_url = page.url
        try:
            visible_text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            visible_text = extract_visible_text_from_html(html)

        if os.getenv("INTRANET_DEBUG", "0") in {"1", "true", "si", "sí"}:
            save_debug_file(f"despues_{documento}_{now_id()}.html", html)
            save_debug_file(f"texto_{documento}_{now_id()}.txt", visible_text)

        historico_info: Dict[str, Any] = {}
        try:
            historico_info = extraer_ultima_atencion_desde_historico(page, context, cfg, documento, log)
            if historico_info.get("success"):
                log(f"[OK] Fecha última atención desde Ver Histórico: {historico_info.get('ult_consulta')}")
            else:
                log(f"[AVISO] Ver Histórico no entregó fecha: {historico_info.get('message')}")
        except Exception as hist_exc:
            historico_info = {"success": False, "message": str(hist_exc)}
            log(f"[AVISO] No se pudo extraer fecha desde Ver Histórico. Detalle: {hist_exc}")

        browser.close()

    tables = extract_tables_from_html(html)
    try:
        data = patient_from_tables(tables, documento, final_url, detected_type)
        data["extraction_method"] = "tabla"
    except Exception as table_exc:
        log(f"[AVISO] Extracción por tabla falló. Intentando texto visible. Detalle: {table_exc}")
        data = patient_from_text(visible_text, documento, final_url, detected_type)
        data["extraction_method"] = "texto_visible"
    data["tipo_doc"] = normalize_doc_type(data.get("tipo_doc")) or detected_type
    data["tables_detected"] = len(tables)

    # Servidor 2 para fecha si el principal no la encontró
    historico_servidor2_info: Dict[str, Any] = {}
    fecha_desde_servidor2 = False
    if not (historico_info.get("success") and historico_info.get("ult_consulta")):
        secondary_url = clean_text(cfg.get("url_servidor2_fecha"))
        if secondary_url and secondary_url not in {clean_text(url), clean_text(cfg.get("url_alternativa"))}:
            try:
                historico_servidor2_info = buscar_fecha_historico_en_servidor_url(documento, detected_type, cfg, secondary_url, log)
                if historico_servidor2_info.get("success") and historico_servidor2_info.get("ult_consulta"):
                    log(f"[OK] Fecha última atención desde servidor 2: {historico_servidor2_info.get('ult_consulta')}")
                    historico_info = historico_servidor2_info
                    fecha_desde_servidor2 = True
                else:
                    log(f"[AVISO] Servidor 2 no entregó fecha: {historico_servidor2_info.get('message')}")
            except Exception as sec_exc:
                historico_servidor2_info = {"success": False, "message": str(sec_exc)}
                log(f"[AVISO] Error consultando servidor 2 para fecha: {sec_exc}")

    if historico_info.get("success") and historico_info.get("ult_consulta"):
        data["ult_consulta_original_tabla_principal"] = data.get("ult_consulta_original") or ""
        data["ult_consulta_tabla_principal"] = data.get("ult_consulta") or ""
        data["ult_consulta_original"] = historico_info.get("ult_consulta_original") or historico_info.get("ult_consulta")
        data["ult_consulta"] = historico_info.get("ult_consulta")
        data["ult_consulta_source"] = "ver_historico"
        data["historico_url"] = historico_info.get("historico_url")
        data["historico_selector"] = historico_info.get("historico_selector")
        data["historico_fechas_detectadas"] = historico_info.get("historico_fechas_detectadas", [])
    else:
        tabla_original = data.get("ult_consulta_original") or ""
        tabla_fecha = normalize_date(data.get("ult_consulta") or tabla_original)
        data["ult_consulta_original_tabla_principal"] = tabla_original
        data["ult_consulta_tabla_principal"] = tabla_fecha
        data["ult_consulta_original"] = tabla_original if tabla_fecha else ""
        data["ult_consulta"] = tabla_fecha
        data["ult_consulta_source"] = "tabla_resultados" if tabla_fecha else "sin_fecha_historico"
        data["historico_message"] = historico_info.get("message") if historico_info else "No se intentó Ver Histórico"
        if historico_servidor2_info:
            data["historico_servidor2_message"] = historico_servidor2_info.get("message", "")
            data["historico_servidor2_url"] = historico_servidor2_info.get("historico_servidor", clean_text(cfg.get("url_servidor2_fecha")))

    data["ultima_entidad"] = clean_text(data.get("contrato") or data.get("ultima_entidad") or last_nonempty(data.get("contratos_detectados", []))).upper()
    data["entidad"] = data["ultima_entidad"]
    data["estado_vigente"] = clean_text(data.get("estado") or data.get("estado_vigente") or last_nonempty(data.get("estados_detectados", []))).upper()
    data["fecha_ultima_atencion"] = clean_text(data.get("ult_consulta"))
    expected_fields = ["tipo_doc", "cedula", "nombre", "contrato", "estado", "ult_consulta"]
    data["campos_extraidos"] = [key for key in expected_fields if clean_text(data.get(key))]
    data["campos_faltantes"] = [key for key in expected_fields if not clean_text(data.get(key))]
    data["extraccion_completa"] = not data["campos_faltantes"]

    if fecha_desde_servidor2:
        data["sistema_origen"] = etiqueta_sistema_origen(True)
        data["origen_datos_url"] = clean_text(historico_info.get("historico_servidor") or cfg.get("url_servidor2_fecha"))
    else:
        data["sistema_origen"] = etiqueta_sistema_origen(False)
        data["origen_datos_url"] = clean_text(final_url or url)
    data["observaciones_origen"] = data["sistema_origen"]
    data["fecha_desde_servidor2"] = fecha_desde_servidor2
    data["extracted_at"] = datetime.now().isoformat(timespec="seconds")
    return data


# ────────────────────────────────────────────────────────────────
# Sistema de trabajos en segundo plano
# ────────────────────────────────────────────────────────────────

def cleanup_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with JOBS_LOCK:
        for job_id in list(JOBS.keys()):
            if JOBS[job_id].get("updated_at", 0) < cutoff:
                JOBS.pop(job_id, None)


def set_job(job_id: str, **updates: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.setdefault(job_id, {})
        job.update(updates)
        job["updated_at"] = time.time()


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def run_lookup_job(job_id: str, documento: str, tipo_doc_hint: str) -> None:
    set_job(job_id, status="running", message="Buscando en Intraweb")
    try:
        data = buscar_documento(documento, tipo_doc_hint=tipo_doc_hint,
                                log=lambda m: print(f"[JOB {job_id}] {m}", flush=True))
        set_job(job_id, status="done", success=True, data=data, message="Datos extraídos correctamente")
    except Exception as exc:
        error_dir = app_dir() / "debug_intranet"
        error_dir.mkdir(parents=True, exist_ok=True)
        err_path = error_dir / f"error_job_{only_digits(documento)}_{now_id()}.txt"
        err_path.write_text(traceback.format_exc(), encoding="utf-8", errors="replace")
        set_job(job_id, status="error", success=False, message=str(exc), debug_error=str(err_path))


def create_lookup_job(documento: str, tipo_doc_hint: str = "") -> str:
    cleanup_jobs()
    doc, doc_type = parse_document_input(documento, tipo_doc_hint)
    if not doc:
        raise ValueError("Debe digitar un número de documento válido.")
    job_id = uuid.uuid4().hex[:12]
    set_job(job_id, status="pending", success=None, documento=doc, tipo_doc_hint=doc_type, message="Tarea creada")
    JOB_EXECUTOR.submit(run_lookup_job, job_id, doc, doc_type)
    return job_id


# ────────────────────────────────────────────────────────────────
# Estado de dependencias para /health
# ────────────────────────────────────────────────────────────────

def dependency_status() -> Dict[str, Any]:
    mode = "HTTP Directo (Sin dependencias, Win7+)"
    if PLAYWRIGHT_AVAILABLE:
        mode = "Playwright Automático (Chrome/Edge/Chromium) + HTTP Fallback"
    elif _BS4_OK:
        mode = "BeautifulSoup4 + HTTP Directo"
    return {
        "available": True,
        "mode": mode,
        "playwright": _PLAYWRIGHT_OK,
        "beautifulsoup4": _BS4_OK,
        "version": APP_VERSION,
        "config_url": str(CONFIG_PATH),
    }
