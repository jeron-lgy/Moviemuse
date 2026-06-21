(function () {
  document.querySelectorAll("[data-confirm]").forEach((trigger) => {
    trigger.addEventListener("click", (event) => {
      if (!window.confirm(trigger.dataset.confirm || "确认执行这个文件操作？")) {
        event.preventDefault();
      }
    });
  });

  const scanStatus = document.querySelector("[data-scan-running]");

  async function watchScanStatus() {
    if (!scanStatus) return;
    try {
      const response = await fetch("/api/scan");
      if (!response.ok) return;
      const payload = await response.json();
      const progress = document.querySelector("#scanProgress");
      const progressText = document.querySelector("#scanProgressText");
      const currentPath = document.querySelector("#scanCurrentPath");
      if (progress) progress.value = payload.progress || 0;
      if (progressText) progressText.textContent = `${payload.processed_files || 0} / ${payload.scan_total_files || 0}`;
      if (currentPath) currentPath.textContent = payload.current_path || "正在枚举视频文件...";
      if (payload.status === "completed" || payload.status === "failed") {
        window.location.reload();
      }
    } catch (error) {
      // Keep the page usable while the scan endpoint is temporarily unavailable.
    }
  }

  if (scanStatus) {
    window.setInterval(watchScanStatus, 3000);
  }

  const subtitleJobs = document.querySelector("#subtitleJobs");
  const refreshSubtitleJobs = document.querySelector("#refreshSubtitleJobs");
  const refreshBackendStatus = document.querySelector("#refreshBackendStatus");
  let subtitleBackendOnline = true;

  function renderSubtitleJobs(jobs) {
    if (!subtitleJobs) return;
    if (!jobs.length) {
      subtitleJobs.innerHTML = '<div class="empty">还没有字幕任务。</div>';
      return;
    }
    subtitleJobs.innerHTML = jobs.map((job) => {
      const percent = Math.round((job.progress || 0) * 100);
      const files = [
        job.original_srt ? `<a href="/subtitles/jobs/${job.id}/files/original_srt">原文 SRT</a>` : "",
        job.translated_srt ? `<a href="/subtitles/jobs/${job.id}/files/translated_srt">翻译 SRT</a>` : "",
        job.bilingual_srt ? `<a href="/subtitles/jobs/${job.id}/files/bilingual_srt">双语 SRT</a>` : "",
      ].join("");
      return `
        <article class="subtitle-job" data-job-id="${job.id}">
          <div class="job-main">
            <strong>${escapeHtml(job.video_path || "")}</strong>
            <span>${escapeHtml(job.message || "")}${job.error ? "：" + escapeHtml(job.error) : ""}</span>
            <small>${escapeHtml(job.model || "")} / ${escapeHtml(job.source_language || "auto")} => ${escapeHtml(job.target_language || "")}</small>
          </div>
          <div class="job-state">
            <em class="status-${escapeHtml(job.status || "")}">${escapeHtml(job.status || "")}</em>
            <progress value="${job.progress || 0}" max="1"></progress>
            <span>${percent}%</span>
          </div>
          <div class="job-files">${files}</div>
        </article>`;
    }).join("");
  }

  function renderCurrentJobs(jobs) {
    const currentJobs = document.querySelector("#currentBackendJobs");
    if (!currentJobs) return;
    const active = (jobs || []).filter((job) => ["queued", "running"].includes(job.status));
    if (!active.length) {
      currentJobs.innerHTML = '<div class="empty compact">当前没有运行中的字幕任务。</div>';
      return;
    }
    currentJobs.innerHTML = active.map((job) => {
      const percent = Math.round((job.progress || 0) * 100);
      return `
        <article class="current-job">
          <strong>${escapeHtml(job.video_path || "")}</strong>
          <span>${escapeHtml(job.message || "")}${job.error ? "：" + escapeHtml(job.error) : ""}</span>
          <progress value="${job.progress || 0}" max="1"></progress>
          <small>${percent}% · ${escapeHtml(job.status || "")}</small>
        </article>`;
    }).join("");
  }

  function renderPathMapChain(pathPreview) {
    const wraps = document.querySelectorAll("#pathMapChain");
    if (!wraps.length || !pathPreview) return;
    const consolePairs = (pathPreview.console_pairs || [])
      .map((pair) => `<code>${escapeHtml(pair[0])} => ${escapeHtml(pair[1])}</code>`)
      .join("") || "<code>未配置 SUBTITLE_PROXY_PATH_MAP</code>";
    const backendPairs = (pathPreview.backend_pairs || [])
      .map((pair) => `<code>${escapeHtml(pair[0])} => ${escapeHtml(pair[1])}</code>`)
      .join("") || "<code>终端不再额外映射</code>";
    const html = `
      <label>控制台收到</label>
      <code>${escapeHtml(pathPreview.input || "")}</code>
      <label>控制台转发</label>
      <code>${escapeHtml(pathPreview.console_output || "")}</code>
      <label>终端最终读取</label>
      <code>${escapeHtml(pathPreview.backend_output || "")}</code>
      <label>控制台映射</label>
      ${consolePairs}
      <label>终端映射</label>
      ${backendPairs}`;
    wraps.forEach((wrap) => {
      wrap.innerHTML = html;
    });
  }

  function renderBackendStatus(status) {
    const terminal = document.querySelector("#terminalStatus");
    if (!terminal && !document.querySelector("#backendStateText")) return;
    const setTextAll = (selector, value) => {
      document.querySelectorAll(selector).forEach((item) => {
        item.textContent = value;
      });
    };
    subtitleBackendOnline = !!status.online;
    terminal?.classList.toggle("online", subtitleBackendOnline);
    terminal?.classList.toggle("offline", !subtitleBackendOnline);
    const pill = terminal?.querySelector(".status-pill");
    if (pill) pill.textContent = subtitleBackendOnline ? "在线" : "离线";
    const message = document.querySelector("#terminalMessage");
    if (message) {
      message.textContent = subtitleBackendOnline
        ? `已连接 ${status.backend_url || "本地服务"}，可以提交 Whisper 任务。`
        : `无法连接 ${status.backend_url || "本地服务"}：${status.error || "后端未启动"}`;
    }
    const sideStatus = document.querySelector("#sideBackendStatus");
    if (sideStatus) sideStatus.textContent = subtitleBackendOnline ? "在线" : "离线";
    const sideText = document.querySelector("#sideBackendText");
    if (sideText) sideText.textContent = status.backend_url || "本地模式";
    setTextAll("#backendStateText", subtitleBackendOnline ? "在线" : "离线");
    setTextAll("#backendUrlText", status.backend_url || "本地模式");
    setTextAll("#backendCpuText", status.hardware?.cpu || "未连接");
    const memory = status.hardware?.memory;
    setTextAll("#backendMemoryText", memory ? `${memory.label || ""} · ${memory.used_percent || 0}%` : "未连接");
    const gpu = status.hardware?.gpus?.[0];
    setTextAll("#backendGpuText", gpu?.label || "未检测");
    const settings = status.settings || {};
    const detail = [settings.default_model, settings.device, settings.compute_type].filter(Boolean).join(" / ");
    setTextAll("#backendWhisperText", detail || "未连接");
    setTextAll("#backendErrorText", subtitleBackendOnline ? "" : (status.error || "后端暂不可用"));
    const activeJobCount = document.querySelector("#activeJobCount");
    if (activeJobCount) activeJobCount.textContent = String(status.jobs?.active || 0);
    const totalJobCount = document.querySelector("#totalJobCount");
    if (totalJobCount) totalJobCount.textContent = String(status.jobs?.total || 0);
    renderCurrentJobs(status.jobs?.items || []);
    renderPathMapChain(status.path_preview);
  }

  async function loadBackendStatus() {
    if (!document.querySelector("#backendStateText") && !document.querySelector("#terminalStatus")) return null;
    try {
      const response = await fetch("/api/subtitle/backend/status");
      if (!response.ok) return null;
      const payload = await response.json();
      renderBackendStatus(payload);
      return payload;
    } catch (error) {
      renderBackendStatus({ online: false, error: "控制台状态接口暂不可用", jobs: { total: 0, active: 0, items: [] } });
      return null;
    }
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  async function loadSubtitleJobs() {
    if (!subtitleJobs) return;
    const backendStatus = await loadBackendStatus();
    if (backendStatus && !backendStatus.online) {
      subtitleJobs.innerHTML = `<div class="warning">无法刷新任务：${escapeHtml(backendStatus.error || "后端暂不可用")}</div>`;
      return;
    }
    try {
      const response = await fetch("/api/subtitle/jobs");
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const detail = payload.detail || "后端暂不可用";
        subtitleJobs.innerHTML = `<div class="warning">无法刷新任务：${escapeHtml(detail)}</div>`;
        return;
      }
      const payload = await response.json();
      renderSubtitleJobs(payload.jobs || []);
    } catch (error) {
      subtitleJobs.innerHTML = '<div class="warning">无法刷新任务：后端暂不可用。</div>';
    }
  }

  refreshBackendStatus?.addEventListener("click", async () => {
    await loadBackendStatus();
  });
  document.querySelectorAll("[data-open-dialog]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.provider) {
        const radio = document.querySelector(`#translateDialog input[name="default_translate_backend"][value="${button.dataset.provider}"]`);
        if (radio) radio.checked = true;
      }
      const dialog = document.getElementById(button.dataset.openDialog || "");
      if (dialog?.showModal) dialog.showModal();
    });
  });
  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => {
      document.getElementById(button.dataset.closeDialog || "")?.close();
    });
  });
  document.querySelectorAll(".config-dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  });
  const testBackendConnection = document.querySelector("#testBackendConnection");
  testBackendConnection?.addEventListener("click", async () => {
    const result = document.querySelector("#backendConnectionResult");
    const urlInput = document.querySelector("#subtitleBackendUrl");
    const tokenInput = document.querySelector("#subtitleBackendToken");
    if (result) result.textContent = "正在测试连接...";
    const body = new FormData();
    body.set("subtitle_backend_url", urlInput?.value || "");
    body.set("subtitle_backend_token", tokenInput?.value || "");
    try {
      const response = await fetch("/api/subtitle/backend/test", { method: "POST", body });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (result) result.textContent = payload.detail || "连接失败";
        return;
      }
      if (result) result.textContent = "连接成功，可以保存这个地址。";
      renderBackendStatus(payload);
    } catch (error) {
      if (result) result.textContent = "连接失败，请检查 IP、端口和 Windows 防火墙。";
    }
  });
  refreshSubtitleJobs?.addEventListener("click", loadSubtitleJobs);
  if (subtitleJobs) {
    loadSubtitleJobs();
    window.setInterval(loadSubtitleJobs, 4000);
  } else if (document.querySelector("#backendStateText") || document.querySelector("#terminalStatus")) {
    loadBackendStatus();
    window.setInterval(loadBackendStatus, 4000);
  }

  const groupWrap = document.querySelector("#groups");
  const ruleBuilder = document.querySelector("#ruleBuilder");
  const enabledAutoRules = new Set();

  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => {
      groupWrap?.classList.toggle("cover-view", button.dataset.view === "cover");
      document.querySelectorAll("[data-view]").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
    });
  });

  function groupFiles(group) {
    return Array.from(group.querySelectorAll(".file-row"));
  }

  function groupStats(group) {
    const rows = groupFiles(group).filter((row) => row.dataset.ignored !== "true");
    return {
      kind: group.dataset.groupKind || "duplicate",
      has4k: rows.some((row) => row.dataset.resolution === "4K"),
      hasSubtitle: rows.some((row) => row.dataset.subtitleKind !== "none"),
      hasUncensored: rows.some((row) => row.dataset.uncensored === "true"),
    };
  }

  function isLowPriority(row) {
    if (row.dataset.ignored === "true") return false;
    return row.dataset.resolution !== "4K"
      && row.dataset.subtitleKind === "none"
      && row.dataset.uncensored !== "true";
  }

  function matchesMoveStrategy(group, row) {
    const stats = groupStats(group);
    if (stats.kind !== "duplicate") return false;
    const hasBetterVersion = stats.has4k || stats.hasSubtitle || stats.hasUncensored;
    return hasBetterVersion && isLowPriority(row);
  }

  function matchesSubtitleStrategy(group, row) {
    if (row.dataset.ignored === "true") return false;
    return row.dataset.subtitleKind === "none" && row.dataset.uncensored !== "true";
  }

  function matchedRowsFor(ruleSet) {
    const matched = new Map();
    if (!groupWrap) return matched;
    const groups = Array.from(groupWrap.querySelectorAll(".scan-group"));

    groups.forEach((group) => {
      groupFiles(group).forEach((row) => {
        const path = row.dataset.path;
        if (!path) return;
        const isMatch = ruleSet === "move"
          ? matchesMoveStrategy(group, row)
          : matchesSubtitleStrategy(group, row);
        if (isMatch) matched.set(path, row);
      });
    });
    return matched;
  }

  function fillForm(formId, paths) {
    const form = document.querySelector(`#${formId}`);
    if (!form) return;
    form.innerHTML = "";
    paths.forEach((path) => {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "paths";
      input.value = path;
      form.appendChild(input);
    });
  }

  function setRuleCount(ruleSet, count) {
    document.querySelectorAll(`[data-rule-count="${ruleSet}"]`).forEach((target) => {
      target.replaceChildren(String(count));
    });
  }

  function updateRuleMatches() {
    const moveMatches = enabledAutoRules.has("move") ? matchedRowsFor("move") : new Map();
    const subtitleMatches = enabledAutoRules.has("subtitle") ? matchedRowsFor("subtitle") : new Map();
    document.querySelectorAll('[data-manual-action="move"]:checked').forEach((check) => {
      const row = check.closest(".file-row");
      if (row?.dataset.path) moveMatches.set(row.dataset.path, row);
    });
    moveMatches.forEach((_, path) => subtitleMatches.delete(path));
    document.querySelectorAll('[data-manual-action="subtitle"]:checked').forEach((check) => {
      const row = check.closest(".file-row");
      if (row?.dataset.path) subtitleMatches.set(row.dataset.path, row);
    });
    document.querySelectorAll(".file-row").forEach((row) => {
      const manualMove = row.querySelector('[data-manual-action="move"]')?.checked || false;
      const manualSubtitle = row.querySelector('[data-manual-action="subtitle"]')?.checked || false;
      const moveHit = moveMatches.has(row.dataset.path);
      const subtitleHit = subtitleMatches.has(row.dataset.path);
      row.classList.toggle("move-hit", moveHit);
      row.classList.toggle("subtitle-hit", subtitleHit);
      row.classList.toggle("manual-hit", manualMove || manualSubtitle);
      if (moveHit && subtitleHit) {
        row.dataset.hitLabel = "移动 + 字幕";
      } else if (moveHit) {
        row.dataset.hitLabel = "移动";
      } else if (subtitleHit) {
        row.dataset.hitLabel = "字幕";
      } else {
        delete row.dataset.hitLabel;
      }
    });
    setRuleCount("move", moveMatches.size);
    setRuleCount("subtitle", subtitleMatches.size);
    fillForm("bulkMoveRuleForm", Array.from(moveMatches.keys()));
    fillForm("bulkSubtitleRuleForm", Array.from(subtitleMatches.keys()));
  }

  ["bulkMoveRuleForm", "bulkSubtitleRuleForm"].forEach((formId) => {
    document.querySelector(`#${formId}`)?.addEventListener("submit", (event) => {
      updateRuleMatches();
      if (!event.currentTarget.querySelector('input[name="paths"]')) {
        event.preventDefault();
        window.alert("当前策略没有命中文件。");
      }
    });
  });

  document.querySelectorAll("[data-manual-action]").forEach((check) => {
    check.addEventListener("change", updateRuleMatches);
  });

  document.querySelectorAll("[data-auto-match]").forEach((button) => {
    button.addEventListener("click", () => {
      const rule = button.dataset.autoMatch;
      if (!rule) return;
      if (enabledAutoRules.has(rule)) {
        enabledAutoRules.delete(rule);
        button.classList.remove("active");
      } else {
        enabledAutoRules.add(rule);
        button.classList.add("active");
      }
      updateRuleMatches();
    });
  });

  if (ruleBuilder) {
    updateRuleMatches();
  }

  function showPageProgress(title, message) {
    const overlay = document.querySelector("#pageProgress");
    if (!overlay) return;
    const titleNode = overlay.querySelector("strong");
    const messageNode = overlay.querySelector("span");
    if (titleNode) titleNode.textContent = title || "正在处理";
    if (messageNode) messageNode.textContent = message || "文件较多时可能需要一点时间，请不要关闭页面。";
    overlay.hidden = false;
  }

  document.querySelectorAll('form[action="/preview"], form[action="/move/jobs"], form[action="/scan/subtitles"], [data-progress-submit]').forEach((form) => {
    form.addEventListener("submit", () => {
      const mode = form.dataset.progressSubmit || (form.getAttribute("action") || "").replace("/", "");
      if (form.getAttribute("action") === "/scan/subtitles") {
        showPageProgress("正在发送字幕任务", "正在把选中的视频提交到字幕算力控制台。");
      } else if (mode === "move" || form.getAttribute("action") === "/move/jobs") {
        showPageProgress("正在创建移动任务", "马上进入移动进度页，文件会在后台逐个移动。");
      } else {
        showPageProgress("正在生成移动预览", "正在检查文件位置、回收站冲突和同盘快速移动条件。");
      }
    });
  });

  const moveJobRoot = document.querySelector("[data-move-job]");
  const moveJobId = moveJobRoot?.dataset.moveJob;

  function renderMoveJob(job) {
    const total = job.total || 0;
    const processed = job.processed || 0;
    const progress = document.querySelector("#moveJobProgress");
    if (progress) {
      progress.max = total || 1;
      progress.value = processed;
    }
    const setText = (selector, value) => {
      const node = document.querySelector(selector);
      if (node) node.textContent = String(value ?? "");
    };
    setText("#moveJobStatus", job.status || "");
    setText("#moveJobMessage", job.message || "");
    setText("#moveJobProcessed", processed);
    setText("#moveJobTotal", total);
    setText("#moveJobCurrent", job.current_path || "");
    setText("#moveJobMoved", job.moved || 0);
    setText("#moveJobSkipped", job.skipped || 0);
    setText("#moveJobFailed", job.failed || 0);
    const list = document.querySelector("#moveJobItems");
    if (list) {
      list.innerHTML = (job.items || []).map((item) => `
        <article class="preview-row move-result ${escapeHtml(item.status || "")}">
          <div>
            <strong>${escapeHtml(item.source || "")}</strong>
            <span>${escapeHtml(item.target || "")}</span>
          </div>
          <em class="move-mode ${escapeHtml(item.mode || "")}">${escapeHtml(item.status || "")} · ${escapeHtml(item.reason || "")}</em>
        </article>
      `).join("");
    }
  }

  async function watchMoveJob() {
    if (!moveJobId) return;
    try {
      const response = await fetch(`/api/move/jobs/${moveJobId}`);
      if (!response.ok) return;
      const job = await response.json();
      renderMoveJob(job);
      if (job.status === "completed" || job.status === "failed") {
        window.clearInterval(moveJobTimer);
      }
    } catch (error) {
      // Keep the current progress visible if the browser briefly loses contact.
    }
  }

  const moveJobTimer = moveJobId ? window.setInterval(watchMoveJob, 1200) : null;
  if (moveJobId) {
    watchMoveJob();
  }
})();
