const now = Date.now() / 1000

export const demoStatus = {
  online: true,
  compute_enabled: true,
  runtime_changed_at: now,
  hostname: 'WIN-9C7F2K3QH7R',
  updated_at: now,
  controller_synced_at: now - 20,
  uptime_seconds: 223200,
  build_version: 'v2.1.0',
  software_update: { current_version: 'v2.1.0', latest_version: 'v2.2.0', version_status: 'update_available', update_available: true, release_url: 'https://github.com/jeron-lgy/Moviemuse/releases', checked_at: now },
  hardware: {
    cpu_usage_percent: 18,
    memory: { total_bytes: 68719476736, available_bytes: 46600395162, used_percent: 32 },
    gpus: [{ name: 'NVIDIA GeForce RTX 5090', memory_total_mb: 32768, memory_used_mb: 8601, utilization_percent: 72, temperature_c: 57, driver: '576.40' }]
  },
  storage: { total_bytes: 511852429312, used_bytes: 41553879040, free_bytes: 470298550272, model_bytes: 41553879040 },
  model_recommendation: { gpu_detected: true, gpu_name: 'NVIDIA GeForce RTX 5090', memory_total_mb: 32768, recommended_model: 'large-v3-turbo', recommended_label: 'large-v3-turbo', reason: '显存充足，推荐使用 large-v3-turbo 兼顾识别质量与速度', tier: 'high' },
  gpu_runtime: { status: 'ready', source: 'local', missing: [], restart_required: false },
  readiness: {
    status: 'ready', ready: true, summary: '环境正常，可以接收任务', blocking_count: 0, warning_count: 0, checked_at: now - 8,
    next_action: { id: 'none', label: '已准备就绪' },
    checks: [
      { id: 'compute', label: '算力开关', status: 'pass', summary: '正在接收新任务' },
      { id: 'controller', label: '控制端配置', status: 'pass', summary: '已接收 MovieMuse 配置' },
      { id: 'gpu', label: 'NVIDIA 显卡', status: 'pass', summary: 'GeForce RTX 5090' },
      { id: 'gpu_runtime', label: 'CUDA / cuDNN', status: 'pass', summary: 'GPU 运行环境可用' },
      { id: 'ffmpeg', label: 'FFmpeg / NVENC', status: 'pass', summary: 'AV1 NVENC 可用' },
      { id: 'path_map', label: '路径映射', status: 'pass', summary: '已配置 1 个 Windows 映射' },
      { id: 'media_read', label: '媒体目录', status: 'pass', summary: '共享目录可读取' },
      { id: 'media_write', label: '输出权限', status: 'pass', summary: '共享目录可创建输出文件' },
      { id: 'model', label: 'Whisper 模型', status: 'pass', summary: 'large-v3-turbo 已验证' }
    ]
  },
  effective_config: { model: 'large-v3-turbo', subtitle_workers: 1, translation_workers: 2, transcode_workers: 1, source: 'controller' },
  counts: { running: 2, waiting: 3, completed_today: 18, failed_today: 1, active: 5 },
  last_error: '',
  activities: [
    { id: 'a1', name: 'S01E03.The.Passage.1080p.BluRay.x264.mkv', path: 'Z:\\Media\\TV\\The.Passage\\S01E03.The.Passage.1080p.BluRay.x264.mkv', type: 'whisper', type_label: 'Whisper 识别', model: 'large-v3', status: 'running', progress: 68, message: '识别中 · 约 3 分钟后完成', created_at: now - 1600, started_at: now - 1127, updated_at: now - 2 },
    { id: 'a2', name: 'S01E03.The.Passage.1080p.BluRay.x264.srt', path: 'Z:\\Media\\TV\\The.Passage\\S01E03.The.Passage.1080p.BluRay.x264.srt', type: 'translation', type_label: 'DeepSeek 翻译', model: 'DeepSeek-V3', status: 'queued', progress: 0, message: '排队中 · 前方 1 个任务', created_at: now - 540, updated_at: now - 5 },
    { id: 'a3', name: 'The.Wandering.Earth.2.2023.2160p.WEB-DL.av1.mkv', path: 'Z:\\Media\\Movies\\The.Wandering.Earth.2.2023.2160p.WEB-DL.av1.mkv', type: 'transcode', type_label: 'AV1 转码', model: 'AV1 10-bit', status: 'running', progress: 41, message: '转码中 · 预计 12 分钟后完成', created_at: now - 900, started_at: now - 723, updated_at: now - 4 },
    { id: 'a4', name: 'S01E02.The.Passage.1080p.BluRay.x264.srt', path: 'Z:\\Media\\TV\\The.Passage\\S01E02.The.Passage.1080p.BluRay.x264.srt', type: 'whisper', type_label: 'Whisper 识别', model: 'large-v3', status: 'completed', progress: 100, message: '完成', created_at: now - 4200, started_at: now - 3900, finished_at: now - 3500, updated_at: now - 3500 },
    { id: 'a5', name: 'The.Wandering.Earth.2.2023.2160p.WEB-DL.av1.mkv', path: 'Z:\\Media\\Movies\\The.Wandering.Earth.2.2023.2160p.WEB-DL.av1.mkv', type: 'transcode', type_label: 'AV1 转码', model: 'AV1', status: 'completed', progress: 100, message: '完成', created_at: now - 8500, started_at: now - 8000, finished_at: now - 5200, updated_at: now - 5200 },
    { id: 'a6', name: 'S01E01.The.Passage.1080p.BluRay.x264.srt', path: 'Z:\\Media\\TV\\The.Passage\\S01E01.The.Passage.1080p.BluRay.x264.srt', type: 'whisper', type_label: 'Whisper 识别', model: 'large-v3', status: 'failed', progress: 0, message: '模型加载失败', error: '模型文件校验失败：文件哈希值不匹配（期望 8f3a2d1c，实际 1a2b3c4d）', created_at: now - 6600, started_at: now - 6582, finished_at: now - 6561, updated_at: now - 6561 }
  ]
}

