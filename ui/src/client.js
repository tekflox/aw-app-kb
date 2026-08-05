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

export async function kbListFiles() {
  return json('api/kb/files');
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

export async function kbAddRepo(gitUrl, name) {
  return json('api/kb/add-repo', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ git_url: gitUrl, name: name || undefined }),
  });
}

export async function kbListRepos() {
  return json('api/kb/repos');
}
