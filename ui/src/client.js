// Framework-free fetch helpers for the Knowledge Base API.
//
// Ported from agentic-workspace's src/app/src/hooks/useComponentStatus.js
// (the kb* functions). This app is served in its own document — either
// directly at "/" (standalone/dev) or through aw-workspace's reverse
// proxy at "/api/apps/kb/" (see windows/main.json's iframe widget) —
// Starlette strips that mount prefix before the request reaches this
// container (src/apps/proxy.py), so RELATIVE paths (no leading "/") are
// what make both cases resolve correctly: the browser resolves them
// against the current document's own URL either way. Never hand-build an
// absolute "/api/kb/..." URL here — see aw-app.json's window spec for why.

async function json(path, opts) {
  const res = await fetch(path, opts);
  return res.json();
}

// The file list is by far the heaviest thing this app serves (~9.9k entries
// on a fully-mapped workspace) and it is polled, so it goes through a
// conditional request: we hand back the previous ETag and, when nothing
// changed, the server answers 304 with no body and we return the SAME array
// reference we returned last time. Identity matters — `setFiles(same)` makes
// React bail out of the re-render, which is what stops the tree being rebuilt
// on every tick. If the ETag ever fails to survive a proxy hop this quietly
// degrades to the old full-body behaviour instead of breaking.
let _filesEtag = null;
let _filesCache = [];

export async function kbListFiles() {
  const res = await fetch('api/kb/files', {
    headers: _filesEtag ? { 'If-None-Match': _filesEtag } : undefined,
  });
  if (res.status === 304) return _filesCache;
  const etag = res.headers.get('ETag');
  const data = await res.json();
  _filesEtag = etag || null;
  _filesCache = Array.isArray(data) ? data : [];
  return _filesCache;
}

export async function kbReadFile(path) {
  return json(`api/kb/file/${path}`);
}

export async function kbSaveFile(path, content) {
  return json(`api/kb/file/${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
}

export async function kbDeleteFile(path) {
  return json(`api/kb/file/${path}`, { method: 'DELETE' });
}

export async function kbSearchFiles(query) {
  return json(`api/kb/search?q=${encodeURIComponent(query)}`);
}

export async function kbMcpSearch(query) {
  return json(`api/kb/mcp-search?q=${encodeURIComponent(query)}`);
}

export async function kbBuild(force = false) {
  return json('api/kb/build', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force }),
  });
}

export async function kbMapPath(path, force = false) {
  return json('api/kb/map', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, force }),
  });
}

export async function kbMapAndBuild(paths, force = false) {
  return json('api/kb/map-and-build', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths, force }),
  });
}

export async function kbGetStatus() {
  return json('api/kb/status');
}

export async function kbGetDocCount() {
  return json('api/kb/doc-count');
}

export async function kbGetSettings() {
  return json('api/kb/settings');
}

export async function kbSaveSettings(settings) {
  return json('api/kb/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  });
}


export async function kbListRepos() {
  return json('api/kb/repos');
}

// --- Core workspace API (NOT this app's own /api/kb/* surface) ------------
//
// Folder mapping is a WORKSPACE-level primitive (Workspace › Folders /
// `aw-workspace-cli folders add`), not something this app owns — this app
// just happens to be the only one that mounts $AW_WORKSPACE_FOLDERS today,
// which is why its CRUD UI now lives here instead of in core nav. These
// three calls hit core directly with an ABSOLUTE path (every other helper
// above is deliberately relative, so it survives being proxied under
// /api/apps/kb/) because /api/folders lives on core, not in this app's own
// mount — a relative path would resolve to api/apps/kb/api/folders instead.
//
// Mapping or unmapping a folder makes core recreate every container that
// declares $AW_WORKSPACE_FOLDERS — including this one. The tab loses its
// connection to THIS iframe for a few seconds right after a successful
// add/remove; callers must expect that and retry (see reloadFoldersAfterRestart
// in App.jsx), not treat the drop as a failure.

export async function coreListFolders() {
  const res = await fetch('/api/folders', { credentials: 'include' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function coreBrowseFolders(path) {
  const q = path ? `?path=${encodeURIComponent(path)}` : '';
  const res = await fetch(`/api/folders/-/browse${q}`, { credentials: 'include' });
  const d = await res.json();
  if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
  return d;
}

export async function coreAddFolder(path, name) {
  const body = { path };
  if (name) body.name = name;
  const res = await fetch('/api/folders', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const d = await res.json();
  if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
  return d;
}

export async function coreRemoveFolder(name) {
  const res = await fetch(`/api/folders/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error(d.detail || `HTTP ${res.status}`);
  }
}
