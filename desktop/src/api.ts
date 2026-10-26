export type Result<T> = { ok: true; data: T } | { ok: false; error: { code: string; message: string } };
export type AuthType = 'password' | 'private-key' | 'agent' | 'tailscale';
export interface Target { host: string; port: number; username: string }
export interface Server extends Target {
  id: string; name: string; fingerprint: string; authType: AuthType;
  remembered: boolean; authenticated: boolean; updatedAt: number;
  deployment?: { job: string; checksum: string; previousChecksum?: string; version: string; startedAt: number; imageOptions?: ImageOptions };
  lifecycle?: LifecycleRecord;
}
export interface Application {
  id: string; serverId: string; appId: 'gaoji' | 'static-site'; name: string; legacy?: boolean;
  deployment?: Server['deployment']; lifecycle?: LifecycleRecord;
}
export interface ApplicationDefinition {
  id: Application['appId']; name: string; version: string; systems: string[];
  inputs: string[]; dataRoot: string; minimumFreeBytes: number; access: string;
}
export interface ApplicationStatus {
  state: 'draft' | 'working' | 'ready' | 'setup' | 'stopped' | 'retained' | 'removed' | 'attention' | 'unknown';
  label: string; message: string; checkedAt: number;
}
export interface Installation {
  rejected?: boolean;
  job: string; stage: string; completed: string[]; message: string;
  status: 'queued' | 'waiting' | 'running' | 'failed' | 'awaiting_setup' | 'unconfirmed' | 'completed';
  created_at?: number; updated_at?: number;
  progress?: { detail: string; elapsed_seconds: number; quiet_seconds: number } | null;
}
export interface Probe {
  os: string; version: string; label: string; arch: string; uid: number; freeBytes: number;
  systemd: boolean; dockerPath: string; nixPath: string;
  privilege: 'root' | 'passwordless' | 'password-required' | 'unavailable';
}
export interface Inspection {
  probe: Probe;
  plan: { eligible: boolean; adapter: string; blockers: string[]; warnings: string[];
    steps: { id: string; title: string }[]; boundaries: string[] };
}
export interface ConnectRequest extends Target {
  id?: string; name: string; remember: boolean; fingerprint: string;
  authentication: { type: AuthType; password?: string; keySelection?: string; passphrase?: string };
}
export interface AppUpdateState {
  phase: 'idle' | 'checking' | 'no_release' | 'up_to_date' | 'available' | 'downloading' | 'ready' | 'error';
  currentVersion: string; targetVersion: string; checkedAt: string; notes: string;
  mode: 'native' | 'installer'; bytes: number; total: number; percent: number; message: string;
}
export interface DesktopApi {
  appUpdateStatus(): Promise<Result<AppUpdateState>>;
  checkAppUpdate(): Promise<Result<AppUpdateState>>;
  downloadAppUpdate(): Promise<Result<AppUpdateState>>;
  installAppUpdate(): Promise<Result<AppUpdateState>>;
  onAppUpdateRequested(callback: () => void): () => void;
  localInspect(): Promise<Result<LocalInspection>>;
  localStart(options?: ImageOptions): Promise<Result<LocalProgress>>;
  localStatus(): Promise<Result<LocalProgress | null>>;
  localDocker(action: 'open' | 'download'): Promise<Result<boolean>>;
  info(): Promise<Result<{ version: string; platform: string; secureStorage: boolean; runtimeAvailable: boolean }>>;
  servers(): Promise<Result<Server[]>>;
  applicationCatalog(): Promise<Result<ApplicationDefinition[]>>;
  applications(id: string): Promise<Result<Application[]>>;
  applicationStatus(id: string): Promise<Result<ApplicationStatus>>;
  renameApplication(request: { id: string; name: string }): Promise<Result<Application>>;
  removeApplicationDraft(request: { id: string; confirm: true }): Promise<Result<Application[]>>;
  createApplication(request: { id: string; appId: Application['appId']; name: string }): Promise<Result<Application>>;
  selectWebsite(): Promise<Result<WebsiteSelection | null>>;
  websiteStatus(id: string): Promise<Result<WebsiteStatus>>;
  websiteSubmit(request: { id: string; operation: WebsiteOperation; revision: number; selection?: string; backupId?: string; confirm: true; eraseInstanceId?: string; password?: string }): Promise<Result<Installation>>;
  websiteResume(request: { id: string; password?: string }): Promise<Result<Installation>>;
  websiteBackupExport(request: { id: string; backupId: string; password: string; sudoPassword?: string }): Promise<Result<{ exported?: boolean; cancelled?: boolean }>>;
  websiteBackupImport(request: { password: string }): Promise<Result<WebsiteSelection | null>>;
  openWebsite(id: string): Promise<Result<boolean>>;
  exportDiagnostics(report: unknown): Promise<Result<{ saved: boolean }>>;
  selectKey(): Promise<Result<{ selection: string; name: string } | null>>;
  discover(target: Target): Promise<Result<Target & { fingerprint: string }>>;
  connect(request: ConnectRequest): Promise<Result<Inspection & { server: Server }>>;
  inspect(id: string): Promise<Result<Inspection>>;
  install(request: { id: string; password?: string; imageOptions?: ImageOptions }): Promise<Result<Installation>>;
  installStatus(id: string): Promise<Result<Installation | null>>;
  updatePlan(id: string): Promise<Result<UpdatePlan>>;
  updateStatus(id: string): Promise<Result<VersionTask | null>>;
  update(request: { id: string; kind: 'update' | 'rollback'; expectedChecksum: string; revision: number; rollbackId?: string; confirm: true; password?: string }): Promise<Result<VersionTask>>;
  resumeUpdate(request: { id: string; password?: string }): Promise<Result<VersionTask>>;
  lifecyclePrepare(request: { id: string; kind: LifecycleKind; password?: string }): Promise<Result<LifecycleTask>>;
  lifecycleSubmit(request: { id: string; operationId: string; confirm: true; password?: string; eraseInstanceId?: string }): Promise<Result<LifecycleTask>>;
  lifecycleStatus(id: string): Promise<Result<LifecycleTask | null>>;
  selectBackup(): Promise<Result<{ selection: string; name: string; bytes: number } | null>>;
  exportBackup(request: { id: string; backupId: string; password: string }): Promise<Result<{ exported?: boolean; cancelled?: boolean }>>;
  importBackup(request: { id: string; selection: string; password: string }): Promise<Result<{ imported: boolean; id: string }>>;
  backupTransferStatus(id: string): Promise<Result<BackupTransferStatus | null>>;
  cancelBackupTransfer(id: string): Promise<Result<boolean>>;
  instance(request: { id: string; action: InstanceAction; payload?: unknown }): Promise<Result<unknown>>;
  openConsole(id: string): Promise<Result<boolean>>;
  forget(id: string): Promise<Result<Server[]>>;
}
export type WebsiteOperation = 'install' | 'update' | 'start' | 'stop' | 'rollback' | 'uninstall' | 'purge' | 'backup' | 'restore' | 'delete-backup';
export interface WebsiteBackup { id: string; created_at: number; bytes: number; files: number; sha256: string }
export interface WebsiteSelection { selection: string; checksum: string; name: string; bytes: number; files: number }
export interface WebsiteVersion { checksum: string; port: number; bytes: number; verified_at: number }
export interface WebsiteStatus {
  task: Installation | null;
  health: 'ready' | 'unknown' | 'unhealthy' | 'stopped';
  site: null | { id: string; revision: number; status: 'not_installed' | 'running' | 'stopped' | 'retained' | 'removed';
    operation?: { status: string; safe_to_replace?: boolean; request?: { operation: WebsiteOperation } };
    cleanup?: { message: string; released_bytes?: number; checked_at: number };
    backups?: WebsiteBackup[];
    current: WebsiteVersion | null; previous: WebsiteVersion | null; data_directory: string; data_bytes: number };
}
export type ImageOptions = { mode: 'auto' | 'prebuilt' | 'source'; source: 'official' | 'tuna' };
export interface LocalProgress { status: string; stage: string; message: string; completed?: string[];
  stage_started_at?: number; updated_at?: number; progress?: Installation['progress'] }
