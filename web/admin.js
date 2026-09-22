(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const state = { me: null, documents: [], selected: null, acl: null, artifacts: [], audit: [], modelConfig: null, pendingReviews: [], preview: null, jobs: [], queue: null, cases: [], runs: [], evaluationMeta: null, orgDepartments: [], orgUsers: [], selectedDepartment: null, view: 'documents' };
  const viewMeta = {
    documents: ['文档库', '版本、发布状态与访问权限'],
    governance: ['知识治理', '审批、发布、作废与回滚'],
    jobs: ['导入任务', '索引队列、重试与失败原因'],
    evaluation: ['评测门', '黄金题、指标与发布前门禁'],
    artifacts: ['文档生成', '创建并下载办公文档'],
    audit: ['审计记录', '管理操作与请求追踪'],
    system: ['系统状态', '服务依赖与身份上下文'],
    org: ['组织管理', '部门与成员维护'],
  };
  const statusText = { indexed: '已发布', queued: '待处理', processing: '处理中', staged: '待审核', rejected: '已驳回', withdrawn: '已作废', failed: '失败', superseded: '已替代', running: '运行中', succeeded: '已完成', cancelled: '已取消', pass: '通过', warn: '警告', block: '阻断', overridden: '已越权放行' };
  const actionText = { document_acl_replace: '更新权限', document_import: '导入文档', document_source_view: '查看原文件', document_source_download: '下载原文件', artifact_create: '生成文件', model_config_update: '切换模型', document_review_approve: '审核通过', document_review_reject: '审核驳回', document_publish: '发布版本', document_withdraw: '作废版本', document_rollback: '回滚版本', document_version_preview: '预览版本', ingestion_job_retry: '重试任务', ingestion_job_cancel: '取消任务', document_index_completed: '索引完成', document_index_failed: '索引失败', evaluation_case_save: '保存评测用例', evaluation_case_delete: '删除评测用例', evaluation_run: '运行评测' };
  const typeText = { user: '用户', group: '用户组', role: '角色', department: '部门' };
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
    if (response.status === 401) {
      location.href = '/login';
      throw new Error('请先登录');
    }
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

  function can(capability) {
    return !!state.me?.capabilities?.includes(capability);
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
    for (const value of ['user', 'group', 'role', 'department']) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = typeText[value];
      option.selected = entry.principal_type === value;
      type.appendChild(option);
    }
    const id = document.createElement('input');
    id.maxLength = 256;
    id.value = entry.principal_id;
    id.placeholder = entry.principal_type === 'user' ? '匿名主体 ID' : entry.principal_type === 'department' ? '部门键' : '名称';
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
    renderGovernanceInspector(item);
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
    // When the user changes mode, do not silently retain a provider from the
    // previous mode (e.g. local llama.cpp). The first local provider is Ollama,
    // which is the provider most users already have running on Windows.
    const current = preferred || '';
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
    const cloud = selectedModelMode() === 'cloud';
    $('modelApiKey').disabled = !cloud;
    $('modelApiKey').placeholder = cloud
      ? '留空则使用已保存密钥，填写新 Key 可更新'
      : '仅云端供应商需要';
    renderLocalModels();
  }

  function renderLocalModels() {
    const provider = $('modelProvider').value;
    const entry = state.modelConfig?.local_models?.[provider];
    const options = entry?.models || [];
    const list = $('modelOptions');
    list.replaceChildren();
    options.forEach(name => {
      const option = document.createElement('option');
      option.value = name;
      list.appendChild(option);
    });
    $('modelOptionsHint').textContent = options.length
      ? `本机已检测到 ${options.length} 个模型，可直接选择；也可以手动填写模型名。`
      : (entry?.reachable === false ? '未连接到本地模型服务；启动后点击刷新即可读取全部模型。' : '暂未发现已安装模型，可手动填写模型名。');
  }

  function renderModelConfig(payload) {
    state.modelConfig = payload;
    const active = payload.active;
    const modeInput = document.querySelector(`#modelForm input[name="mode"][value="${active.mode}"]`);
    if (modeInput) modeInput.checked = true;
    $('modelName').value = active.model;
    $('modelApiKey').value = '';
    $('responseStrategy').value = payload.response_strategy || payload.override?.response_strategy || 'knowledge_first';
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
          response_strategy: data.get('response_strategy') || 'knowledge_first',
          api_key: String(data.get('api_key') || '').trim(),
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

  async function loadJobs() {
    const status = $('jobFilter').value;
    const query = status ? `?limit=50&status=${encodeURIComponent(status)}` : '?limit=50';
    const payload = await api(`/api/admin/ingestion/jobs${query}`);
    state.jobs = payload.items || [];
    state.queue = payload.queue || null;
    renderJobs(!!payload.worker_enabled);
  }

  function renderJobs(workerEnabled) {
    const queue = state.queue || {};
    $('metricQueued').textContent = Number(queue.queued || 0).toLocaleString();
    $('metricRunning').textContent = Number(queue.running || 0).toLocaleString();
    $('metricFailedJobs').textContent = Number(queue.failed || 0).toLocaleString();
    $('metricSucceeded').textContent = Number(queue.succeeded || 0).toLocaleString();
    $('workerState').textContent = workerEnabled ? '异步导入已启用' : '异步导入未启用';
    $('jobsHint').textContent = workerEnabled
      ? '导入接口把文档排队，由 Worker 进程完成解析与向量化；失败可重试，超过重试上限后需要人工介入。'
      : '当前为同步导入（IT_INGESTION_WORKER_ENABLED=false），此列表只显示历史任务。';

    const body = $('jobRows');
    body.replaceChildren();
    $('jobsCount').textContent = `${state.jobs.length} 条`;
    $('jobsEmpty').hidden = state.jobs.length > 0;
    for (const item of state.jobs) {
      const row = document.createElement('tr');
      const jobCell = document.createElement('td');
      jobCell.textContent = `#${item.job_id} · ${item.job_type}`;
      const titleCell = document.createElement('td');
      titleCell.textContent = item.title || '-';
      const versionCell = document.createElement('td');
      versionCell.textContent = item.version ? `v${item.version}` : '-';
      const statusCell = document.createElement('td');
      statusCell.appendChild(statusBadge(item.status));
      const attemptCell = document.createElement('td');
      attemptCell.textContent = `${Number(item.attempts || 0)}/${Number(item.max_attempts || 0)}`;
      const errorCell = document.createElement('td');
      errorCell.textContent = item.last_error_code || '-';
      const timeCell = document.createElement('td');
      timeCell.textContent = formatTime(item.finished_at || item.started_at || item.created_at);
      const actionCell = document.createElement('td');
      const actions = document.createElement('div');
      actions.className = 'version-actions';
      if (item.status === 'failed' || item.status === 'cancelled') {
        actions.appendChild(versionAction('重试', 'document.write', () => retryJob(item.job_id)));
      }
      if (item.status === 'queued') {
        actions.appendChild(versionAction('取消', 'document.write', () => cancelJob(item.job_id)));
      }
      actionCell.appendChild(actions);
      row.append(jobCell, titleCell, versionCell, statusCell, attemptCell, errorCell, timeCell, actionCell);
      body.appendChild(row);
    }
  }

  async function retryJob(jobId) {
    try {
      await api(`/api/admin/ingestion/jobs/${jobId}/retry`, { method: 'POST' });
      toast(`任务 #${jobId} 已重新排队`);
      await loadJobs();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function cancelJob(jobId) {
    try {
      await api(`/api/admin/ingestion/jobs/${jobId}/cancel`, { method: 'POST' });
      toast(`任务 #${jobId} 已取消`);
      await loadJobs();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function loadEvaluation() {
    const [cases, runs] = await Promise.all([
      api('/api/admin/evaluation/cases'),
      api('/api/admin/evaluation/runs?limit=50'),
    ]);
    state.cases = cases.items || [];
    state.evaluationMeta = cases;
    state.runs = runs.items || [];
    renderEvaluationCases();
    renderEvaluationRuns();
  }

  function renderEvaluationCases() {
    const thresholds = (state.evaluationMeta || {}).thresholds || {};
    $('thresholdPill').textContent = thresholds.min_recall === undefined
      ? '-'
      : `recall ≥ ${thresholds.min_recall} · 引用 ≥ ${thresholds.min_citation_accuracy}`;
    const body = $('caseRows');
    body.replaceChildren();
    $('caseCount').textContent = `${state.cases.length} 条`;
    $('casesEmpty').hidden = state.cases.length > 0;
    for (const item of state.cases) {
      const row = document.createElement('tr');
      const keyCell = document.createElement('td');
      keyCell.textContent = item.case_key;
      const questionCell = document.createElement('td');
      questionCell.textContent = item.question;
      const documentCell = document.createElement('td');
      documentCell.textContent = item.expected_document_key || '-';
      const headingCell = document.createElement('td');
      headingCell.textContent = item.expected_heading || '-';
      const typeCell = document.createElement('td');
      typeCell.textContent = item.expect_refusal ? '应拒答' : '应命中';
      const statusCell = document.createElement('td');
      const badge = document.createElement('span');
      badge.className = `status status-${item.active ? 'indexed' : 'superseded'}`;
      badge.textContent = item.active ? '启用' : '已停用';
      statusCell.appendChild(badge);

      const actionCell = document.createElement('td');
      const actions = document.createElement('div');
      actions.className = 'version-actions';
      actions.append(versionAction('编辑', 'evaluation.run', () => openCaseDialog(item)));
      if (item.active) {
        actions.append(versionAction('停用', 'evaluation.run', () => deactivateCase(item)));
      }
      actionCell.appendChild(actions);

      row.append(keyCell, questionCell, documentCell, headingCell, typeCell, statusCell, actionCell);
      body.appendChild(row);
    }
  }

  function metricText(value) {
    return value === null || value === undefined ? '-' : Number(value).toFixed(3);
  }

  function renderEvaluationRuns() {
    const body = $('runRows');
    body.replaceChildren();
    $('runCount').textContent = `${state.runs.length} 条`;
    $('runsEmpty').hidden = state.runs.length > 0;
    for (const item of state.runs) {
      const row = document.createElement('tr');
      const idCell = document.createElement('td');
      idCell.textContent = `#${item.run_id}`;
      const triggerCell = document.createElement('td');
      triggerCell.textContent = item.trigger;
      const gateCell = document.createElement('td');
      gateCell.appendChild(statusBadge(item.gate_result || item.status));
      const recallCell = document.createElement('td');
      recallCell.textContent = metricText(item.recall_at_k);
      const citationCell = document.createElement('td');
      citationCell.textContent = metricText(item.citation_accuracy);
      const refusalCell = document.createElement('td');
      refusalCell.textContent = metricText(item.refusal_accuracy);
      const passCell = document.createElement('td');
      passCell.textContent = `${Number(item.passed_cases || 0)}/${Number(item.total_cases || 0)}`;
      const timeCell = document.createElement('td');
      timeCell.textContent = formatTime(item.finished_at || item.started_at);
      const actionCell = document.createElement('td');
      actionCell.appendChild(versionAction('明细', 'document.read', () => showRunDetail(item.run_id)));
      row.append(idCell, triggerCell, gateCell, recallCell, citationCell, refusalCell, passCell, timeCell, actionCell);
      body.appendChild(row);
    }
  }

  async function showRunDetail(runId) {
    try {
      const payload = await api(`/api/admin/evaluation/runs/${runId}`);
      const run = payload.run;
      const container = $('runDetail');
      container.replaceChildren();
      const head = document.createElement('div');
      head.className = 'section-title';
      const title = document.createElement('h2');
      title.textContent = `运行 #${run.run_id} · ${statusText[run.gate_result] || run.status}`;
      head.appendChild(title);
      const reason = document.createElement('p');
      reason.className = 'reason-hint';
      reason.textContent = run.gate_reason || '指标全部达标';
      container.append(head, reason);
      for (const item of run.results) {
        const card = document.createElement('div');
        card.className = 'chunk-item';
        const label = document.createElement('strong');
        let outcome = '未命中';
        if (item.refusal_ok !== null && item.refusal_ok !== undefined) {
          outcome = item.refusal_ok ? '正确拒答' : '未拒答';
        } else if (item.citation_ok) {
          outcome = `命中第 ${item.matched_rank} 位且章节一致`;
        } else if (item.retrieved) {
          outcome = '命中但章节不符';
        }
        label.textContent = `${item.case_key} · ${outcome} · ${item.latency_ms}ms`;
        const body = document.createElement('pre');
        body.textContent = item.question;
        card.append(label, body);
        container.appendChild(card);
      }
      if (!run.results.length) {
        const empty = document.createElement('p');
        empty.className = 'reason-hint';
        empty.textContent = '该运行没有用例结果。';
        container.appendChild(empty);
      }
      container.hidden = false;
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function openCaseDialog(item = null) {
    const form = $('caseForm');
    form.reset();
    $('caseDialogTitle').textContent = item ? `编辑用例 ${item.case_key}` : '新增用例';
    if (item) {
      form.elements.case_key.value = item.case_key;
      form.elements.question.value = item.question;
      form.elements.expect_refusal.checked = !!item.expect_refusal;
      form.elements.expected_document_key.value = item.expected_document_key || '';
      form.elements.expected_heading.value = item.expected_heading || '';
      form.elements.tags.value = item.tags || '';
      form.elements.active.checked = !!item.active;
    }
    $('caseDialog').showModal();
  }

  async function submitCase(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = {
      case_key: form.elements.case_key.value.trim(),
      question: form.elements.question.value.trim(),
      expect_refusal: form.elements.expect_refusal.checked,
      expected_document_key: form.elements.expected_document_key.value.trim(),
      expected_heading: form.elements.expected_heading.value.trim(),
      tags: form.elements.tags.value.trim(),
      active: form.elements.active.checked,
    };
    try {
      await api('/api/admin/evaluation/cases', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      toast(`用例 ${payload.case_key} 已保存`);
      $('caseDialog').close();
      await Promise.all([loadEvaluation(), loadAudit(true)]);
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function deactivateCase(item) {
    try {
      await api('/api/admin/evaluation/cases', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          case_key: item.case_key,
          question: item.question,
          expect_refusal: item.expect_refusal,
          expected_document_key: item.expected_document_key || '',
          expected_heading: item.expected_heading || '',
          tags: item.tags || '',
          active: false,
        }),
      });
      toast(`用例 ${item.case_key} 已停用`);
      await loadEvaluation();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function runEvaluation() {
    try {
      const payload = await api('/api/admin/evaluation/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ trigger: 'manual' }),
      });
      const run = payload.run;
      toast(
        `评测完成：${statusText[run.gate_result] || run.status}，通过 `
        + `${run.passed_cases}/${run.total_cases}`,
      );
      await Promise.all([loadEvaluation(), loadAudit(true)]);
      await showRunDetail(run.run_id);
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function reviewerPath(documentId, version, action) {
    return `/api/admin/documents/${documentId}/versions/${version}/${action}`;
  }

  function formatTime(value) {
    if (!value) return '-';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false });
  }

  function syncNavigation() {
    const governanceVisible = can('document.review');
    $('governanceNav').hidden = !governanceVisible;
    $('jobsNav').hidden = !can('document.read');
    $('evaluationNav').hidden = !can('document.read');
    $('orgNav').hidden = !can('audit.read');
    $('runEvaluation').hidden = !can('evaluation.run');
    $('openCase').hidden = !can('evaluation.run');
    const modeText = state.me?.governance_mode === 'review' ? '审批发布' : '直接发布';
    $('governanceMode').textContent = modeText;
    $('governanceModeInline').textContent = modeText;
    const gateText = { off: '评测门已关闭', warn: '评测门：仅告警', block: '评测门：阻断发布' };
    $('gateModePill').textContent = gateText[state.me?.evaluation_gate_mode] || '-';
    if (!governanceVisible && state.view === 'governance') setView('documents');
    if (!can('document.read') && ['jobs', 'evaluation'].includes(state.view)) setView('documents');
  }

  async function loadGovernance() {
    const payload = await api('/api/admin/governance/pending');
    state.pendingReviews = payload.items || [];
    renderGovernance();
  }

  function renderGovernance() {
    const body = $('governanceRows');
    body.replaceChildren();
    $('governanceCount').textContent = `${state.pendingReviews.length} 个`;
    $('governanceEmpty').hidden = state.pendingReviews.length > 0;
    for (const item of state.pendingReviews) {
      const row = document.createElement('tr');

      const titleCell = document.createElement('td');
      const copy = document.createElement('span');
      copy.className = 'document-copy';
      const title = document.createElement('strong');
      title.textContent = item.title;
      const source = document.createElement('small');
      source.textContent = item.source_key;
      copy.append(title, source);
      titleCell.appendChild(copy);

      const versionCell = document.createElement('td');
      versionCell.textContent = `v${item.version}`;
      const submitterCell = document.createElement('td');
      submitterCell.textContent = shortId(item.submitted_by_subject_id, 10);
      const timeCell = document.createElement('td');
      timeCell.textContent = formatTime(item.submitted_at);
      const chunkCell = document.createElement('td');
      chunkCell.textContent = Number(item.chunk_count || 0).toLocaleString();
      const actionCell = document.createElement('td');
      const review = document.createElement('button');
      review.className = 'secondary-button';
      review.type = 'button';
      review.textContent = '审核';
      review.addEventListener('click', () => openPreview(item.document_id, item.version, item.title));
      actionCell.appendChild(review);

      row.append(titleCell, versionCell, submitterCell, timeCell, chunkCell, actionCell);
      body.appendChild(row);
    }
  }

  function versionAction(label, capability, handler, kind = 'secondary-button') {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = kind;
    button.textContent = label;
    button.hidden = !can(capability);
    button.addEventListener('click', handler);
    return button;
  }

  function renderGovernanceInspector(item) {
    const list = $('versionList');
    list.replaceChildren();
    const versions = item?.versions || [];
    $('versionEmpty').hidden = versions.length > 0;
    for (const version of versions) {
      const card = document.createElement('div');
      card.className = 'version-item';

      const head = document.createElement('div');
      head.className = 'version-item-head';
      const label = document.createElement('strong');
      label.textContent = `v${version.version}`;
      head.append(label, statusBadge(version.status));

      const meta = document.createElement('div');
      meta.className = 'version-item-meta';
      const parts = [`提交 ${shortId(version.submitted_by_subject_id, 10)} · ${formatTime(version.submitted_at)}`];
      if (version.published_at) parts.push(`发布 ${formatTime(version.published_at)}`);
      if (version.withdrawn_reason) parts.push(`作废原因：${version.withdrawn_reason}`);
      if (version.error_code) parts.push(`错误：${version.error_code}`);
      meta.textContent = parts.join(' · ');

      const actions = document.createElement('div');
      actions.className = 'version-actions';
      if (version.status === 'staged') {
        actions.append(
          versionAction('预览', 'document.review', () => openPreview(item.document_id, version.version, item.title)),
          versionAction('通过', 'document.review', () => decideVersion(item.document_id, version.version, 'approve', ''), 'primary-button'),
          versionAction('驳回', 'document.review', () => decideVersion(item.document_id, version.version, 'reject', '')),
          versionAction('发布', 'document.publish', () => publishVersion(item.document_id, version.version), 'primary-button'),
        );
      }
      if (version.status === 'indexed') {
        actions.append(versionAction('作废', 'document.withdraw', () => openReason(
          '作废版本',
          '作废后该版本立即退出检索；原文件、知识块和审批记录都会保留。',
          reason => withdrawVersion(item.document_id, version.version, reason),
        )));
      }
      if (version.status === 'superseded' || version.status === 'withdrawn') {
        actions.append(versionAction('回滚到此版本', 'document.rollback', () => openReason(
          '回滚版本',
          '回滚会把该版本重新置为已发布，当前已发布版本转为已替代；不会重新向量化。',
          reason => rollbackVersion(item.document_id, version.version, reason),
        )));
      }

      const history = document.createElement('div');
      history.className = 'version-item-meta';
      actions.append(versionAction('审批记录', 'document.read', () => loadVersionReviews(
        item.document_id, version.version, history,
      )));

      card.append(head, meta, actions, history);
      list.appendChild(card);
    }
  }

  async function loadVersionReviews(documentId, version, container) {
    try {
      const payload = await api(reviewerPath(documentId, version, 'reviews'));
      container.replaceChildren();
      if (!payload.items.length) {
        container.textContent = '暂无审批记录';
        return;
      }
      container.textContent = payload.items.map(record => {
        const override = record.is_override ? '（越权）' : '';
        const comment = record.comment ? ` · ${record.comment}` : '';
        return `${formatTime(record.created_at)} · ${record.action}${override} · ${shortId(record.actor_subject_id, 8)}${comment}`;
      }).join('\n');
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function openPreview(documentId, version, title = '') {
    try {
      const payload = await api(reviewerPath(documentId, version, 'preview'));
      const record = payload.version;
      state.preview = { document_id: documentId, version, status: record.status };
      $('previewTitle').textContent = title ? `${title} · v${version}` : `v${version}`;

      const meta = $('previewMeta');
      meta.replaceChildren();
      const rows = [
        ['状态', statusText[record.status] || record.status],
        ['知识块', String(record.chunk_count ?? payload.chunks.length)],
        ['提交人', shortId(record.submitted_by_subject_id, 12)],
        ['提交时间', formatTime(record.submitted_at)],
      ];
      for (const [name, value] of rows) {
        const cell = document.createElement('div');
        const term = document.createElement('dt');
        term.textContent = name;
        const detail = document.createElement('dd');
        detail.textContent = value;
        cell.append(term, detail);
        meta.appendChild(cell);
      }

      const chunks = $('previewChunks');
      chunks.replaceChildren();
      for (const chunk of payload.chunks) {
        const item = document.createElement('div');
        item.className = 'chunk-item';
        const heading = document.createElement('strong');
        const page = chunk.page ? ` · 第 ${chunk.page} 页` : '';
        heading.textContent = `#${chunk.ordinal + 1} ${chunk.heading || '正文'}${page}`;
        const body = document.createElement('pre');
        body.textContent = chunk.content;
        item.append(heading, body);
        chunks.appendChild(item);
      }
      if (!payload.chunks.length) {
        const empty = document.createElement('p');
        empty.className = 'reason-hint';
        empty.textContent = '该版本没有可预览的内容块。';
        chunks.appendChild(empty);
      }

      $('previewComment').value = '';
      $('overrideReview').checked = false;
      $('overrideField').hidden = !(
        state.me?.governance_override_allowed || state.me?.evaluation_override_allowed
      );
      const staged = record.status === 'staged';
      $('approveVersion').hidden = !(staged && can('document.review'));
      $('rejectVersion').hidden = !(staged && can('document.review'));
      $('publishVersion').hidden = !(staged && can('document.publish'));
      $('previewDialog').showModal();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function overrideRequested() {
    return !!$('overrideReview')?.checked;
  }

  async function afterGovernanceChange() {
    await Promise.all([
      loadDocuments(),
      loadGovernance().catch(() => {}),
      loadJobs().catch(() => {}),
      loadAudit(true),
    ]);
    if (state.selected) {
      const refreshed = state.documents.find(item => item.document_id === state.selected.document_id);
      if (refreshed) {
        state.selected = refreshed;
        renderGovernanceInspector(refreshed);
      }
    }
  }

  async function decideVersion(documentId, version, decision, comment) {
    try {
      await api(reviewerPath(documentId, version, 'review'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, comment, override: overrideRequested() }),
      });
      toast(decision === 'approve' ? '已通过审核，仍需发布才会生效' : '已驳回该版本');
      $('previewDialog').close();
      await afterGovernanceChange();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function publishVersion(documentId, version) {
    try {
      await api(reviewerPath(documentId, version, 'publish'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          comment: $('previewComment').value.trim(),
          override: overrideRequested(),
        }),
      });
      toast('版本已发布上线');
      $('previewDialog').close();
      await afterGovernanceChange();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function withdrawVersion(documentId, version, reason) {
    try {
      await api(reviewerPath(documentId, version, 'withdraw'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),
      });
      toast('版本已作废并退出检索');
      await afterGovernanceChange();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function rollbackVersion(documentId, version, reason) {
    try {
      await api(reviewerPath(documentId, version, 'rollback'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),
      });
      toast('已回滚到该版本');
      await afterGovernanceChange();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  let reasonHandler = null;

  function openReason(title, hint, handler) {
    reasonHandler = handler;
    $('reasonTitle').textContent = title;
    $('reasonHint').textContent = hint;
    $('reasonText').value = '';
    $('reasonDialog').showModal();
  }

  async function confirmReason() {
    const reason = $('reasonText').value.trim();
    if (!reason) {
      toast('请填写原因', 'error');
      return;
    }
    const handler = reasonHandler;
    reasonHandler = null;
    $('reasonDialog').close();
    if (handler) await handler(reason);
  }

  function setView(view) {
    state.view = view;
    document.querySelectorAll('.nav-item').forEach(button => button.classList.toggle('active', button.dataset.view === view));
    document.querySelectorAll('.view').forEach(section => section.classList.toggle('active', section.id === `view-${view}`));
    [$('pageTitle').textContent, $('pageSubtitle').textContent] = viewMeta[view];
    closeSidebar();
    if (view === 'audit') loadAudit();
    if (view === 'governance') loadGovernance().catch(error => toast(error.message, 'error'));
    if (view === 'jobs') loadJobs().catch(error => toast(error.message, 'error'));
    if (view === 'evaluation') loadEvaluation().catch(error => toast(error.message, 'error'));
    if (view === 'artifacts') loadArtifacts();
    if (view === 'org') loadOrg();
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
      if (result.duplicate) {
        toast('文档内容未变化');
      } else if (result.queued) {
        toast(`已排队：任务 #${result.job_id}，由 Worker 完成索引`);
      } else if (result.review_required) {
        toast(`已提交待审核：v${result.version}`);
      } else {
        toast(`文档已导入并发布为 v${result.version}`);
      }
      await afterGovernanceChange();
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
      if (state.view === 'org') await loadOrg();
      if (state.view === 'governance') await loadGovernance();
      if (state.view === 'jobs') await loadJobs();
      if (state.view === 'evaluation') await loadEvaluation();
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

  async function setupAuthentication() {
    const config = await api('/api/auth/config');
    $('logoutButton').hidden = !['local', 'oidc'].includes(config.mode);
  }

  async function logout() {
    const payload = await api('/api/auth/logout', { method: 'POST' });
    location.href = payload.redirect || '/login';
  }

  // --- 组织管理（部门 / 成员）---------------------------------------------------
  async function loadOrg() {
    const [departments, users] = await Promise.all([
      api('/api/admin/org/departments'),
      api('/api/admin/org/users'),
    ]);
    state.orgDepartments = departments.items || [];
    state.orgUsers = users.items || [];
    renderOrg();
  }

  function memberCount(departmentKey) {
    return state.orgUsers.filter(u => u.department_key === departmentKey).length;
  }

  function renderOrg() {
    const body = $('departmentRows');
    body.replaceChildren();
    $('departmentCount').textContent = `${state.orgDepartments.length} 个`;
    $('departmentEmpty').hidden = state.orgDepartments.length > 0;
    for (const dept of state.orgDepartments) {
      const row = document.createElement('tr');
      row.tabIndex = 0;
      const keyCell = document.createElement('td');
      keyCell.textContent = dept.department_key;
      const nameCell = document.createElement('td');
      nameCell.textContent = dept.name || dept.department_key;
      const countCell = document.createElement('td');
      countCell.textContent = String(memberCount(dept.department_key));
      const actionCell = document.createElement('td');
      const memberBtn = document.createElement('button');
      memberBtn.type = 'button';
      memberBtn.className = 'text-button';
      memberBtn.textContent = '查看成员';
      memberBtn.addEventListener('click', event => { event.stopPropagation(); selectDepartment(dept.department_key); });
      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'text-button acl-write-only';
      delBtn.textContent = '删除';
      delBtn.addEventListener('click', event => { event.stopPropagation(); deleteDepartment(dept.department_key); });
      actionCell.append(memberBtn, delBtn);
      row.append(keyCell, nameCell, countCell, actionCell);
      row.addEventListener('click', () => selectDepartment(dept.department_key));
      body.appendChild(row);
    }
    if (state.selectedDepartment) renderOrgMembers(state.selectedDepartment);
    syncOrgCapabilities();
  }

  function selectDepartment(departmentKey) {
    state.selectedDepartment = departmentKey;
    $('orgInspector').hidden = false;
    $('orgInspectorTitle').textContent = departmentKey;
    $('memberDialogDept').textContent = departmentKey;
    renderOrg();
  }

  function renderOrgMembers(departmentKey) {
    const body = $('memberList');
    body.replaceChildren();
    const members = state.orgUsers.filter(u => u.department_key === departmentKey);
    $('memberEmpty').hidden = members.length > 0;
    for (const user of members) {
      const row = document.createElement('div');
      row.className = 'acl-row';
      const name = document.createElement('span');
      name.textContent = `${user.display_name || user.subject_id}（${user.subject_id}）`;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'icon-button acl-write-only';
      remove.title = '移除';
      remove.setAttribute('aria-label', '移除');
      remove.appendChild(icon('trash'));
      remove.addEventListener('click', () => removeMember(departmentKey, user.subject_id));
      row.append(name, remove);
      body.appendChild(row);
    }
  }

  function syncOrgCapabilities() {
    const write = can('acl.write');
    document.querySelectorAll('#view-org .acl-write-only').forEach(el => { el.hidden = !write; });
  }

  function openCreateDepartment() { $('departmentForm').reset(); $('departmentDialog').showModal(); }
  function openAddMember() {
    const select = $('memberUserSelect');
    select.replaceChildren();
    const members = new Set(state.orgUsers.filter(u => u.department_key === state.selectedDepartment).map(u => u.subject_id));
    const candidates = state.orgUsers.filter(u => !members.has(u.subject_id));
    if (!candidates.length) {
      const opt = document.createElement('option');
      opt.value = ''; opt.textContent = '没有可添加的用户'; opt.disabled = true;
      select.appendChild(opt);
    } else {
      for (const u of candidates) {
        const opt = document.createElement('option');
        opt.value = u.subject_id;
        opt.textContent = `${u.display_name || u.subject_id}（${u.subject_id}）`;
        select.appendChild(opt);
      }
    }
    $('memberDialog').showModal();
  }

  async function submitCreateDepartment(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const key = form.elements.department_key.value.trim();
    const name = form.elements.name.value.trim();
    if (!key) { toast('部门键不能为空', 'error'); return; }
    try {
      await api('/api/admin/org/departments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ department_key: key, name }),
      });
      $('departmentDialog').close();
      toast('部门已创建');
      await loadOrg();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function deleteDepartment(departmentKey) {
    if (!window.confirm(`确认删除部门「${departmentKey}」？该部门成员关系将被移除。`)) return;
    try {
      await api(`/api/admin/org/departments/${encodeURIComponent(departmentKey)}`, { method: 'DELETE' });
      if (state.selectedDepartment === departmentKey) { state.selectedDepartment = null; $('orgInspector').hidden = true; }
      toast('部门已删除');
      await loadOrg();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function submitMember(event) {
    event.preventDefault();
    const subjectId = $('memberUserSelect').value;
    if (!subjectId || !state.selectedDepartment) return;
    try {
      await api(`/api/admin/org/departments/${encodeURIComponent(state.selectedDepartment)}/members`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject_id: subjectId }),
      });
      $('memberDialog').close();
      toast('成员已添加');
      await loadOrg();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function removeMember(departmentKey, subjectId) {
    if (!window.confirm('确认将用户从该部门移除？')) return;
    try {
      await api(`/api/admin/org/departments/${encodeURIComponent(departmentKey)}/members/${encodeURIComponent(subjectId)}`, { method: 'DELETE' });
      toast('成员已移除');
      await loadOrg();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function bind() {
    document.querySelectorAll('.nav-item').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
    $('menuButton').addEventListener('click', openSidebar);
    $('sidebarScrim').addEventListener('click', closeSidebar);
    $('refreshButton').addEventListener('click', refresh);
    $('logoutButton').addEventListener('click', logout);
    $('documentSearch').addEventListener('input', renderDocuments);
    $('scopeFilter').addEventListener('change', renderDocuments);
    $('auditFilter').addEventListener('change', renderAudit);
    $('jobFilter').addEventListener('change', () => loadJobs().catch(error => toast(error.message, 'error')));
    $('runEvaluation').addEventListener('click', runEvaluation);
    $('openCase').addEventListener('click', () => openCaseDialog());
    $('closeCase').addEventListener('click', () => $('caseDialog').close());
    $('cancelCase').addEventListener('click', () => $('caseDialog').close());
    $('caseForm').addEventListener('submit', submitCase);
    $('closeInspector').addEventListener('click', closeInspector);
    $('addAcl').addEventListener('click', () => addAclRow());
    $('aclForm').addEventListener('submit', saveAcl);
    $('openImport').addEventListener('click', openImport);
    $('closeImport').addEventListener('click', closeImport);
    $('cancelImport').addEventListener('click', closeImport);
    $('importForm').addEventListener('submit', submitImport);
    $('closePreview').addEventListener('click', () => $('previewDialog').close());
    $('approveVersion').addEventListener('click', () => {
      if (!state.preview) return;
      decideVersion(state.preview.document_id, state.preview.version, 'approve',
        $('previewComment').value.trim());
    });
    $('rejectVersion').addEventListener('click', () => {
      const comment = $('previewComment').value.trim();
      if (!comment) {
        toast('驳回必须填写审核意见', 'error');
        return;
      }
      if (!state.preview) return;
      decideVersion(state.preview.document_id, state.preview.version, 'reject', comment);
    });
    $('publishVersion').addEventListener('click', () => {
      if (!state.preview) return;
      publishVersion(state.preview.document_id, state.preview.version);
    });
    $('closeReason').addEventListener('click', () => $('reasonDialog').close());
    $('cancelReason').addEventListener('click', () => $('reasonDialog').close());
    $('confirmReason').addEventListener('click', confirmReason);
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
    $('openCreateDepartment').addEventListener('click', openCreateDepartment);
    $('closeDepartment').addEventListener('click', () => $('departmentDialog').close());
    $('cancelDepartment').addEventListener('click', () => $('departmentDialog').close());
    $('departmentForm').addEventListener('submit', submitCreateDepartment);
    $('openAddMember').addEventListener('click', openAddMember);
    $('closeMember').addEventListener('click', () => $('memberDialog').close());
    $('cancelMember').addEventListener('click', () => $('memberDialog').close());
    $('memberForm').addEventListener('submit', submitMember);
    $('closeOrgInspector').addEventListener('click', () => { state.selectedDepartment = null; $('orgInspector').hidden = true; renderOrg(); });
  }

  async function init() {
    bind();
    syncArtifactFormat();
    try {
      await setupAuthentication();
      state.me = await api('/api/me');
      setIdentity();
      syncNavigation();
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
    await Promise.all([
      loadArtifacts(true),
      loadHealth(),
      loadModelConfig(true),
      can('document.review') ? loadGovernance().catch(() => {}) : Promise.resolve(),
      can('document.read') ? loadJobs().catch(() => {}) : Promise.resolve(),
      can('document.read') ? loadEvaluation().catch(() => {}) : Promise.resolve(),
      can('audit.read') ? loadOrg().catch(() => {}) : Promise.resolve(),
    ]);
  }

  init();
})();
