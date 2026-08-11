# Historias Clínicas Proinsalud — Servidor Intraweb

Servidor web local + API que automatiza la búsqueda de pacientes en la intranet de Proinsalud.

## ¿Qué hace?

- Abre Chrome/Edge/Chromium automáticamente y llena el formulario de la intranet
- Extrae: nombre, cédula, EPS/contrato, estado, fecha última atención, médico
- Sirve un dashboard web en `http://127.0.0.1:8765`
- Motor fiel al original v4.16 con soporte de `rowspan`/`colspan`

## Ejecutar localmente (con Python instalado)

```bash
pip install -r requirements.txt
playwright install chromium
python server.py
```

## Ejecutar sin Python (EXE)

Descargue el EXE desde la sección **Releases** de este repositorio.  
En Windows 7, ejecute primero `INSTALAR_REQUISITOS.bat`.

## Endpoints API

| Endpoint | Descripción |
|---|---|
| `GET /health` | Estado del servidor y dependencias |
| `GET /buscar?documento=12345&tipo=CC` | Búsqueda sincrónica |
| `GET /buscar_async?documento=12345&tipo=CC` | Búsqueda en segundo plano |
| `GET /resultado?job_id=...` | Resultado de búsqueda async |
| `GET /config` | Configuración activa |

## Configuración

Edite `config_servidor.json`:

```json
{
  "search_urls": [
    "http://intraweb.local/intraweb/intranet/depura_historia/busca_paciente.php"
  ],
  "url_servidor2_fecha": "http://192.168.3.90/intraweb/intranet/depura_historia/busca_paciente.php",
  "timeout_ms": 45000,
  "headless": false
}
```

## Despliegue en Render / Nube

> ⚠️ El servidor debe ejecutarse **dentro de la red de la clínica** para acceder a `intraweb.local`.  
> En la nube solo funciona la interfaz web; la búsqueda requiere red local.

```bash
# Render arranca con:
python server.py --host 0.0.0.0 --port $PORT --no-browser
```

## Versión

`5.4.0-extractor-fiel-v416-entidad-fecha`