export interface LocalInspection {
  installed: boolean; ready: boolean; recorded: boolean; blockers: string[];
  freeBytes: number; memoryBytes: number; platform: string; volume: string; status: LocalProgress | null;
}
export type LifecycleKind = 'uninstall' | 'reinstall' | 'purge';
export type ServiceState = 'installed' | 'retained' | 'removing' | 'restoring' | 'purging' | 'removed' | 'unknown';
export interface LifecycleRecord {
  id: string; job: string; instance_id: string; kind: LifecycleKind; created_at: number;
  service_state: ServiceState; dispatched: boolean; settled: boolean;
}
export interface LifecycleTask {
  id: string; job: string; instance_id: string; kind: LifecycleKind; created_at: number; updated_at?: number;
  service_state: ServiceState; dispatched: boolean; status: 'preview' | 'queued' | 'running' | 'failed' | 'completed' | 'unconfirmed' | 'interrupted';
  stage: string; message: string; resumable: boolean; units?: string[]; data_directory?: string;
  data_bytes?: number; delete_resources?: Record<'containers' | 'volumes' | 'networks', { id: string; name: string }[]>;
}
export interface VersionTask {
  id: string; job: string; target_checksum: string; status: string; stage: string; message: string;
  updated_at?: number; resumable?: boolean; finished?: boolean;
}
export interface UpdatePlan {
  current: { checksum: string; version: string; revision: number; maintenance_pending: boolean; legacy?: boolean;
    operation?: { id: string; kind: string; status: string; stage: string } | null };
  available: { version: string; checksum: string }; canUpdate: boolean;
  rollbacks: { id: string; target_checksum: string; version: string; created_at: number }[];
}
export type InstanceAction = 'status' | 'services' | 'qqStatus' | 'qqRefresh' | 'save' | 'modelTest' | 'modelList' | 'embeddingTest' | 'clusterTest' | 'matrixTest' | 'imessageTest' | 'operation' | 'backups'
  | 'storageStatus' | 'storageScan' | 'storagePlan' | 'storagePolicy' | 'storageCleanup';