export const demoModels = {
  active_model: 'large-v3-turbo',
  storage: demoStatus.storage,
  recommendation: demoStatus.model_recommendation,
  downloads: [{ id: 'download1', model_id: 'large-v3', state: 'downloading', progress: 63, downloaded_bytes: 1.9 * 1024 ** 3, total_bytes: 3 * 1024 ** 3, speed_bytes_per_second: 28.6 * 1024 ** 2, eta_seconds: 40, current_file: 'model.bin', files_completed: 4, files_total: 9 }],
  models: [
    { id: 'large-v3-turbo', label: 'large-v3-turbo', installed: true, verified: true, active: true, actual_size_bytes: 2.44 * 1024 ** 3, status: 'installed', modified_at: now - 1200, local_revision: '8f3a2d1c90', latest_revision: '9b4c3e2a11', version_status: 'update_available', version_checked_at: now },
    { id: 'large-v3', label: 'large-v3', installed: true, verified: true, active: false, actual_size_bytes: 3 * 1024 ** 3, status: 'installed', modified_at: now - 56000, local_revision: '4ac11b82ef', latest_revision: '4ac11b82ef', version_status: 'up_to_date', version_checked_at: now },
    { id: 'medium', label: 'medium', installed: false, available: true, verified: false, active: false, actual_size_bytes: 0, size_bytes: 1.42 * 1024 ** 3, status: 'available', version_status: 'not_installed' },
    { id: 'small', label: 'small', installed: false, verified: false, active: false, actual_size_bytes: 0, size_bytes: 244 * 1024 ** 2, status: 'not_downloaded', version_status: 'not_installed' },
    { id: 'base', label: 'base', installed: false, verified: false, active: false, actual_size_bytes: 0, size_bytes: 142 * 1024 ** 2, status: 'not_downloaded', version_status: 'not_installed' }
  ]
}
