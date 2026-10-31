import getWorkbenchServiceOverride from '@codingame/monaco-vscode-workbench-service-override'
import getExplorerServiceOverride from '@codingame/monaco-vscode-explorer-service-override'
import getSearchServiceOverride from '@codingame/monaco-vscode-search-service-override'
import getPreferencesServiceOverride from '@codingame/monaco-vscode-preferences-service-override'
import getKeybindingsServiceOverride from '@codingame/monaco-vscode-keybindings-service-override'
import getThemeServiceOverride from '@codingame/monaco-vscode-theme-service-override'
import getTextmateServiceOverride from '@codingame/monaco-vscode-textmate-service-override'
import getLanguagesServiceOverride from '@codingame/monaco-vscode-languages-service-override'
import getExtensionsServiceOverride from '@codingame/monaco-vscode-extensions-service-override'
import getFilesServiceOverride, { registerCustomProvider, registerFileSystemOverlay } from '@codingame/monaco-vscode-files-service-override'
import getHostServiceOverride from '@codingame/monaco-vscode-host-service-override'
import getQuickAccessServiceOverride from '@codingame/monaco-vscode-quickaccess-service-override'
import getLifecycleServiceOverride from '@codingame/monaco-vscode-lifecycle-service-override'
import getStorageServiceOverride from '@codingame/monaco-vscode-storage-service-override'
import getConfigurationServiceOverride from '@codingame/monaco-vscode-configuration-service-override'
import getEnvironmentServiceOverride from '@codingame/monaco-vscode-environment-service-override'
import getModelServiceOverride from '@codingame/monaco-vscode-model-service-override'
import getOutputServiceOverride from '@codingame/monaco-vscode-output-service-override'
import getMarkersServiceOverride from '@codingame/monaco-vscode-markers-service-override'
import getSnippetsServiceOverride from '@codingame/monaco-vscode-snippets-service-override'
import getTerminalServiceOverride from '@codingame/monaco-vscode-terminal-service-override'
import getScmServiceOverride from '@codingame/monaco-vscode-scm-service-override'
import getOutlineServiceOverride from '@codingame/monaco-vscode-outline-service-override'
import getTimelineServiceOverride from '@codingame/monaco-vscode-timeline-service-override'
import getNotificationsServiceOverride from '@codingame/monaco-vscode-notifications-service-override'
import getDialogsServiceOverride from '@codingame/monaco-vscode-dialogs-service-override'
import getBulkEditServiceOverride from '@codingame/monaco-vscode-bulk-edit-service-override'
import getExtensionGalleryServiceOverride from '@codingame/monaco-vscode-extension-gallery-service-override'
import getViewTitleBarServiceOverride from '@codingame/monaco-vscode-view-title-bar-service-override'
import getViewStatusBarServiceOverride from '@codingame/monaco-vscode-view-status-bar-service-override'
import getAccessibilityServiceOverride from '@codingame/monaco-vscode-accessibility-service-override'
import getWorkingCopyServiceOverride from '@codingame/monaco-vscode-working-copy-service-override'
import getWorkspaceTrustServiceOverride from '@codingame/monaco-vscode-workspace-trust-service-override'
import getLogServiceOverride from '@codingame/monaco-vscode-log-service-override'
import getUserDataProfileServiceOverride from '@codingame/monaco-vscode-user-data-profile-service-override'
import getAuthenticationServiceOverride from '@codingame/monaco-vscode-authentication-service-override'
import getSecretStorageServiceOverride from '@codingame/monaco-vscode-secret-storage-service-override'
import getWelcomeServiceOverride from '@codingame/monaco-vscode-welcome-service-override'
import getWalkthroughServiceOverride from '@codingame/monaco-vscode-walkthrough-service-override'
import getEmmetServiceOverride from '@codingame/monaco-vscode-emmet-service-override'
import getMultiDiffEditorServiceOverride from '@codingame/monaco-vscode-multi-diff-editor-service-override'
import getLanguageDetectionWorkerServiceOverride from '@codingame/monaco-vscode-language-detection-worker-service-override'
import { MidumFileSystemProvider } from './midum-fs'
import { MidumTerminalBackend } from './terminal'
import { URI } from '@codingame/monaco-vscode-api/vscode/vs/base/common/uri'
import { host } from './paths'

// ── workers: `new Worker(new URL(...))` is what lets Vite bundle each worker; the class only records url+options ──────
class Worker { constructor(public url: URL | string, public options?: WorkerOptions) {} }