export type StoragePolicy = { enabled: boolean; keep_backups: number; keep_restore_points: number; min_age_days: number };
export type StorageItem = { id: string; kind: 'backup' | 'restore_point'; created_at: number; bytes: number; paths: string[]; protected: string; eligible: boolean };
export type StorageScan = { created_at: number; live: Array<{ name: string; bytes: number | null; complete: boolean }>; items: StorageItem[]; warnings: string[]; candidate_bytes: number };
export type StorageJob = { id: string; status: string; message: string; updated_at: number; items_done: number; estimated_bytes: number; free_before: number; free_after?: number; resumable: boolean };
export type CapacityStatus = {
  state: 'ready' | 'blocked' | 'unknown'; reason: string; complete?: boolean; sampled_at?: number;
  policy?: { max_data_gib: number; reserve_free_gib: number }; used_bytes?: number;
  data_bytes?: number | null; sandbox_bytes?: number | null; writable_bytes?: number | null;
  disks?: { name: string; free_bytes: number; total_bytes: number }[];
};
export type StorageStatus = { revision: number; policy: StoragePolicy; disk: { total_bytes: number; free_bytes: number }; scan: StorageScan | null; job: StorageJob | null; capacity?: CapacityStatus };
export type StoragePlan = { id: string; created_at: number; items: StorageItem[]; estimated_bytes: number; warnings: string[] };
export type OperationKind = 'activate' | 'stop' | 'backup' | 'restore' | 'recover' | 'resume_qq';
export interface Backup { id: string; created_at: number; bytes: number; sha256: string; files: number; revision: number; imported?: boolean; source_instance_id?: string }
export interface BackupTransferStatus { status: string; phase: string; bytes: number; total: number; name: string; message: string }
export interface ModelProfile {
  id: string; label: string; provider: string; protocol: 'openai-chat' | 'anthropic-messages';
  base_url: string; model: string; api_key_required: boolean; tools: boolean; vision: boolean;
  reasoning_effort: string; max_input_tokens: number; max_output_tokens: number; fallbacks: string[]; has_key?: boolean;
  thinking?: 'auto' | 'enabled' | 'disabled';
}
export interface InstanceSettings {
  name: string; bot_qq: string; admin_qq: string; group_ids: string[]; allow_all_groups: boolean;
  system_prompt: string; models: ModelProfile[]; default_model: string; vision_model: string; features: Record<string, boolean>;
  components?: ComponentSettings;
}
export interface ComponentSettings {
  capacity: { max_data_gib: number; reserve_free_gib: number };
  search: { max_results: number; timeout_seconds: number };
  voice: { name: string; rate: string; pitch: string; max_chars: number };
  embedding: { base_url: string; model: string; dimensions: number; api_key_required: boolean };
  cluster: { base_url: string; timeout_seconds: number; allowed_groups: string[]; log_allowed_groups: string[] };
  matrix: { enabled: boolean; base_url: string; user_id: string; routes: { id: string; qq_group: string; room_id: string }[] };
  imessage: { enabled: boolean; base_url: string; bot_handle: string; routes: { id: string; qq_group: string; chat_guid: string }[] };
  historian: { profile: string };
  alerts: { base_url: string; group_id: string };
  video: { max_duration_minutes: number; max_download_mb: number; frame_count: number; threads: number; timeout_seconds: number };
}
export interface InstanceStatus {
  instance: { id: string; revision: number; settings: InstanceSettings; component_keys_present?: string[] };
  desired: { bot_enabled: boolean; revision?: number };
  maintenance_pending?: boolean;
  component_requirements?: Record<string, string>;
  video_model?: { model: string; total_bytes: number; downloaded_bytes: number; state: string };
  sandbox_runtime?: { supported: boolean; state: string; bytes: number };
  cluster_configuration?: { schema: number };
  matrix_configuration?: { schema: number };
  imessage_configuration?: { schema: number };
  capacity_configuration?: { schema: number };
  model_key_references?: { schema: number };
  qq_handover?: { schema: number; state: 'pending' | 'approved'; operation_id: string; source_instance_id: string; target_instance_id: string } | null;
  operation: { id: string; kind: string; status: string; stage: string; message: string } | null;
}
export interface QQStatus { online: boolean; qq: string; nickname: string; phase: string; qrcode: string; offline: boolean; has_error: boolean }
export async function instanceCall<T>(id: string, action: InstanceAction, payload?: unknown): Promise<T> {
  return await unwrap(desktop?.instance({ id, action, payload })) as T;
}
declare global { interface Window { gaojiDesktop?: DesktopApi } }
export const desktop = window.gaojiDesktop;
export class DesktopError extends Error {
  constructor(public code: string, message: string) { super(message); }
}
export async function unwrap<T>(promise: Promise<Result<T>> | undefined): Promise<T> {
  if (!promise) throw new Error('请在高级桌面 App 内连接服务器');
  const result = await promise;
  if (!result.ok) throw new DesktopError(result.error.code, result.error.message);
  return result.data;
}
