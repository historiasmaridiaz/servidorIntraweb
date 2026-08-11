# Servidor Web & API Historias Clínicas (Proinsalud) v5.4.0

[![Python CI Test Suite](https://github.com/TU-USUARIO/servidor-historias-clinicas/actions/workflows/test.yml/badge.svg)](https://github.com/TU-USUARIO/servidor-historias-clinicas/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)

Aplicativo y Servidor Web unificado con API RESTful e Interfaz Dashboard SPA moderna para la consulta de Historias Clínicas en la red Proinsalud. Listo para subir a **GitHub** y desplegar en la nube (Render, Railway, Docker, Vercel o GitHub Pages) **sin necesidad de scripts `.bat` locales**.

---

## 🚀 Inicio Rápido Local (Sin `.bat`)

Puedes ejecutar el servidor en cualquier sistema operativo (Windows, Linux, macOS) desde la terminal:

```bash
python server.py
```

El servidor detectará la ejecución y abrirá automáticamente en tu navegador `http://127.0.0.1:8765/`.

### Opciones de línea de comandos:
- `python server.py --host 0.0.0.0 --port 8080 --no-browser` : Para servidores y la nube.
- `python server.py --self-test` : Diagnóstico automatizado de funcionamiento.

---

## 🛠️ Cómo Subir este Proyecto a GitHub

1. Abre tu terminal en la carpeta del proyecto:
   ```bash
   git init
   git add .
   git commit -m "feat: servidor web e interfaz de Historias Clinicas v5.4.0"
   ```

2. Crea un nuevo repositorio en tu cuenta de GitHub (ejemplo: `servidor-historias-clinicas`) y conéctalo:
   ```bash
   git remote add origin https://github.com/TU-USUARIO/servidor-historias-clinicas.git
   git branch -M main
   git push -u origin main
   ```

---

## ☁️ Opciones de Despliegue en la Nube (1-Click)

### 1. Render.com
Este repositorio incluye `render.yaml` y `Procfile`.
1. Conecta tu repositorio de GitHub a Render.
2. Selecciona **Web Service**. Render detectará automáticamente Python y desplegará la aplicación usando el puerto dinámico `$PORT`.

### 2. Docker / Cloud Run / Railway
Construye y ejecuta la imagen usando el `Dockerfile` incluido:
```bash
docker build -t servidor-historias-clinicas .
docker run -p 8080:8080 servidor-historias-clinicas
```

### 3. Vercel
Usa la configuración `vercel.json` incluida ejecutando:
```bash
vercel --prod
```

---

## 📡 Endpoints de la API REST

- `GET /` : Dashboard Web SPA Interactivo.
- `GET /health` : Estado de salud de la API, versión de Python y motor de intranet.
- `GET /buscar?documento=123456&tipo=CC` : Consulta sincrónica.
- `GET /buscar_async?documento=123456&tipo=CC` : Inicia trabajo asincrónico (retorna `job_id`).
- `GET /resultado?job_id=...` : Consulta de estado (`pending`, `done`, `not_found`, `error`).
- `GET /config` | `POST /config` : Obtener y actualizar parámetros de servidor.
- `GET /shutdown` : Detener el servidor.

---

## 🧪 Pruebas Automatizadas

```bash
python test_servidor.py
```
*(GitHub Actions ejecutará automáticamente las pruebas unitarias en cada `push` o `pull_request`).*
