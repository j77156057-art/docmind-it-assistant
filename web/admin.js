(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const state = { me: null, documents: [], selected: null, acl: null, artifacts: [], audit: [], modelConfig: null, view: 'documents' };
  const viewMeta = {
    documents: ['文档库', '版本、发布状态与访问权限'],
    artifacts: ['文档生成', '创建并下载办公文档'],
    audit: ['审计记录', '管理操作与请求追踪'],
    system: ['系统状态', '服务依赖与身份上下文'],
  };
  const statusText = { indexed: '已发布', pending: '处理中', failed: '失败', superseded: '已替代' };
  const actionText = { document_acl_replace: '更新权限', document_import: '导入文档', document_source_view: '查看原文件', document_source_download: '下载原文件', artifact_create: '生成文件', model_config_update: '切换模型' };
  const typeText = { user: '用户', group: '用户组', role: '角色' };
  const artifactExtensions = { docx: 'docx', pdf: 'pdf', pptx: 'pptx', xlsx: 'xlsx' };
  const artifactPlaceholders = {
    docx: '输入正文，空行分隔段落',
    pdf: '输入正文，空行分隔段落',
    pptx: '每个段落生成一页，第一行作为页标题',
    xlsx: '第一行作为表头，使用逗号或 Tab 分隔列',
  };

  function icon(id) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${id}`);
    svg.appendChild(use);
    return svg;
  }

  async function api(url, options = {}) {
    const response = await fetch(url, { credentials: 'same-origin', ...options });
    const payload = await response.json().catch(() => ({ ok: false, error: '响应格式无效' }));
    if (!response.ok || payload.ok === false) {
      const error = new Error(payload.error || '请求失败');
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function toast(message, kind = 'success') {
    const item = document.createElement('div');
    item.className = `toast ${kind}`;
    item.textContent = message;
    $('toasts').appendChild(item);
    window.setTimeout(() => item.remove(), 3600);
  }

  function canAdmin() {
    return state.me?.roles?.includes('admin');
  }

  function shortId(value, length = 12) {
    if (!value) return '-';
    return value.length > length ? `${value.slice(0, length)}...` : value;
  }

  function setIdentity() {
    const name = state.me.display_name || '当前管理员';
    $('displayName').textContent = name;
    $('subjectId').textContent = shortId(state.me.subject_id, 14);
    $('avatar').textContent = name.slice(0, 2).toUpperCase();
    const highest = canAdmin() ? 'ADMIN' : 'AUDITOR';
    $('roleBadge').textContent = highest;
    document.body.classList.toggle('readonly', !canAdmin());
    $('systemSubject').textContent = state.me.subject_id;
    $('systemRoles').textContent = state.me.roles.join(', ') || '-';
    $('systemGroups').textContent = state.me.groups.join(', ') || '-';
  }

  function groupDocuments(items) {
    const grouped = new Map();
    for (const item of items) {
      if (!grouped.has(item.document_id)) grouped.set(item.document_id, { ...item, versions: [] });
      grouped.get(item.document_id).versions.push(item);
    }
    return [...grouped.values()].map(item => {
      item.versions.sort((a, b) => b.version - a.version);
      const current = item.versions.find(version => version.status === 'indexed') || item.versions[0];
      return { ...item, ...current, versions: item.versions };
    });
  }

  function filteredDocuments() {
    const query = $('documentSearch').value.trim().toLowerCase();
    const scope = $('scopeFilter').value;
    return state.documents.filter(item => {
      const matchesText = !query || `${item.title} ${item.source_key}`.toLowerCase().includes(query);
      const matchesScope = scope === 'all' || item.access_scope === scope;
      return matchesText && matchesScope;
    });
  }

  function statusBadge(status) {
    const span = document.createElement('span');
    span.className = `status status-${status}`;
    span.textContent = statusText[status] || status;
    return span;
  }

  function scopeBadge(scope) {
    const span = document.createElement('span');
    span.className = `scope scope-${scope}`;
    span.textContent = scope === 'public' ? '公开' : '受限';
    return span;
  }

  function renderDocuments() {
    const items = filteredDocuments();
    const body = $('documentRows');
    body.replaceChildren();
    $('documentEmpty').hidden = items.length > 0;
    for (const item of items) {
      const row = document.createElement('tr');
      row.dataset.id = item.document_id;
      row.classList.toggle('selected', state.selected?.document_id === item.document_id);
      row.tabIndex = 0;

      const documentCell = document.createElement('td');
      const documentName = document.createElement('div');
      documentName.className = 'document-name';
      const fileIcon = document.createElement('span');
      fileIcon.className = 'file-icon';
      fileIcon.appendChild(icon('file'));
      const copy = document.createElement('span');
      copy.className = 'document-copy';
      const title = document.createElement('strong');
      title.textContent = item.title;
      const source = document.createElement('small');
      source.textContent = item.source_key;
      copy.append(title, source);
      documentName.append(fileIcon, copy);
      documentCell.appendChild(documentName);

      const versionCell = document.createElement('td');
      versionCell.textContent = `v${item.version} / ${item.versions.length}`;
      const statusCell = document.createElement('td');
      statusCell.appendChild(statusBadge(item.status));
      const scopeCell = document.createElement('td');
      scopeCell.appendChild(scopeBadge(item.access_scope));
      const chunkCell = document.createElement('td');
      chunkCell.textContent = Number(item.chunk_count || 0).toLocaleString();
      const actionCell = document.createElement('td');
      const action = document.createElement('button');
      action.className = 'icon-button row-action';
      action.type = 'button';
      action.title = '查看权限';
      action.setAttribute('aria-label', '查看权限');
      action.appendChild(icon('chevron'));
      actionCell.appendChild(action);
      row.append(documentCell, versionCell, statusCell, scopeCell, chunkCell, actionCell);
      row.addEventListener('click', () => selectDocument(item));
      row.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectDocument(item); }
      });
      body.appendChild(row);
    }
  }

  function updateMetrics() {
    $('metricDocuments').textContent = state.documents.length.toLocaleString();
    $('metricIndexed').textContent = state.documents.filter(item => item.status === 'indexed').length.toLocaleString();
    $('metricRestricted').textContent = state.documents.filter(item => item.access_scope === 'restricted').length.toLocaleString();
    $('metricChunks').textContent = state.documents.reduce((sum, item) => sum + Number(item.chunk_count || 0), 0).toLocaleString();
  }

  async function loadDocuments() {
    const payload = await api('/api/admin/documents');
    state.documents = groupDocuments(payload.items || []);
    updateMetrics();
    renderDocuments();
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function renderArtifacts() {
    const body = $('artifactRows');
    body.replaceChildren();
    $('artifactCount').textContent = `${state.artifacts.length} 个`;
    $('artifactEmpty').hidden = state.artifacts.length > 0;
    for (const item of state.artifacts) {
      const row = document.createElement('tr');
      const nameCell = document.createElement('td');
      const name = document.createElement('strong');
      name.className = 'artifact-name';
      name.textContent = item.filename;
      nameCell.appendChild(name);
      const formatCell = document.createElement('td');
      const format = document.createElement('span');
      format.className = `format-badge format-${item.format}`;
      format.textContent = item.format.toUpperCase();
      formatCell.appendChild(format);
      const sizeCell = document.createElement('td');
      sizeCell.textContent = formatBytes(item.bytes);
      const timeCell = document.createElement('td');
      timeCell.textContent = new Date(item.created_at).toLocaleString('zh-CN', { hour12: false });
      const actionCell = document.createElement('td');
      const download = document.createElement('a');
      download.className = 'icon-button row-action';
      download.href = item.download_url;
      download.title = '下载';
      download.setAttribute('aria-label', `下载 ${item.filename}`);
      download.appendChild(icon('download'));
      actionCell.appendChild(download);
      row.append(nameCell, formatCell, sizeCell, timeCell, actionCell);
      body.appendChild(row);
    }
  }

  async function loadArtifacts(silent = false) {
    try {
      const payload = await api('/api/admin/artifacts');
      state.artifacts = payload.items || [];
      renderArtifacts();
    } catch (error) {
      if (!silent) toast(error.message, 'error');
    }
  }

  function selectedArtifactFormat() {
    return new FormData($('artifactForm')).get('format') || 'docx';
  }

  function syncArtifactFormat() {
    const format = selectedArtifactFormat();
    const filename = $('artifactFilename');
    const stem = filename.value.trim().replace(/\.[a-z0-9]+$/i, '') || 'document';
    filename.value = `${stem}.${artifactExtensions[format]}`;
    $('artifactContent').placeholder = artifactPlaceholders[format];
  }

  function splitParagraphs(content) {
    return content.split(/\n\s*\n/).map(value => value.trim()).filter(Boolean);
  }

  function artifactPayload(form) {
    const data = new FormData(form);
    const format = data.get('format');
    const content = String(data.get('content') || '').trim();
    const payload = {
      format,
      filename: String(data.get('filename') || '').trim(),
      title: String(data.get('title') || '').trim(),
      subtitle: String(data.get('subtitle') || '').trim(),
    };
    if (format === 'docx' || format === 'pdf') {
      payload.sections = [{ paragraphs: splitParagraphs(content) }];
    } else if (format === 'pptx') {
      payload.slides = splitParagraphs(content).map((block, index) => {
        const lines = block.split('\n').map(value => value.trim()).filter(Boolean);
        return { title: lines.shift() || `第 ${index + 1} 页`, bullets: lines };
      });
    } else {
      const lines = content.split('\n').map(value => value.trim()).filter(Boolean);
      const delimiter = lines[0]?.includes('\t') ? '\t' : ',';
      const rows = lines.map(line => line.split(delimiter).map(value => value.trim()));
      payload.sheets = [{ name: payload.title || 'Data', headers: rows.shift() || [], rows }];
    }
    return payload;
  }

  async function submitArtifact(event) {
    event.preventDefault();
    if (!canAdmin()) return;
    const button = $('submitArtifact');
    button.disabled = true;
    button.querySelector('span').textContent = '正在生成';
    try {
      const result = await api('/api/admin/artifacts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(artifactPayload(event.currentTarget)),
      });
      toast(`${result.filename} 已生成`);
      $('artifactContent').value = '';
      await Promise.all([loadArtifacts(true), loadAudit(true)]);
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      button.disabled = false;
      button.querySelector('span').textContent = '生成文件';
    }
  }

  function addAclRow(entry = { principal_type: 'group', principal_id: '' }) {
    const row = document.createElement('div');
    row.className = 'acl-row';
    const type = document.createElement('select');
    type.setAttribute('aria-label', '主体类型');
    for (const value of ['user', 'group', 'role']) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = typeText[value];
      option.selected = entry.principal_type === value;
      type.appendChild(option);
    }
    const id = document.createElement('input');
    id.maxLength = 256;
    id.value = entry.principal_id;
    id.placeholder = entry.principal_type === 'user' ? '匿名主体 ID' : '名称';
    id.setAttribute('aria-label', '主体标识');
    type.addEventListener('change', () => { id.placeholder = type.value === 'user' ? '匿名主体 ID' : '名称'; });
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'icon-button admin-only';
    remove.title = '移除';
    remove.setAttribute('aria-label', '移除');
    remove.appendChild(icon('trash'));
    remove.addEventListener('click', () => { row.remove(); updateAclEmpty(); });
    if (!canAdmin()) { type.disabled = true; id.disabled = true; remove.disabled = true; }
    row.append(type, id, remove);
    $('aclList').appendChild(row);
    updateAclEmpty();
  }

  function updateAclEmpty() {
    $('aclEmpty').hidden = $('aclList').children.length > 0;
  }

  function renderAcl() {
    $('aclList').replaceChildren();
    for (const entry of state.acl.entries || []) addAclRow(entry);
    const scope = state.acl.access_scope || 'restricted';
    const scopeInput = document.querySelector(`#aclForm input[name="access_scope"][value="${scope}"]`);
    if (scopeInput) scopeInput.checked = true;
    for (const input of document.querySelectorAll('#aclForm input[name="access_scope"]')) input.disabled = !canAdmin();
    updateAclEmpty();
  }

  async function selectDocument(item) {
    state.selected = item;
    renderDocuments();
    $('inspector').hidden = false;
    $('documentGrid').classList.add('with-inspector');
    $('inspectorTitle').textContent = item.title;
    $('inspectorSource').textContent = item.source_key;
    $('inspectorClass').textContent = item.classification;
    $('inspectorVersion').textContent = `v${item.version} · ${item.versions.length} 个版本`;
    $('openSource').hidden = !item.source_available;
    $('downloadSource').hidden = !item.source_available;
    $('sourceUnavailable').hidden = item.source_available;
    if (item.source_available) {
      $('openSource').href = item.source_url;
      $('downloadSource').href = item.download_url;
      $('downloadSource').setAttribute('download', item.source_filename || 'document');
    } else {
      $('openSource').removeAttribute('href');
      $('downloadSource').removeAttribute('href');
    }
    try {
      state.acl = await api(`/api/admin/documents/${item.document_id}/acl`);
      renderAcl();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function closeInspector() {
    state.selected = null;
    state.acl = null;
    $('inspector').hidden = true;
    $('documentGrid').classList.remove('with-inspector');
    renderDocuments();
  }

  async function saveAcl(event) {
    event.preventDefault();
    if (!canAdmin() || !state.selected) return;
    const entries = [...$('aclList').children].map(row => ({
      principal_type: row.querySelector('select').value,
      principal_id: row.querySelector('input').value.trim(),
    })).filter(entry => entry.principal_id);
    const accessScope = new FormData(event.currentTarget).get('access_scope');
    const button = $('saveAcl');
    button.disabled = true;
    try {
      state.acl = await api(`/api/admin/documents/${state.selected.document_id}/acl`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ access_scope: accessScope, entries }),
      });
      toast('访问权限已保存');
      await Promise.all([loadDocuments(), loadAudit(true)]);
      const refreshed = state.documents.find(item => item.document_id === state.selected.document_id);
      if (refreshed) state.selected = refreshed;
      renderDocuments();
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      button.disabled = false;
    }
  }

  function renderAudit() {
    const filter = $('auditFilter').value;
    const items = state.audit.filter(item => filter === 'all' || item.result === filter);
    const body = $('auditRows');
    body.replaceChildren();
    $('auditCount').textContent = `${items.length} 条`;
    $('auditEmpty').hidden = items.length > 0;
    for (const item of items) {
      const row = document.createElement('tr');
      const values = [
        new Date(item.created_at).toLocaleString('zh-CN', { hour12: false }),
        actionText[item.action] || item.action,
        item.target_ref,
        shortId(item.actor_subject_id, 10),
        item.result === 'success' ? '成功' : '失败',
        shortId(item.request_id, 12),
      ];
      values.forEach((value, index) => {
        const cell = document.createElement('td');
        cell.textContent = value;
        if (index === 3) cell.className = 'actor';
        if (index === 4) cell.className = `result-${item.result}`;
        if (index === 5) cell.className = 'actor';
        row.appendChild(cell);
      });
      body.appendChild(row);
    }
  }

  async function loadAudit(silent = false) {
    try {
      const payload = await api('/api/admin/audit-events?limit=100');
      state.audit = payload.items || [];
      renderAudit();
    } catch (error) {
      if (!silent) toast(error.message, 'error');
    }
  }

  function renderHealth(payload) {
    $('overallStatus').textContent = payload.ok ? '正常' : '异常';
    $('overallStatus').className = `status-pill ${payload.ok ? 'ok' : 'error'}`;
    $('serviceDot').className = `service-dot ${payload.ok ? 'ok' : ''}`;
    const list = $('healthChecks');
    list.replaceChildren();
    for (const [name, check] of Object.entries(payload.checks || {})) {
      const row = document.createElement('div');
      row.className = 'check-row';
      const indicator = document.createElement('i');
      indicator.className = `check-indicator ${check.ok ? 'ok' : ''}`;
      const label = document.createElement('strong');
      label.textContent = name.replace('_', ' ');
      const reason = document.createElement('span');
      reason.textContent = check.ok ? '正常' : check.reason;
      row.append(indicator, label, reason);
      list.appendChild(row);
    }
  }

  async function loadHealth() {
    try {
      const response = await fetch('/health/ready', { credentials: 'same-origin' });
      const payload = await response.json();
      renderHealth(payload);
    } catch (_error) {
      renderHealth({ ok: false, checks: { service: { ok: false, reason: 'unavailable' } } });
    }
  }

  function selectedModelMode() {
    return new FormData($('modelForm')).get('mode') || 'knowledge';
  }

  function providersForMode(mode) {
    return (state.modelConfig?.providers || []).filter(item => item.mode === mode);
  }

  function syncModelProvider(preferred = '') {
    const providers = providersForMode(selectedModelMode());
    const select = $('modelProvider');
    const current = preferred || select.value;
    select.replaceChildren();
    for (const item of providers) {
      const option = document.createElement('option');
      option.value = item.key;
      option.textContent = item.label;
      option.dataset.defaultModel = item.default_model;
      option.dataset.contextWindow = item.context_window;
      option.dataset.credential = item.api_key_configured ? '已配置' : (item.mode === 'cloud' ? '未配置' : '无需密钥');
      option.dataset.baseUrlConfigured = item.base_url_configured ? 'true' : 'false';
      select.appendChild(option);
    }
    if (providers.some(item => item.key === current)) select.value = current;
    const selected = providers.find(item => item.key === select.value);
    if (selected && !$('modelName').value.trim()) $('modelName').value = selected.default_model;
    renderModelSelection();
  }

  function renderModelSelection() {
    const option = $('modelProvider').selectedOptions[0];
    if (!option) {
      $('modelContext').textContent = '-';
      $('modelCredential').textContent = '-';
      return;
    }
    const context = Number(option.dataset.contextWindow || 0);
    $('modelContext').textContent = context ? `${context.toLocaleString()} Token` : '不适用';
    $('modelCredential').textContent = option.dataset.baseUrlConfigured === 'false' ? '服务地址未配置' : option.dataset.credential;
  }

  function renderModelConfig(payload) {
    state.modelConfig = payload;
    const active = payload.active;
    const modeInput = document.querySelector(`#modelForm input[name="mode"][value="${active.mode}"]`);
    if (modeInput) modeInput.checked = true;
    $('modelName').value = active.model;
    syncModelProvider(active.provider);
    $('activeModel').textContent = `${active.provider} / ${active.model}`;
    $('modelStatus').textContent = active.ready ? '可用' : '未就绪';
    $('modelStatus').className = `status-pill ${active.ready ? 'ok' : 'error'}`;
    renderModelRuntime(payload.runtime || {});
  }

  function renderModelRuntime(runtime) {
    const ready = !!runtime.ready;
    $('runtimeIndicator').className = ready ? 'ok' : '';
    $('runtimeTitle').textContent = runtime.message || (ready ? '模型可用' : '模型未启动');
    const details = [];
    if (runtime.required === false) details.push(runtime.provider === 'builtin' ? '内置确定性路由' : '按请求连接云服务');
    if (runtime.service_reachable === true) details.push('服务在线');
    if (runtime.service_reachable === false) details.push('服务不可达');
    if (runtime.installed === true) details.push('模型已安装');
    if (runtime.installed === false && runtime.service_reachable) details.push('模型未安装');
    if (runtime.loaded === true) details.push('已驻留');
    if (runtime.loaded === false && runtime.installed) details.push('未驻留');
    if (Number(runtime.vram_gb || 0) > 0) details.push(`显存 ${Number(runtime.vram_gb).toFixed(2)} GB`);
    if (runtime.latency_ms) details.push(`验证 ${runtime.latency_ms} ms`);
    $('runtimeDetail').textContent = details.join(' · ') || `${runtime.provider || '-'} / ${runtime.model || '-'}`;
    $('modelStatus').textContent = ready ? (runtime.loaded ? '已启动' : '可用') : '未启动';
    $('modelStatus').className = `status-pill ${ready ? 'ok' : 'error'}`;
  }

  async function loadModelConfig(silent = false) {
    try {
      renderModelConfig(await api('/api/admin/model-config'));
    } catch (error) {
      $('modelStatus').textContent = '不可用';
      $('modelStatus').className = 'status-pill error';
      if (!silent) toast(error.message, 'error');
    }
  }

  async function saveModelConfig(event) {
    event.preventDefault();
    if (!canAdmin()) return;
    const button = $('saveModel');
    const data = new FormData(event.currentTarget);
    button.disabled = true;
    button.querySelector('span').textContent = '正在启动并验证';
    try {
      const result = await api('/api/admin/model-config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode: data.get('mode'),
          provider: data.get('provider'),
          model: String(data.get('model') || '').trim(),
        }),
      });
      const message = result.runtime?.verified
        ? `已启动并验证 ${result.active.provider} / ${result.active.model}`
        : `已切换到 ${result.active.provider} / ${result.active.model}`;
      toast(message);
      await Promise.all([loadModelConfig(true), loadHealth(), loadAudit(true)]);
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      button.disabled = false;
      button.querySelector('span').textContent = '应用配置';
    }
  }

  function setView(view) {
    state.view = view;
    document.querySelectorAll('.nav-item').forEach(button => button.classList.toggle('active', button.dataset.view === view));
    document.querySelectorAll('.view').forEach(section => section.classList.toggle('active', section.id === `view-${view}`));
    [$('pageTitle').textContent, $('pageSubtitle').textContent] = viewMeta[view];
    closeSidebar();
    if (view === 'audit') loadAudit();
    if (view === 'artifacts') loadArtifacts();
    if (view === 'system') Promise.all([loadHealth(), loadModelConfig()]);
  }

  function openSidebar() {
    $('sidebar').classList.add('open');
    $('sidebarScrim').classList.add('show');
  }

  function closeSidebar() {
    $('sidebar').classList.remove('open');
    $('sidebarScrim').classList.remove('show');
  }

  function openImport() {
    $('importForm').reset();
    $('fileLabel').textContent = '选择文档';
    $('importDialog').showModal();
  }

  function closeImport() {
    $('importDialog').close();
  }

  async function submitImport(event) {
    event.preventDefault();
    const file = $('importFile').files[0];
    if (!file) return;
    const data = new FormData(event.currentTarget);
    data.set('file', file);
    const button = $('submitImport');
    button.disabled = true;
    button.querySelector('span').textContent = '正在导入';
    try {
      const result = await api('/api/admin/documents/import', { method: 'POST', body: data });
      closeImport();
      toast(result.duplicate ? '文档内容未变化' : `文档已导入为 v${result.version}`);
      await Promise.all([loadDocuments(), loadAudit(true)]);
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      button.disabled = false;
      button.querySelector('span').textContent = '开始导入';
    }
  }

  async function refresh() {
    const button = $('refreshButton');
    button.classList.add('loading');
    button.disabled = true;
    try {
      if (state.view === 'documents') await loadDocuments();
      if (state.view === 'artifacts') await loadArtifacts();
      if (state.view === 'audit') await loadAudit();
      if (state.view === 'system') await Promise.all([loadHealth(), loadModelConfig(true)]);
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      button.classList.remove('loading');
      button.disabled = false;
    }
  }

  function bind() {
    document.querySelectorAll('.nav-item').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
    $('menuButton').addEventListener('click', openSidebar);
    $('sidebarScrim').addEventListener('click', closeSidebar);
    $('refreshButton').addEventListener('click', refresh);
    $('documentSearch').addEventListener('input', renderDocuments);
    $('scopeFilter').addEventListener('change', renderDocuments);
    $('auditFilter').addEventListener('change', renderAudit);
    $('closeInspector').addEventListener('click', closeInspector);
    $('addAcl').addEventListener('click', () => addAclRow());
    $('aclForm').addEventListener('submit', saveAcl);
    $('openImport').addEventListener('click', openImport);
    $('closeImport').addEventListener('click', closeImport);
    $('cancelImport').addEventListener('click', closeImport);
    $('importForm').addEventListener('submit', submitImport);
    $('artifactForm').addEventListener('submit', submitArtifact);
    $('modelForm').addEventListener('submit', saveModelConfig);
    $('checkModelRuntime').addEventListener('click', () => loadModelConfig());
    document.querySelectorAll('#modelForm input[name="mode"]').forEach(input => input.addEventListener('change', () => {
      $('modelName').value = '';
      syncModelProvider();
    }));
    $('modelProvider').addEventListener('change', () => {
      const option = $('modelProvider').selectedOptions[0];
      $('modelName').value = option?.dataset.defaultModel || '';
      renderModelSelection();
    });
    document.querySelectorAll('#artifactForm input[name="format"]').forEach(input => input.addEventListener('change', syncArtifactFormat));
    $('importFile').addEventListener('change', event => {
      $('fileLabel').textContent = event.target.files[0]?.name || '选择文档';
    });
    for (const eventName of ['dragenter', 'dragover']) $('fileDrop').addEventListener(eventName, event => { event.preventDefault(); $('fileDrop').classList.add('dragging'); });
    for (const eventName of ['dragleave', 'drop']) $('fileDrop').addEventListener(eventName, event => { event.preventDefault(); $('fileDrop').classList.remove('dragging'); });
    $('fileDrop').addEventListener('drop', event => {
      if (event.dataTransfer.files.length) {
        $('importFile').files = event.dataTransfer.files;
        $('fileLabel').textContent = event.dataTransfer.files[0].name;
      }
    });
  }

  async function init() {
    bind();
    syncArtifactFormat();
    try {
      state.me = await api('/api/me');
      setIdentity();
    } catch (error) {
      $('roleBadge').textContent = error.status === 403 ? '无管理权限' : '未认证';
      $('displayName').textContent = '无法进入管理控制台';
      $('subjectId').textContent = error.message;
      document.body.classList.add('readonly');
      toast(error.message, 'error');
      await Promise.all([loadHealth(), loadModelConfig(true)]);
      return;
    }

    try {
      await loadDocuments();
    } catch (error) {
      toast(`文档库加载失败：${error.message}`, 'error');
    }
    await Promise.all([loadArtifacts(true), loadHealth(), loadModelConfig(true)]);
  }

  init();
})();
