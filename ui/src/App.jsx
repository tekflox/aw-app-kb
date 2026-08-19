// Ported from agentic-workspace's src/app/src/components/KnowledgeBasePanel.jsx.
// Logic and markup are copied near-verbatim; only the import (now the
// local client.js instead of the host SPA's useComponentStatus.js hook)
// and the settings calls (now a dedicated /api/kb/settings pair instead
// of a read-modify-write against the shared /api/settings/aw blob) changed.
import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import {
  kbListFiles, kbReadFile, kbSaveFile, kbDeleteFile, kbSearchFiles, kbMcpSearch,
  kbBuild, kbMapPath, kbMapAndBuild, kbGetStatus, kbGetDocCount, kbGetSettings, kbSaveSettings,
  kbListRepos, coreBrowseFolders, coreAddFolder, coreRemoveFolder,
} from './client';
import { marked } from 'marked';

marked.setOptions({ breaks: true, gfm: true });

export default function App() {
  const [files, setFiles] = useState([]);
  const [selectedFile, setSelectedFile] = useState(null);
  const [content, setContent] = useState('');
  const [originalContent, setOriginalContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState(null);
  const [searchMode, setSearchMode] = useState('files');
  const [mcpResults, setMcpResults] = useState(null);
  const [sidebarFilter, setSidebarFilter] = useState('');
  const [expandedDirs, setExpandedDirs] = useState(new Set());
  const [viewMode, setViewMode] = useState('preview'); // 'preview' or 'edit'

  // Management panel state
  const [showManage, setShowManage] = useState(false);
  const [mapPaths, setMapPaths] = useState([]);
  const [force, setForce] = useState(false);
  const [jobStatus, setJobStatus] = useState({ running: false, operation: null, output: [], error: null, last_run: null });
  const [docCount, setDocCount] = useState(null);
  const [pathInput, setPathInput] = useState('');
  const [settingsSaving, setSettingsSaving] = useState(false);
  // Folders the user mapped at the WORKSPACE level (/api/folders), handed
  // to this container through the $AW_WORKSPACE_FOLDERS mount. Tracked
  // apart from the repo list because they carry a different promise:
  // any directory at all, chosen deliberately, with no git repo behind it.
  const [mappedFolders, setMappedFolders] = useState([]);
  // Workspace folders switched OFF for indexing. Opt-OUT, mirroring the
  // backend: the workspace decides what EXISTS, this only records exceptions,
  // so a folder mapped later is indexed without touching anything here.
  const [disabledFolders, setDisabledFolders] = useState([]);
  // Workspace-folder CRUD, ported in from core's Workspace nav (see
  // client.js's coreListFolders/coreBrowseFolders/coreAddFolder/
  // coreRemoveFolder comment for why this hits an absolute /api/folders
  // path instead of this app's own api/kb/* surface).
  const [folderBusy, setFolderBusy] = useState(false);
  const [folderError, setFolderError] = useState(null);
  const [folderPathInput, setFolderPathInput] = useState('');
  const [folderNameInput, setFolderNameInput] = useState('');
  const [folderBrowser, setFolderBrowser] = useState(null); // null = collapsed
  const [foldersReconnecting, setFoldersReconnecting] = useState(false);

  const outputRef = useRef(null);
  const pollRef = useRef(null);
  // Tracks the previous poll's job state so the expensive refreshes fire on
  // the running -> idle transition instead of on every idle tick.
  const wasRunningRef = useRef(false);

  useEffect(() => {
    kbListFiles().then(setFiles);
  }, []);

  // Load KB settings on mount
  useEffect(() => {
    kbGetSettings().then((s) => {
      setMapPaths(s.map_paths || []);
      setDisabledFolders(s.disabled_folders || []);
    });
    kbGetDocCount().then((d) => setDocCount(d.count ?? 0));
    kbGetStatus().then(setJobStatus);
    kbListRepos().then((r) => setMappedFolders(r.folders || []));
  }, []);

  // Polling for job status.
  //
  // This used to re-fetch EVERYTHING on every idle tick: the `!status.running`
  // branch is true whenever no job is running, i.e. essentially always, so an
  // idle tab fired status + doc-count + the full ~9.9k-entry file list + the
  // repo list every 10 seconds. Doc count and repo list only ever change when
  // a build/map/add-repo job finishes, so they belong on the running -> idle
  // EDGE, not on the level. The file list stays on the tick (an agent can
  // write to the KB behind our back) but is a conditional request now — see
  // kbListFiles: unchanged means 304 and the same array reference back, so no
  // re-render. Idle cadence is also slower, and a hidden tab polls nothing.
  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      if (document.hidden) return;
      try {
        const status = await kbGetStatus();
        if (cancelled) return;
        setJobStatus(status);
        if (wasRunningRef.current && !status.running) {
          const dc = await kbGetDocCount();
          if (cancelled) return;
          setDocCount(dc.count ?? 0);
          // Cover add-repo jobs finishing (Mapped Folders bare names resolve
          // against this list — stale list = the exact "path not found"
          // confusion this was built to prevent).
          kbListRepos().then((r) => !cancelled && setMappedFolders(r.folders || []));
        }
        wasRunningRef.current = status.running;
        kbListFiles().then((f) => !cancelled && setFiles(f));
      } catch {}
    };

    const interval = jobStatus.running ? 2000 : 30000;
    pollRef.current = setInterval(poll, interval);
    // Coming back to a backgrounded tab should feel current immediately
    // rather than after up to 30 s of staleness.
    const onVisibility = () => { if (!document.hidden) poll(); };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      cancelled = true;
      clearInterval(pollRef.current);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [jobStatus.running]);

  // Auto-scroll output log to bottom
  useEffect(() => {
    if (outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [jobStatus.output]);

  const showMessage = (msg) => {
    setMessage(msg);
    setTimeout(() => setMessage(''), 3000);
  };

  // Opening a hit must NOT throw the result list away. It used to clear both
  // result sets here, so every click cost you the search: you read one hit,
  // then had to retype the query to reach the second. The sidebar keeps the
  // results (with the open one highlighted) until you explicitly go Back.
  const openFile = useCallback(async (path) => {
    const res = await kbReadFile(path);
    if (res.success) {
      setSelectedFile(path);
      setContent(res.content);
      setOriginalContent(res.content);
      setViewMode('preview');
      setShowManage(false);
    }
  }, []);

  // Leave the result list and return the sidebar to the file tree. The query
  // stays in the box on purpose — Back is navigation, not a reset, so Enter
  // re-runs the same search.
  const backToTree = useCallback(() => {
    setSearchResults(null);
    setMcpResults(null);
  }, []);

  const handleSave = useCallback(async () => {
    if (!selectedFile) return;
    setSaving(true);
    try {
      const res = await kbSaveFile(selectedFile, content);
      if (res.success) {
        showMessage('Saved (index rebuilding...)');
        setOriginalContent(content);
        kbListFiles().then(setFiles);
      }
    } finally {
      setSaving(false);
    }
  }, [selectedFile, content]);

  const handleDelete = useCallback(async () => {
    if (!selectedFile) return;
    if (!confirm(`Delete ${selectedFile}?`)) return;
    const res = await kbDeleteFile(selectedFile);
    if (res.success) {
      showMessage('Deleted');
      setSelectedFile(null);
      setContent('');
      setOriginalContent('');
      kbListFiles().then(setFiles);
    }
  }, [selectedFile]);

  const handleSearch = useCallback(async () => {
    if (!searchQuery.trim()) return;
    if (searchMode === 'files') {
      const results = await kbSearchFiles(searchQuery);
      setSearchResults(results);
      setMcpResults(null);
    } else {
      const results = await kbMcpSearch(searchQuery);
      setMcpResults(Array.isArray(results) ? results : []);
      setSearchResults(null);
    }
  }, [searchQuery, searchMode]);

  // Management handlers
  const handleAddPath = useCallback(() => {
    const p = pathInput.trim();
    if (!p || mapPaths.includes(p)) return;
    setMapPaths((prev) => [...prev, p]);
    setPathInput('');
  }, [pathInput, mapPaths]);

  const handleRemovePath = useCallback((path) => {
    setMapPaths((prev) => prev.filter((p) => p !== path));
  }, []);

  const handleToggleFolder = useCallback(async (name, enabled) => {
    const next = enabled
      ? disabledFolders.filter((n) => n !== name)
      : [...disabledFolders, name];
    setDisabledFolders(next);
    try {
      await kbSaveSettings({ disabled_folders: next });
    } catch {
      setDisabledFolders(disabledFolders);   // put the switch back
      showMessage(`Could not save: ${name} left unchanged`);
    }
  }, [disabledFolders]);

  // Adding/removing a workspace folder makes core recreate THIS container
  // (see client.js), so the very fetch that would confirm success loses its
  // connection instead. Poll kbListRepos — the first call it answers again
  // means the container is back — rather than reading the drop as an error.
  const reloadFoldersAfterRestart = useCallback(async () => {
    setFoldersReconnecting(true);
    for (let attempt = 0; attempt < 12; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      try {
        const r = await kbListRepos();
        setMappedFolders(r.folders || []);
        setFoldersReconnecting(false);
        return;
      } catch {
        // container still restarting — keep polling
      }
    }
    setFoldersReconnecting(false);
    setFolderError('Lost the connection during the restart and could not reconnect — refresh the window.');
  }, []);

  const browseWorkspaceFolders = useCallback(async (path) => {
    try {
      const d = await coreBrowseFolders(path);
      setFolderBrowser(d);
      setFolderPathInput(d.path);
      setFolderError(null);
    } catch (e) {
      setFolderError(e.message);
    }
  }, []);

  const addWorkspaceFolder = useCallback(async (path, name) => {
    const target = (path || '').trim();
    if (!target) return;
    setFolderBusy(true);
    setFolderError(null);
    try {
      await coreAddFolder(target, (name || '').trim() || undefined);
      setFolderPathInput('');
      setFolderNameInput('');
      setFolderBrowser(null);
      showMessage('Folder mapped — KB container is restarting to pick it up');
      reloadFoldersAfterRestart();
    } catch (e) {
      setFolderError(e.message);
    } finally {
      setFolderBusy(false);
    }
  }, [reloadFoldersAfterRestart]);

  const removeWorkspaceFolder = useCallback(async (name) => {
    setFolderBusy(true);
    setFolderError(null);
    try {
      await coreRemoveFolder(name);
      showMessage('Folder unmapped — KB container is restarting to drop it');
      reloadFoldersAfterRestart();
    } catch (e) {
      setFolderError(e.message);
    } finally {
      setFolderBusy(false);
    }
  }, [reloadFoldersAfterRestart]);

  const handleSaveSettings = useCallback(async () => {
    setSettingsSaving(true);
    try {
      await kbSaveSettings({ map_paths: mapPaths });
      showMessage('Settings saved');
    } catch {
      showMessage('Error saving settings');
    } finally {
      setSettingsSaving(false);
    }
  }, [mapPaths]);

  const handleMapAndBuild = useCallback(async () => {
    if (jobStatus.running) return;
    const res = await kbMapAndBuild(mapPaths, force);
    if (res.error) showMessage(res.error);
    else setJobStatus((prev) => ({ ...prev, running: true, operation: 'map-and-build', output: [], error: null }));
  }, [mapPaths, force, jobStatus.running]);

  const handleBuildOnly = useCallback(async () => {
    if (jobStatus.running) return;
    const res = await kbBuild(force);
    if (res.error) showMessage(res.error);
    else setJobStatus((prev) => ({ ...prev, running: true, operation: 'build', output: [], error: null }));
  }, [force, jobStatus.running]);

  const handleMapOne = useCallback(async (path) => {
    if (jobStatus.running) return;
    const res = await kbMapPath(path, force);
    if (res.error) showMessage(res.error);
    else setJobStatus((prev) => ({ ...prev, running: true, operation: `map:${path}`, output: [], error: null }));
  }, [force, jobStatus.running]);

  const isDirty = content !== originalContent;
  const isMarkdown = selectedFile?.endsWith('.md');

  // Render markdown with frontmatter stripped
  const renderedHtml = useMemo(() => {
    if (!content) return '';
    let md = content;
    // Strip YAML frontmatter
    if (md.startsWith('---')) {
      const end = md.indexOf('---', 3);
      if (end > 0) md = md.slice(end + 3).trim();
    }
    return marked.parse(md);
  }, [content]);

  // Build tree from flat file list. Memoized: buildTree walks all ~9.9k
  // entries, and this component re-renders on every keystroke in the editor.
  const tree = useMemo(() => buildTree(files, sidebarFilter), [files, sidebarFilter]);

  const displayLines = jobStatus.output ? jobStatus.output.slice(-30) : [];

  return (
    <div className="flex-1 flex bg-[var(--color-bg-primary)] overflow-hidden h-screen">
      {/* Left: File tree + search */}
      <div className="w-72 flex flex-col border-r border-[var(--color-border)] bg-[var(--color-bg-secondary)] shrink-0">
        {/* Header with Manage button */}
        <div className="flex items-center justify-between px-2 pt-2 pb-1">
          <span className="text-[10px] uppercase text-[var(--color-text-muted)] tracking-wide font-semibold">Knowledge Base</span>
          <button
            onClick={() => { setShowManage((v) => !v); setSelectedFile(null); }}
            className={`px-2 py-0.5 text-[10px] rounded transition-colors ${showManage ? 'bg-[var(--color-accent)]/20 text-[var(--color-accent)]' : 'text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)] hover:bg-white/5'}`}
          >
            Manage
          </button>
        </div>

        {/* Search bar */}
        <div className="p-2 border-b border-[var(--color-border)]">
          <div className="flex items-center gap-1 mb-1.5">
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
              placeholder="Search knowledge base..."
              className="flex-1 bg-[var(--color-bg-primary)] text-xs text-[var(--color-text-primary)] border border-[var(--color-border)] rounded px-2 py-1.5 outline-none focus:border-[var(--color-accent)]"
            />
            <button onClick={handleSearch}
              className="p-1.5 rounded hover:bg-white/10 text-[var(--color-text-muted)] hover:text-[var(--color-accent)]">
              <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="8" /><path d="M21 21l-4.35-4.35" /></svg>
            </button>
          </div>
          <div className="flex items-center gap-1">
            <button onClick={() => { setSearchMode('files'); setSearchResults(null); setMcpResults(null); }}
              className={`px-2 py-0.5 text-[10px] rounded ${searchMode === 'files' ? 'bg-[var(--color-accent)]/20 text-[var(--color-accent)]' : 'text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)]'}`}>
              Files
            </button>
            <button onClick={() => { setSearchMode('semantic'); setSearchResults(null); setMcpResults(null); }}
              className={`px-2 py-0.5 text-[10px] rounded ${searchMode === 'semantic' ? 'bg-[var(--color-accent)]/20 text-[var(--color-accent)]' : 'text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)]'}`}>
              Semantic
            </button>
            {/* Back, not Clear: what the button actually does is leave the
                result list and return to the file tree. Sits apart from the
                Files/Semantic mode pair (ml-auto) and carries an arrow, so it
                reads as navigation rather than a third search mode. */}
            {(searchResults || mcpResults) && (
              <button onClick={backToTree}
                title="Back to the file tree"
                className="ml-auto flex items-center gap-1 px-2 py-0.5 text-[10px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)] hover:bg-white/5 hover:border-[var(--color-accent)] transition-colors">
                <svg className="w-2.5 h-2.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M19 12H5" /><path d="M12 19l-7-7 7-7" />
                </svg>
                Back
              </button>
            )}
          </div>
        </div>

        {/* Filter */}
        <div className="px-2 py-1 border-b border-[var(--color-border)]">
          <input
            value={sidebarFilter}
            onChange={(e) => setSidebarFilter(e.target.value)}
            placeholder="Filter files..."
            className="w-full bg-transparent text-xs text-[var(--color-text-primary)] outline-none placeholder:text-[var(--color-text-muted)]"
          />
        </div>

        {/* File tree or search results */}
        <div className="flex-1 overflow-y-auto text-xs">
          {searchResults ? (
            <div className="p-1">
              <div className="px-2 py-1 text-[10px] uppercase text-[var(--color-text-muted)]">
                {searchResults.length} results
              </div>
              {searchResults.map((r) => (
                <button key={r.path} onClick={() => openFile(r.path)}
                  className={`w-full text-left px-2 py-1.5 rounded hover:bg-white/5 ${selectedFile === r.path ? 'bg-white/10' : ''}`}>
                  <div className="text-[var(--color-text-primary)] truncate">{r.path}</div>
                  {r.snippet && <div className="text-[10px] text-[var(--color-text-muted)] truncate mt-0.5">{r.snippet}</div>}
                </button>
              ))}
            </div>
          ) : mcpResults ? (
            <div className="p-1">
              <div className="px-2 py-1 text-[10px] uppercase text-[var(--color-text-muted)]">
                {mcpResults.length} semantic results
              </div>
              {mcpResults.map((r, i) => {
                // One expression for both the click target and the highlight —
                // computing it twice is how they drift apart.
                const path = r.id || `${r.metadata?.repo || ''}/${r.metadata?.path || ''}`;
                return (
                <button key={i} onClick={() => openFile(path)}
                  className={`w-full text-left px-2 py-1.5 rounded hover:bg-white/5 ${selectedFile === path ? 'bg-white/10' : ''}`}>
                  <div className="flex items-center justify-between">
                    <span className="text-[var(--color-text-primary)] truncate">{r.id || r.metadata?.path || 'Unknown'}</span>
                    <span className="text-[10px] text-[var(--color-accent)] shrink-0 ml-1">{(r.score * 100).toFixed(0)}%</span>
                  </div>
                  {r.content && <div className="text-[10px] text-[var(--color-text-muted)] mt-0.5 line-clamp-2">{r.content.slice(0, 150)}</div>}
                </button>
                );
              })}
            </div>
          ) : (
            <FileTree
              tree={tree}
              selectedFile={selectedFile}
              expandedDirs={expandedDirs}
              onToggleDir={(dir) => setExpandedDirs((prev) => {
                const next = new Set(prev);
                if (next.has(dir)) next.delete(dir); else next.add(dir);
                return next;
              })}
              onSelectFile={openFile}
            />
          )}
        </div>

        {/* Footer stats */}
        <div className="px-2 py-1.5 border-t border-[var(--color-border)] text-[10px] text-[var(--color-text-muted)] flex items-center gap-2">
          <span>{files.length} files</span>
          {docCount !== null && <span>· {docCount} indexed</span>}
          {jobStatus.running && (
            <span className="flex items-center gap-1 whitespace-nowrap text-[var(--color-accent)]">
              <Spinner size="sm" />
              {jobStatus.operation}
            </span>
          )}
        </div>
      </div>

      {/* Right: Management panel or file viewer */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {showManage ? (
          <ManagePanel
            mapPaths={mapPaths}
            pathInput={pathInput}
            force={force}
            jobStatus={jobStatus}
            docCount={docCount}
            outputRef={outputRef}
            displayLines={displayLines}
            settingsSaving={settingsSaving}
            mappedFolders={mappedFolders}
            disabledFolders={disabledFolders}
            onToggleFolder={handleToggleFolder}
            onAddPath={handleAddPath}
            onRemovePath={handleRemovePath}
            onMapOne={handleMapOne}
            onMapAndBuild={handleMapAndBuild}
            onBuildOnly={handleBuildOnly}
            onPathInputChange={setPathInput}
            onForceChange={setForce}
            onSaveSettings={handleSaveSettings}
            onPathInputKeyDown={(e) => e.key === 'Enter' && handleAddPath()}
            folderBusy={folderBusy}
            folderError={folderError}
            folderPathInput={folderPathInput}
            folderNameInput={folderNameInput}
            folderBrowser={folderBrowser}
            foldersReconnecting={foldersReconnecting}
            onFolderPathInputChange={setFolderPathInput}
            onFolderNameInputChange={setFolderNameInput}
            onBrowseFolders={browseWorkspaceFolders}
            onCloseBrowser={() => setFolderBrowser(null)}
            onAddFolder={addWorkspaceFolder}
            onRemoveFolder={removeWorkspaceFolder}
          />
        ) : selectedFile ? (
          <>
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-2 border-b border-[var(--color-border)] bg-[var(--color-bg-header)] shrink-0">
              <div className="flex items-center gap-2 min-w-0">
                <svg className="w-3.5 h-3.5 text-[var(--color-text-muted)] shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" />
                  <path d="M14 2v6h6" />
                </svg>
                <span className="text-sm text-[var(--color-text-primary)] truncate font-mono">{selectedFile}</span>
                {isDirty && <span className="text-[10px] text-[var(--color-accent)]">modified</span>}
              </div>
              <div className="flex items-center gap-2">
                {message && <span className="text-xs text-[var(--color-text-muted)]">{message}</span>}
                {/* View mode toggle */}
                {isMarkdown && (
                  <div className="flex items-center border border-[var(--color-border)] rounded overflow-hidden">
                    <button onClick={() => setViewMode('preview')}
                      className={`px-2 py-0.5 text-[10px] transition-colors ${viewMode === 'preview' ? 'bg-[var(--color-accent)]/20 text-[var(--color-accent)]' : 'text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)]'}`}>
                      <svg className="w-3 h-3 inline mr-1" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></svg>
                      Preview
                    </button>
                    <button onClick={() => setViewMode('edit')}
                      className={`px-2 py-0.5 text-[10px] transition-colors ${viewMode === 'edit' ? 'bg-[var(--color-accent)]/20 text-[var(--color-accent)]' : 'text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)]'}`}>
                      <svg className="w-3 h-3 inline mr-1" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></svg>
                      Edit
                    </button>
                  </div>
                )}
                <button onClick={handleDelete}
                  className="px-3 py-1 text-xs rounded bg-[var(--color-danger)]/10 text-[var(--color-danger)] hover:bg-[var(--color-danger)]/20 transition-colors">
                  Delete
                </button>
                <button onClick={handleSave} disabled={saving || !isDirty}
                  className="px-3 py-1 text-xs rounded bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent-hover)] transition-colors disabled:opacity-50">
                  {saving ? 'Saving...' : 'Save'}
                </button>
              </div>
            </div>

            {/* Content area */}
            {viewMode === 'preview' && isMarkdown ? (
              <div
                className="flex-1 overflow-y-auto p-6 prose prose-invert prose-sm max-w-none kb-preview"
                dangerouslySetInnerHTML={{ __html: renderedHtml }}
              />
            ) : (
              <textarea
                value={content}
                onChange={(e) => setContent(e.target.value)}
                className="flex-1 bg-[var(--color-bg-terminal)] text-sm text-[var(--color-text-primary)] font-mono p-4 outline-none resize-none"
                spellCheck={false}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === 's') {
                    e.preventDefault();
                    handleSave();
                  }
                }}
              />
            )}
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center text-[var(--color-text-muted)]">
            <div className="text-center">
              <svg className="w-12 h-12 mx-auto mb-3 opacity-20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1">
                <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20" />
              </svg>
              <p className="text-sm">Select a file to view or edit</p>
              <p className="text-xs mt-1 text-[var(--color-text-muted)]/60">{files.length} files in knowledge base</p>
              <button
                onClick={() => setShowManage(true)}
                className="mt-3 px-3 py-1.5 text-xs rounded border border-dashed border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)] hover:border-[var(--color-accent)] transition-colors"
              >
                Open Build Manager
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Spinner
// ---------------------------------------------------------------------------
// Sizes must be LITERAL class names. `w-${size}` is invisible to Tailwind's
// scanner, so `w-3`/`w-2.5` were never emitted into the CSS and the svg fell
// back to its intrinsic size — a spinner as tall as the panel, which then
// squeezed the label beside it into a three-line wrap.
const SPINNER_SIZES = {
  sm: 'w-3 h-3',
  md: 'w-4 h-4',
};

function ToggleSwitch({ checked, onChange, disabled, label }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => !disabled && onChange(!checked)}
      title={label}
      className={`relative inline-flex h-4 w-7 shrink-0 items-center rounded-full transition-colors ${
        disabled ? 'bg-white/5 cursor-not-allowed'
          : checked ? 'bg-[var(--color-accent)]' : 'bg-white/10 hover:bg-white/15'
      }`}
    >
      <span
        className={`inline-block h-3 w-3 transform rounded-full bg-white shadow transition-transform ${
          checked ? 'translate-x-[14px]' : 'translate-x-0.5'
        }`}
      />
    </button>
  );
}

function Spinner({ size = 'sm' }) {
  return (
    <svg
      className={`${SPINNER_SIZES[size] || SPINNER_SIZES.sm} shrink-0 animate-spin`}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
    >
      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Management Panel
// ---------------------------------------------------------------------------
function ManagePanel({
  mapPaths, pathInput, force, jobStatus, docCount, outputRef, displayLines,
  settingsSaving, mappedFolders, disabledFolders, onToggleFolder,
  onAddPath, onRemovePath, onMapOne, onMapAndBuild, onBuildOnly,
  onPathInputChange, onForceChange, onSaveSettings, onPathInputKeyDown,
  folderBusy, folderError, folderPathInput, folderNameInput, folderBrowser,
  foldersReconnecting, onFolderPathInputChange, onFolderNameInputChange,
  onBrowseFolders, onCloseBrowser, onAddFolder, onRemoveFolder,
}) {
  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-4">
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-sm font-semibold text-[var(--color-text-primary)]">Knowledge Base Manager</h2>
        {docCount !== null && (
          <span className="text-xs text-[var(--color-text-muted)] bg-white/5 px-2 py-0.5 rounded">
            {docCount} docs indexed
          </span>
        )}
      </div>

      {/* Mapped Folders — any directory the user points at, workspace-wide,
          no git repo required. This used to be a read-only mirror of
          Workspace > Folders (add/remove lived only in core nav); it is now
          the one place that CRUD happens, ported in whole because this app
          is the only consumer of $AW_WORKSPACE_FOLDERS. Adding or removing
          restarts THIS container a few seconds later — see
          reloadFoldersAfterRestart in App.jsx — so the panel below shows a
          "reconnecting" state instead of reading the drop as an error.

          This list used to also render `map_paths` — a second, app-local
          copy of the same idea — under this same heading. The two drifted
          (map_paths held the absolute host path /opt/aw-workspace, invisible
          from in here) and the panel showed the stale copy, so the UI said
          one thing while --map-all did another. map_paths survives below as
          "Extra paths", scoped to the one case workspace folders can't
          express: kb's own private clones from --add-repo. */}
      <div className="border border-[var(--color-border)] rounded overflow-hidden">
        <div className="px-3 py-2 bg-[var(--color-bg-header)] flex items-center justify-between">
          <span className="text-xs font-semibold text-[var(--color-text-primary)]">Mapped Folders</span>
          <span className="text-[10px] text-[var(--color-text-muted)]">
            {foldersReconnecting
              ? 'restarting…'
              : `${mappedFolders.filter((n) => !disabledFolders.includes(n)).length} of ${mappedFolders.length} indexed`}
          </span>
        </div>

        {folderError && (
          <p className="px-3 py-1.5 text-[11px] bg-[var(--color-danger)]/10 text-[var(--color-danger)] border-b border-[var(--color-danger)]/30">
            {folderError}
          </p>
        )}

        <div className="divide-y divide-[var(--color-border)]">
          {mappedFolders.length === 0 && (
            <p className="px-3 py-2 text-xs text-[var(--color-text-muted)] italic">
              No folders mapped yet. Map one below — any directory, no git repo needed.
            </p>
          )}
          {mappedFolders.map((name) => {
            const enabled = !disabledFolders.includes(name);
            const busy = folderBusy || foldersReconnecting;
            return (
              <div key={name} className="flex items-center gap-2 px-3 py-1.5">
                <ToggleSwitch
                  checked={enabled}
                  onChange={(next) => onToggleFolder(name, next)}
                  label={enabled ? `Stop indexing ${name}` : `Index ${name}`}
                />
                <span
                  className={`flex-1 text-xs font-mono truncate ${
                    enabled ? 'text-[var(--color-text-primary)]' : 'text-[var(--color-text-muted)] line-through'
                  }`}
                  title={name}
                >
                  {name}
                </span>
                <button
                  onClick={() => onMapOne(name)}
                  disabled={jobStatus.running || !enabled}
                  title={enabled ? 'Map this folder now' : 'Switched off — not indexed'}
                  className="px-2 py-0.5 text-[10px] rounded bg-[var(--color-accent)]/10 text-[var(--color-accent)] hover:bg-[var(--color-accent)]/20 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  Map
                </button>
                <button
                  onClick={() => onRemoveFolder(name)}
                  disabled={busy}
                  title={`Unmap ${name}`}
                  className="p-1 rounded hover:bg-white/10 text-[var(--color-text-muted)] hover:text-[var(--color-danger)] transition-colors disabled:opacity-40 shrink-0"
                >
                  <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M18 6L6 18M6 6l12 12" />
                  </svg>
                </button>
              </div>
            );
          })}
        </div>

        {/* Add row — path + optional name + a directory browser, same shape
            as the core Workspace nav's Folders submenu this replaced. */}
        <div className="px-3 py-2 border-t border-[var(--color-border)] space-y-2">
          <div className="flex items-center gap-1.5">
            <input
              value={folderPathInput}
              onChange={(e) => onFolderPathInputChange(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && onAddFolder(folderPathInput, folderNameInput)}
              placeholder="/absolute/path/to/folder"
              disabled={folderBusy || foldersReconnecting}
              className="flex-1 min-w-0 px-2 py-1 text-[11px] font-mono rounded bg-[var(--color-bg-primary)] border border-[var(--color-border)] text-[var(--color-text-primary)] placeholder:text-[var(--color-text-muted)] focus:outline-none focus:border-[var(--color-accent)] disabled:opacity-50"
            />
            <input
              value={folderNameInput}
              onChange={(e) => onFolderNameInputChange(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && onAddFolder(folderPathInput, folderNameInput)}
              placeholder="name (optional)"
              disabled={folderBusy || foldersReconnecting}
              style={{ width: 110 }}
              className="px-2 py-1 text-[11px] rounded bg-[var(--color-bg-primary)] border border-[var(--color-border)] text-[var(--color-text-primary)] placeholder:text-[var(--color-text-muted)] focus:outline-none focus:border-[var(--color-accent)] disabled:opacity-50"
            />
            <button
              onClick={() => (folderBrowser ? onCloseBrowser() : onBrowseFolders(folderPathInput || null))}
              disabled={folderBusy || foldersReconnecting}
              className="px-2 py-1 text-[10px] rounded bg-white/5 hover:bg-white/10 text-[var(--color-text-muted)] transition-colors disabled:opacity-40 shrink-0"
            >
              {folderBrowser ? 'Hide' : 'Browse…'}
            </button>
            <button
              onClick={() => onAddFolder(folderPathInput, folderNameInput)}
              disabled={folderBusy || foldersReconnecting || !folderPathInput.trim()}
              className="px-2 py-1 text-[10px] rounded bg-[var(--color-accent)]/15 text-[var(--color-accent)] hover:bg-[var(--color-accent)]/25 transition-colors disabled:opacity-40 shrink-0"
            >
              Map
            </button>
          </div>

          {folderBrowser && (
            <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-primary)]">
              <div className="flex items-center gap-2 px-2 py-1 border-b border-[var(--color-border)]">
                <button
                  onClick={() => onBrowseFolders(folderBrowser.parent)}
                  disabled={!folderBrowser.parent}
                  className="text-[11px] text-[var(--color-text-muted)] hover:text-[var(--color-accent)] disabled:opacity-30"
                  title="Up one level"
                >
                  ↑
                </button>
                <span className="flex-1 text-[10px] font-mono text-[var(--color-text-muted)] truncate" title={folderBrowser.path}>
                  {folderBrowser.path}
                </span>
                <button
                  onClick={() => onAddFolder(folderBrowser.path, folderNameInput)}
                  disabled={folderBusy}
                  className="text-[10px] text-[var(--color-accent)] hover:underline disabled:opacity-40 shrink-0"
                >
                  Map this one
                </button>
              </div>
              <div className="overflow-y-auto" style={{ maxHeight: '26vh' }}>
                {folderBrowser.entries.length === 0 ? (
                  <div className="px-2 py-3 text-center text-[11px] text-[var(--color-text-muted)] italic">
                    No subdirectories.
                  </div>
                ) : folderBrowser.entries.map((e) => (
                  <div
                    key={e.path}
                    className="flex items-center gap-2 px-2 py-1 hover:bg-white/[0.05] cursor-pointer"
                    onClick={() => onBrowseFolders(e.path)}
                    title={e.path}
                  >
                    <span className="flex-1 text-[11px] text-[var(--color-text-primary)] truncate">{e.name}</span>
                    <button
                      onClick={(ev) => { ev.stopPropagation(); onAddFolder(e.path, ''); }}
                      disabled={folderBusy}
                      className="text-[10px] text-[var(--color-accent)] hover:underline disabled:opacity-40 shrink-0"
                    >
                      Map
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <p className="px-3 py-2 border-t border-[var(--color-border)] text-[10px] text-[var(--color-text-muted)]">
          The toggle only controls indexing; switching one off drops its docs from
          the KB on the next map. Mapping/unmapping restarts this window for a few
          seconds while the change lands.
        </p>
      </div>

      {/* Extra paths — only for what the workspace folder map cannot express:
          kb's private clones from --add-repo. Anything a workspace folder
          already covers belongs above, not here. */}
      <div className="border border-[var(--color-border)] rounded overflow-hidden">
        <div className="px-3 py-2 bg-[var(--color-bg-header)] flex items-center justify-between">
          <span className="text-xs font-semibold text-[var(--color-text-primary)]">Extra paths</span>
          <span className="text-[10px] text-[var(--color-text-muted)]">{mapPaths.length} extra</span>
        </div>
        <div className="divide-y divide-[var(--color-border)]">
          {mapPaths.length === 0 && (
            <p className="px-3 py-2 text-xs text-[var(--color-text-muted)] italic">
              None — the workspace folders above are all that gets indexed.
            </p>
          )}
          {mapPaths.map((p) => (
            <div key={p} className="flex items-center gap-2 px-3 py-1.5">
              <span className="flex-1 text-xs font-mono text-[var(--color-text-primary)] truncate" title={p}>{p}</span>
              {mappedFolders.includes(p) && (
                <span className="text-[10px] text-[var(--color-text-muted)] italic">already a workspace folder</span>
              )}
              <button
                onClick={() => onMapOne(p)}
                disabled={jobStatus.running}
                title="Map this path"
                className="px-2 py-0.5 text-[10px] rounded bg-[var(--color-accent)]/10 text-[var(--color-accent)] hover:bg-[var(--color-accent)]/20 transition-colors disabled:opacity-40"
              >
                Map
              </button>
              <button
                onClick={() => onRemovePath(p)}
                title="Remove path"
                className="px-1.5 py-0.5 text-[10px] rounded text-[var(--color-danger)] hover:bg-[var(--color-danger)]/10 transition-colors"
              >
                ×
              </button>
            </div>
          ))}
        </div>
        {/* Add path input */}
        <div className="px-3 py-2 border-t border-[var(--color-border)] flex items-center gap-2">
          <input
            value={pathInput}
            onChange={(e) => onPathInputChange(e.target.value)}
            onKeyDown={onPathInputKeyDown}
            placeholder='Path or repo name to also index (e.g. /opt/aw-workspace/repos)'
            className="flex-1 bg-[var(--color-bg-primary)] text-xs text-[var(--color-text-primary)] border border-[var(--color-border)] rounded px-2 py-1 outline-none focus:border-[var(--color-accent)]"
          />
          <button
            onClick={onAddPath}
            className="px-2 py-1 text-xs rounded bg-[var(--color-bg-header)] border border-[var(--color-border)] text-[var(--color-text-primary)] hover:bg-white/10 transition-colors"
          >
            Add
          </button>
        </div>
      </div>

      {/* Action buttons */}
      <div className="flex items-center gap-2 flex-wrap">
        <button
          onClick={onMapAndBuild}
          disabled={jobStatus.running || (mappedFolders.length === 0 && mapPaths.length === 0)}
          className="px-3 py-1.5 text-xs rounded bg-[var(--color-accent)] text-white hover:opacity-90 transition-opacity disabled:opacity-40 flex items-center gap-1.5"
        >
          {jobStatus.running && jobStatus.operation === 'map-and-build' && <Spinner size="sm" />}
          Map All + Build
        </button>
        <button
          onClick={onBuildOnly}
          disabled={jobStatus.running}
          className="px-3 py-1.5 text-xs rounded bg-[var(--color-bg-header)] border border-[var(--color-border)] text-[var(--color-text-primary)] hover:bg-white/10 transition-colors disabled:opacity-40 flex items-center gap-1.5"
        >
          {jobStatus.running && jobStatus.operation === 'build' && <Spinner size="sm" />}
          Build Only
        </button>
        <label className="flex items-center gap-1.5 text-xs text-[var(--color-text-muted)] cursor-pointer select-none ml-auto">
          <input
            type="checkbox"
            checked={force}
            onChange={(e) => onForceChange(e.target.checked)}
            className="accent-[var(--color-accent)]"
          />
          Force (wipe & rebuild)
        </label>
      </div>

      {/* Save settings button */}
      <div className="flex items-center gap-2">
        <button
          onClick={onSaveSettings}
          disabled={settingsSaving}
          className="px-3 py-1 text-xs rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)] hover:bg-white/5 transition-colors disabled:opacity-40"
        >
          {settingsSaving ? 'Saving...' : 'Save paths'}
        </button>
        <span className="text-[10px] text-[var(--color-text-muted)]">Persists map_paths to this app's own settings.json</span>
      </div>

      {/* Status section */}
      <div className="border border-[var(--color-border)] rounded overflow-hidden">
        <div className="px-3 py-2 bg-[var(--color-bg-header)] flex items-center gap-2">
          <span className="text-xs font-semibold text-[var(--color-text-primary)]">Status</span>
          {jobStatus.running && (
            <span className="flex items-center gap-1 whitespace-nowrap text-[10px] text-[var(--color-accent)]">
              <Spinner size="sm" />
              {jobStatus.operation}
            </span>
          )}
          {!jobStatus.running && jobStatus.last_run && (
            <span className="text-[10px] text-[var(--color-text-muted)]">
              Last run: {new Date(jobStatus.last_run).toLocaleTimeString()}
            </span>
          )}
        </div>

        {jobStatus.error && (
          <div className="px-3 py-2 text-xs text-[var(--color-danger)] bg-[var(--color-danger)]/5 border-b border-[var(--color-border)]">
            {jobStatus.error}
          </div>
        )}

        {displayLines.length > 0 ? (
          <pre
            ref={outputRef}
            className="p-3 text-[11px] font-mono text-[var(--color-text-secondary)] bg-[var(--color-bg-terminal)] overflow-y-auto max-h-64 whitespace-pre-wrap break-words"
          >
            {displayLines.join('\n')}
          </pre>
        ) : (
          <div className="px-3 py-4 text-xs text-[var(--color-text-muted)] italic text-center">
            No output yet. Run a build or map operation to see progress here.
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tree helpers
// ---------------------------------------------------------------------------
function buildTree(files, filter) {
  const root = { __files: [] };
  const filterLower = filter.toLowerCase();
  for (const file of files) {
    if (filter && !file.path.toLowerCase().includes(filterLower)) continue;
    const parts = file.path.split('/');
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!node[parts[i]]) node[parts[i]] = { __files: [] };
      node = node[parts[i]];
    }
    node.__files.push(file);
  }
  return root;
}

function FileTree({ tree, selectedFile, expandedDirs, onToggleDir, onSelectFile, prefix = '' }) {
  const dirs = Object.keys(tree).filter((k) => k !== '__files').sort();
  const files = tree.__files || [];
  return (
    <>
      {dirs.map((dir) => {
        const fullPath = prefix ? `${prefix}/${dir}` : dir;
        const isExpanded = expandedDirs.has(fullPath);
        return (
          <div key={dir}>
            <button onClick={() => onToggleDir(fullPath)}
              className="w-full flex items-center gap-1 px-2 py-1 hover:bg-white/5 text-[var(--color-text-secondary)]">
              <svg className={`w-3 h-3 shrink-0 transition-transform ${isExpanded ? '' : '-rotate-90'}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M6 9l6 6 6-6" /></svg>
              <svg className="w-3 h-3 shrink-0 text-[var(--color-accent)]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></svg>
              <span className="truncate">{dir}</span>
            </button>
            {isExpanded && (
              <div className="pl-3">
                <FileTree tree={tree[dir]} selectedFile={selectedFile} expandedDirs={expandedDirs} onToggleDir={onToggleDir} onSelectFile={onSelectFile} prefix={fullPath} />
              </div>
            )}
          </div>
        );
      })}
      {files.map((f) => (
        <button key={f.path} onClick={() => onSelectFile(f.path)}
          className={`w-full flex items-center gap-1 px-2 py-1 hover:bg-white/5 ${selectedFile === f.path ? 'bg-white/10 text-[var(--color-text-primary)]' : 'text-[var(--color-text-secondary)]'}`}>
          <svg className="w-3 h-3 shrink-0 ml-3 text-[var(--color-text-muted)]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" /><path d="M14 2v6h6" />
          </svg>
          <span className="truncate">{f.name}</span>
        </button>
      ))}
    </>
  );
}
