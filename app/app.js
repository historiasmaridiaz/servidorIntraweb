/**
 * Lógica del Dashboard Web SPA para Historias Clínicas (Proinsalud).
 */

document.addEventListener('DOMContentLoaded', () => {
  // Inicializar Iconos Lucide
  if (window.lucide) {
    lucide.createIcons();
  }

  const API_ORIGIN = window.location.origin;

  // Elementos DOM
  const serverStatusBadge = document.getElementById('serverStatusBadge');
  const intranetStatusBadge = document.getElementById('intranetStatusBadge');
  const appVersionTag = document.getElementById('appVersionTag');

  const searchForm = document.getElementById('searchForm');
  const tipoDocSelect = document.getElementById('tipoDocSelect');
  const documentoInput = document.getElementById('documentoInput');
  const btnBuscar = document.getElementById('btnBuscar');
  const btnLimpiar = document.getElementById('btnLimpiar');

  const progressContainer = document.getElementById('progressContainer');
  const progressStatusText = document.getElementById('progressStatusText');

  const resultPanel = document.getElementById('resultPanel');
  const patientNombre = document.getElementById('patientNombre');
  const patientDocBadge = document.getElementById('patientDocBadge');
  const patientEstadoBadge = document.getElementById('patientEstadoBadge');
  const patientContrato = document.getElementById('patientContrato');
  const patientUltConsulta = document.getElementById('patientUltConsulta');
  const patientMedico = document.getElementById('patientMedico');
  const patientEdad = document.getElementById('patientEdad');
  const patientSourceUrl = document.getElementById('patientSourceUrl');
  const patientMethod = document.getElementById('patientMethod');
  const btnCopyData = document.getElementById('btnCopyData');

  const historyTableBody = document.getElementById('historyTableBody');
  const btnClearHistory = document.getElementById('btnClearHistory');

  const configModal = document.getElementById('configModal');
  const btnOpenConfig = document.getElementById('btnOpenConfig');
  const btnCloseConfig = document.getElementById('btnCloseConfig');
  const btnCancelConfig = document.getElementById('btnCancelConfig');
  const configForm = document.getElementById('configForm');
  const cfgHost = document.getElementById('cfgHost');
  const cfgPort = document.getElementById('cfgPort');
  const cfgWebAppUrl = document.getElementById('cfgWebAppUrl');
  const cfgDriveUrl = document.getElementById('cfgDriveUrl');

  let currentPatientData = null;
  let historyList = JSON.parse(localStorage.getItem('hclinicas_history') || '[]');

  // --- Health Check Inicial ---
  async function checkHealth() {
    try {
      const res = await fetch(`${API_ORIGIN}/health`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      if (data.success) {
        serverStatusBadge.innerHTML = `
          <span class="status-dot online"></span>
          <span class="status-label">Servidor Local: Activo (${data.python_bits} bits)</span>
        `;
        if (data.version) appVersionTag.textContent = `v${data.version}`;

        const motor = data.motor_intranet || {};
        if (motor.available) {
          intranetStatusBadge.innerHTML = `
            <span class="status-dot online"></span>
            <span class="status-label">Intranet: ${motor.mode || 'Conectada'}</span>
          `;
        } else {
          intranetStatusBadge.innerHTML = `
            <span class="status-dot warning"></span>
            <span class="status-label">Intranet: Raspado HTTP Respaldo</span>
          `;
        }
      }
    } catch (err) {
      serverStatusBadge.innerHTML = `
        <span class="status-dot offline"></span>
        <span class="status-label">Servidor Desconectado</span>
      `;
      intranetStatusBadge.innerHTML = `
        <span class="status-dot offline"></span>
        <span class="status-label">Sin conexión</span>
      `;
    }
  }

  // --- Búsqueda Asincrónica ---
  searchForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const doc = documentoInput.value.trim();
    const tipo = tipoDocSelect.value;

    if (!doc) return;

    // UI State Loading
    btnBuscar.disabled = true;
    progressContainer.classList.remove('hidden');
    resultPanel.classList.add('hidden');
    progressStatusText.innerHTML = `<i data-lucide="loader" class="spin"></i> Iniciando consulta asincrónica en intranet...`;
    if (window.lucide) lucide.createIcons();

    try {
      // 1. Iniciar trabajo asincrónico
      const initRes = await fetch(`${API_ORIGIN}/buscar_async?documento=${encodeURIComponent(doc)}&tipo=${encodeURIComponent(tipo)}`);
      const initData = await initRes.json();

      if (!initData.success || !initData.job_id) {
        throw new Error(initData.message || 'No se pudo iniciar la consulta.');
      }

      const jobId = initData.job_id;
      let attempts = 0;
      const maxAttempts = 60; // Max 30 segundos polling

      // 2. Polling /resultado
      const pollInterval = setInterval(async () => {
        attempts++;
        progressStatusText.innerHTML = `<i data-lucide="loader" class="spin"></i> Consultando intranet local (${attempts}s)...`;
        if (window.lucide) lucide.createIcons();

        try {
          const res = await fetch(`${API_ORIGIN}/resultado?job_id=${encodeURIComponent(jobId)}`);
          const pollData = await res.json();

          if (pollData.status === 'done') {
            clearInterval(pollInterval);
            displayPatientData(pollData.data);
            addToHistory(pollData.data);
            resetSearchState();
          } else if (pollData.status === 'not_found') {
            clearInterval(pollInterval);
            alert(`No se encontraron registros para el documento ${doc} (${tipo}).`);
            resetSearchState();
          } else if (pollData.status === 'error') {
            clearInterval(pollInterval);
            alert(`Error en la consulta: ${pollData.message || 'Desconocido'}`);
            resetSearchState();
          } else if (attempts >= maxAttempts) {
            clearInterval(pollInterval);
            alert('La consulta excedió el tiempo máximo de espera.');
            resetSearchState();
          }
        } catch (err) {
          clearInterval(pollInterval);
          alert(`Error de red durante la consulta: ${err.message}`);
          resetSearchState();
        }
      }, 1000);

    } catch (err) {
      alert(`Error al iniciar búsqueda: ${err.message}`);
      resetSearchState();
    }
  });

  function resetSearchState() {
    btnBuscar.disabled = false;
    progressContainer.classList.add('hidden');
  }

  // --- Mostrar Datos del Paciente ---
  function displayPatientData(data) {
    currentPatientData = data;

    patientNombre.textContent = data.nombre || 'PACIENTE SIN NOMBRE';
    patientDocBadge.textContent = `${data.tipo_doc || 'DOC'} ${data.cedula || ''}`;
    patientEstadoBadge.textContent = data.estado || 'NO ESPECIFICADO';
    
    // Cambiar estilo de badge según estado
    if ((data.estado || '').toUpperCase().includes('ACTIVO')) {
      patientEstadoBadge.className = 'badge badge-status';
    } else {
      patientEstadoBadge.className = 'badge badge-doc';
    }

    patientContrato.textContent = data.contrato || 'Sin contrato reportado';
    patientUltConsulta.textContent = data.ult_consulta || data.ult_consulta_original || 'Sin registro';
    patientMedico.textContent = data.medico || 'No especificado';
    patientEdad.textContent = data.edad ? `${data.edad} años` : '--';

    patientSourceUrl.textContent = data.source_url || 'Intranet Local';
    patientMethod.textContent = data.extraction_method || 'HTTP Directo';

    resultPanel.classList.remove('hidden');
    resultPanel.scrollIntoView({ behavior: 'smooth' });
  }

  // --- Limpiar Formulario ---
  btnLimpiar.addEventListener('click', () => {
    documentoInput.value = '';
    resultPanel.classList.add('hidden');
    currentPatientData = null;
    documentoInput.focus();
  });

  // --- Copiar Datos ---
  btnCopyData.addEventListener('click', () => {
    if (!currentPatientData) return;
    const text = `PACIENTE: ${currentPatientData.nombre}\nDOC: ${currentPatientData.tipo_doc || ''} ${currentPatientData.cedula}\nCONTRATO: ${currentPatientData.contrato || ''}\nESTADO: ${currentPatientData.estado || ''}\nULT. ATENCION: ${currentPatientData.ult_consulta || ''}`;
    navigator.clipboard.writeText(text).then(() => {
      alert('¡Datos copiados al portapapeles!');
    });
  });

  // --- Historial de Consultas ---
  function renderHistory() {
    if (historyList.length === 0) {
      historyTableBody.innerHTML = `<tr class="empty-row"><td colspan="7">No hay búsquedas realizadas en esta sesión.</td></tr>`;
      return;
    }

    historyTableBody.innerHTML = historyList.map((item, idx) => `
      <tr>
        <td>${item.time}</td>
        <td><strong>${item.tipo_doc || ''} ${item.cedula}</strong></td>
        <td>${item.nombre || 'N/A'}</td>
        <td>${item.contrato || 'N/A'}</td>
        <td><span class="badge ${item.estado?.includes('ACTIVO') ? 'badge-status' : 'badge-doc'}">${item.estado || 'N/A'}</span></td>
        <td>${item.ult_consulta || 'N/A'}</td>
        <td>
          <button class="btn btn-sm btn-outline btn-view-hist" data-idx="${idx}">
            <i data-lucide="eye"></i> Ver
          </button>
        </td>
      </tr>
    `).join('');

    if (window.lucide) lucide.createIcons();

    // Event listener para el botón Ver
    document.querySelectorAll('.btn-view-hist').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const idx = e.currentTarget.getAttribute('data-idx');
        if (historyList[idx]) {
          displayPatientData(historyList[idx]);
        }
      });
    });
  }

  function addToHistory(data) {
    const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const item = { ...data, time: timeStr };
    
    // Evitar duplicados consecutivos
    if (historyList.length > 0 && historyList[0].cedula === item.cedula) {
      historyList[0] = item;
    } else {
      historyList.unshift(item);
    }

    if (historyList.length > 15) historyList.pop();
    localStorage.setItem('hclinicas_history', JSON.stringify(historyList));
    renderHistory();
  }

  btnClearHistory.addEventListener('click', () => {
    historyList = [];
    localStorage.removeItem('hclinicas_history');
    renderHistory();
  });

  // --- Modal de Configuración ---
  btnOpenConfig.addEventListener('click', async () => {
    try {
      const res = await fetch(`${API_ORIGIN}/config`);
      const data = await res.json();
      if (data.success) {
        cfgHost.value = data.config.host || '127.0.0.1';
        cfgPort.value = data.config.port || 8765;
        cfgWebAppUrl.value = data.app_config.web_app_url || '';
        cfgDriveUrl.value = data.app_config.drive_url || '';
        configModal.classList.remove('hidden');
      }
    } catch (err) {
      alert('No se pudo cargar la configuración actual.');
    }
  });

  function closeConfig() {
    configModal.classList.add('hidden');
  }

  btnCloseConfig.addEventListener('click', closeConfig);
  btnCancelConfig.addEventListener('click', closeConfig);

  configForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const payload = {
      server_config: {
        host: cfgHost.value.trim(),
        port: parseInt(cfgPort.value, 10),
      },
      app_config: {
        web_app_url: cfgWebAppUrl.value.trim(),
        drive_url: cfgDriveUrl.value.trim(),
      }
    };

    try {
      const res = await fetch(`${API_ORIGIN}/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (data.success) {
        alert('Configuración guardada correctamente.');
        closeConfig();
        checkHealth();
      } else {
        alert(`Error al guardar: ${data.message}`);
      }
    } catch (err) {
      alert(`Error al conectar con el servidor: ${err.message}`);
    }
  });

  // Init
  checkHealth();
  renderHistory();
});