export function installWorkers(): void {
  const workers: Partial<Record<string, Worker>> = {
    editorWorkerService: new Worker(new URL('monaco-editor/esm/vs/editor/editor.worker.js', import.meta.url), { type: 'module' }),
    extensionHostWorkerMain: new Worker(new URL('@codingame/monaco-vscode-api/workers/extensionHost.worker', import.meta.url), { type: 'module' }),
    TextMateWorker: new Worker(new URL('@codingame/monaco-vscode-textmate-service-override/worker', import.meta.url), { type: 'module' }),
    OutputLinkDetectionWorker: new Worker(new URL('@codingame/monaco-vscode-output-service-override/worker', import.meta.url), { type: 'module' }),
    LanguageDetectionWorker: new Worker(new URL('@codingame/monaco-vscode-language-detection-worker-service-override/worker', import.meta.url), { type: 'module' }),
    LocalFileSearchWorker: new Worker(new URL('@codingame/monaco-vscode-search-service-override/worker', import.meta.url), { type: 'module' })
  }
  ;(window as any).MonacoEnvironment = {
    getWorkerUrl: (_: string, label: string) => workers[label]?.url.toString(),
    getWorkerOptions: (_: string, label: string) => workers[label]?.options
  }
}

export function buildServices(opts: { userRoot: string; userMap: any; webviewDomain: string; safe: boolean; onOpenWorkspace: (p: string) => void }) {
  // Safe mode: the worker extension host (hidden iframe on {uuid}.localhost) and the webview domain are skipped. Built-in
  // grammars/themes and Midum's own features run in the main thread and are unaffected; only marketplace extensions are lost.
  const domain = opts.safe ? undefined : opts.webviewDomain
  // user data (settings.json, keybindings.json, snippets, installed extensions) lives on disk next to Midum's own
  // storage, NOT in IndexedDB: Midum's local server picks a new port each launch, which would change the browser origin.
  registerCustomProvider('vscode-userdata', new MidumFileSystemProvider(opts.userMap, true) as any)
  registerFileSystemOverlay(10, new MidumFileSystemProvider() as any)
  void host; void URI
  return {
    ...getLogServiceOverride(),
    ...getExtensionsServiceOverride({ enableWorkerExtensionHost: !opts.safe, iframeAlternateDomain: domain }),
    ...getFilesServiceOverride(),
    ...getModelServiceOverride(),
    ...getNotificationsServiceOverride(),
    ...getDialogsServiceOverride(),
    ...getHostServiceOverride(),
    ...getConfigurationServiceOverride(),
    ...getKeybindingsServiceOverride(),
    ...getTextmateServiceOverride(),
    ...getThemeServiceOverride(),
    ...getLanguagesServiceOverride(),
    ...getPreferencesServiceOverride(),
    ...getViewTitleBarServiceOverride(),
    ...getViewStatusBarServiceOverride(),
    ...getOutputServiceOverride(),
    ...getTerminalServiceOverride(new MidumTerminalBackend()),
    ...getAccessibilityServiceOverride(),
    ...getLanguageDetectionWorkerServiceOverride(),
    ...getStorageServiceOverride({ fallbackOverride: { 'workbench.activity.showAccounts': false } }),
    ...getLifecycleServiceOverride(),
    ...getEnvironmentServiceOverride(),
    ...getWorkspaceTrustServiceOverride(),
    ...getWorkingCopyServiceOverride(),
    ...getScmServiceOverride(),
    ...getUserDataProfileServiceOverride(),
    ...getAuthenticationServiceOverride(),
    ...getSecretStorageServiceOverride(),
    ...getMarkersServiceOverride(),
    ...getSnippetsServiceOverride(),
    ...getOutlineServiceOverride(),
    ...getTimelineServiceOverride(),
    ...getBulkEditServiceOverride(),
    ...getExplorerServiceOverride(),
    ...getSearchServiceOverride(),
    ...getQuickAccessServiceOverride({ isKeybindingConfigurationVisible: () => true, shouldUseGlobalPicker: () => true }),
    ...getExtensionGalleryServiceOverride({ webOnly: false }),
    ...getWelcomeServiceOverride(),
    ...getWalkthroughServiceOverride(),
    ...getEmmetServiceOverride(),
    ...getMultiDiffEditorServiceOverride(),
    ...getWorkbenchServiceOverride(undefined, domain)
  }
}
