<template>
  <section class="settings-view">
    <PageHeader
      kicker="系统"
      title="系统"
      description="集中配置订阅链路、洗版策略、Jellyfin、MTeam、qBittorrent 和通知。"
    />

    <NoticeBanner v-if="message">{{ message }}</NoticeBanner>
    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>

    <BaseCard as="nav" class="setting-tabs">
      <button v-for="tab in tabs" :key="tab.key" type="button" :class="{ active: activeTab === tab.key }" @click="setActiveTab(tab.key)">
        {{ tab.label }}
      </button>
    </BaseCard>

    <BaseCard v-if="demo.enabled && demo.hideSystemSettings && activeTab !== 'demo'" class="setting-panel demo-guard">
      <div class="panel-head">
        <div>
          <h2>演示模式已开启</h2>
          <p>系统设置暂时不可见。这里没有修改、清空或覆盖真实配置，关闭演示模式后会恢复显示。</p>
        </div>
        <BaseButton type="button" @click="setActiveTab('demo')">管理演示模式</BaseButton>
      </div>
      <div class="demo-guard-body">
        <strong>真实设置已隐藏</strong>
        <span>MTeam、qBittorrent、Jellyfin、代理、账号和通知配置仍保留在后端，只是当前页面不展示。</span>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'demo'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>演示模式</h2>
          <p>用一张演示图临时替换控制台里的封面，并隐藏系统设置；不会改写订阅、缓存或真实配置。</p>
        </div>
      </div>
      <div class="form-grid">
        <FormField as="div" label="启用演示模式" wide>
          <BaseSwitch v-model="demo.enabled" aria-label="启用演示模式" />
        </FormField>
        <FormField label="演示封面图" wide hint="留空时使用 MovieMuse 图标。支持 http(s)、/static 本地路径或 data URL。">
          <input v-model.trim="demo.coverUrl" placeholder="/static/icons/moviemuse-app-icon-1024.png">
        </FormField>
        <FormField as="div" label="隐藏系统设置" wide>
          <BaseSwitch v-model="demo.hideSystemSettings" aria-label="隐藏系统设置" />
        </FormField>
      </div>
      <div class="demo-preview">
        <span>预览</span>
        <img :src="demo.replacementCoverUrl" alt="">
      </div>
      <div class="panel-footer">
        <BaseButton type="button" :disabled="loading" @click="loadAll">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveAll">
          {{ saving ? '保存中' : '保存演示模式' }}
        </BaseButton>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'mteam'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>MTeam</h2>
          <p>搜索番号和女优时联动 MTeam，支持 RSS 和 API 参数保留。</p>
        </div>
        <BaseButton type="button" @click="testIntegration('mteam')">测试连接</BaseButton>
      </div>
      <div class="form-grid">
        <FormField as="div" label="启用" wide>
          <BaseSwitch v-model="system.mteam.enabled" aria-label="启用 MTeam" />
        </FormField>
        <FormField label="网址">
          <input v-model.trim="system.mteam.site_url" placeholder="https://zp.m-team.io/">
        </FormField>
        <FormField label="模式">
          <select v-model="system.mteam.mode">
            <option value="rss">RSS</option>
            <option value="api">API</option>
          </select>
        </FormField>
        <FormField label="RSS 地址" wide>
          <input v-model.trim="system.mteam.rss_url">
        </FormField>
        <FormField label="API 地址">
          <input v-model.trim="system.mteam.api_url">
        </FormField>
        <FormField label="API Key">
          <SecretInput v-model.trim="system.mteam.api_key" autocomplete="off">
          </SecretInput>
        </FormField>
      </div>
      <div class="panel-footer">
        <BaseButton type="button" :disabled="loading" @click="loadAll">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveAll">
          {{ saving ? '保存中' : '保存' }}
        </BaseButton>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'qb'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>qBittorrent</h2>
          <p>订阅下载会把 MTeam 种子推送到这里。</p>
        </div>
        <BaseButton type="button" @click="testIntegration('qbittorrent')">测试连接</BaseButton>
      </div>
      <div class="form-grid">
        <FormField label="地址">
          <input v-model.trim="system.qbittorrent.url" placeholder="http://host:8080">
        </FormField>
        <FormField label="API Key" hint="qB 5.2+ 可用；失败时会自动尝试账号密码。">
          <SecretInput v-model.trim="system.qbittorrent.api_key" autocomplete="off" placeholder="可选">
          </SecretInput>
        </FormField>
        <FormField label="用户名">
          <input v-model.trim="system.qbittorrent.username">
        </FormField>
        <FormField label="密码">
          <SecretInput v-model.trim="system.qbittorrent.password" autocomplete="off">
          </SecretInput>
        </FormField>
        <FormField label="下载路径">
          <input v-model.trim="system.qbittorrent.save_path">
        </FormField>
        <FormField label="下载分类">
          <input v-model.trim="system.qbittorrent.category">
        </FormField>
        <FormField label="标签">
          <input v-model.trim="system.qbittorrent.tags">
        </FormField>
      </div>
      <div class="panel-footer">
        <BaseButton type="button" :disabled="loading" @click="loadAll">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveAll">
          {{ saving ? '保存中' : '保存' }}
        </BaseButton>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'jellyfin'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>Jellyfin</h2>
          <p>订阅前查重，已入库的番号会自动切到已入库。读取媒体库前会先保存当前 Jellyfin 配置。</p>
        </div>
        <div class="panel-actions">
          <BaseButton type="button" :disabled="loadingLibraries" @click="loadJellyfinLibraries">
            {{ loadingLibraries ? '读取中' : '读取媒体库' }}
          </BaseButton>
          <BaseButton type="button" @click="testIntegration('jellyfin')">测试连接</BaseButton>
        </div>
      </div>
      <div class="form-grid">
        <FormField label="地址">
          <input v-model.trim="system.jellyfin.url" placeholder="http://host:8096">
        </FormField>
        <FormField label="密钥">
          <SecretInput v-model.trim="system.jellyfin.api_key" autocomplete="off">
          </SecretInput>
        </FormField>
        <FormField label="用户">
          <input v-model.trim="system.jellyfin.username">
        </FormField>
        <FormField label="媒体库">
          <select v-if="jellyfinLibraries.length" v-model="selectedJellyfinLibrary" @change="syncJellyfinLibrary">
            <option value="">全部媒体库</option>
            <option v-for="library in jellyfinLibraries" :key="library.id" :value="library.id">
              {{ library.name }}
            </option>
          </select>
          <input v-else v-model.trim="system.jellyfin.library_id" placeholder="点击读取媒体库，或手动填写媒体库 ID">
        </FormField>
        <FormField label="媒体库名称">
          <input v-model.trim="system.jellyfin.library_name">
        </FormField>
        <FormField as="div" label="启用查重">
          <BaseSwitch v-model="system.jellyfin.dedupe_enabled" aria-label="启用 Jellyfin 查重" />
        </FormField>
      </div>
      <div :class="['nfo-repair-panel', { 'is-busy': repairingNfo }]">
        <div class="nfo-repair-head">
          <div>
            <h3>NFO 演员关系修复</h3>
            <p>只处理转码目录内同一影片目录中已有演员节点的 NFO，把演员同步到缺少 actor 的 NFO。</p>
          </div>
          <div class="panel-actions">
            <BaseButton type="button" :disabled="repairingNfo" @click="previewNfoActorRepair">
              <span class="busy-button-content">
                <i v-if="repairingNfo" class="busy-spinner" aria-hidden="true"></i>
                {{ repairingNfo ? '扫描中' : '扫描候选' }}
              </span>
            </BaseButton>
            <BaseButton
              variant="primary"
              type="button"
              :disabled="repairingNfo || !nfoRepairResult?.target_files"
              @click="applyNfoActorRepair"
            >
              执行修复
            </BaseButton>
          </div>
        </div>
        <div v-if="repairingNfo" class="busy-strip" aria-label="正在扫描 NFO 候选">
          <i></i>
        </div>
        <div v-if="nfoRepairResult" class="nfo-repair-summary">
          <span>目录 {{ nfoRepairResult.repairable_dirs || 0 }}</span>
          <span>待修文件 {{ nfoRepairResult.target_files || 0 }}</span>
          <span v-if="!nfoRepairResult.dry_run">已修 {{ nfoRepairResult.repaired_files || 0 }}</span>
          <span v-if="!nfoRepairResult.dry_run && nfoRepairFailedCount">失败 {{ nfoRepairFailedCount }}</span>
        </div>
        <div v-if="nfoRepairItems.length" class="nfo-repair-list">
          <div v-for="item in nfoRepairItems" :key="item.directory" class="nfo-repair-row">
            <strong>{{ item.catalog_id || folderName(item.directory) }}</strong>
            <span>{{ item.actors?.join('、') }}</span>
            <small>{{ item.target_files?.join('、') }} ← {{ item.source_files?.join('、') }}</small>
            <small v-if="repairResultText(item.results)" class="nfo-repair-result">{{ repairResultText(item.results) }}</small>
          </div>
        </div>
        <div v-else-if="nfoRepairResult" class="empty-line">没有发现第一批可自动修复的 NFO。</div>
      </div>
      <div :class="['nfo-repair-panel', { 'is-busy': refreshingJellyfinActors }]">
        <div class="nfo-repair-head">
          <div>
            <h3>Jellyfin 演员关系刷新</h3>
            <p>扫描同名 NFO 已有 actor、但 Jellyfin 里演员为空的影片，只触发这些条目的单独元数据刷新。</p>
          </div>
          <div class="panel-actions">
            <BaseButton type="button" :disabled="refreshingJellyfinActors" @click="previewJellyfinActorRefresh">
              <span class="busy-button-content">
                <i v-if="refreshingJellyfinActors" class="busy-spinner" aria-hidden="true"></i>
                {{ refreshingJellyfinActors ? '扫描中' : '扫描缺失' }}
              </span>
            </BaseButton>
            <BaseButton
              variant="primary"
              type="button"
              :disabled="refreshingJellyfinActors || !jellyfinActorRefreshResult?.target_items"
              @click="applyJellyfinActorRefresh"
            >
              刷新元数据
            </BaseButton>
          </div>
        </div>
        <div v-if="refreshingJellyfinActors" class="busy-strip" aria-label="正在扫描 Jellyfin 演员关系">
          <i></i>
        </div>
        <div v-if="jellyfinActorRefreshResult" class="nfo-repair-summary">
          <span>NFO {{ jellyfinActorRefreshResult.actor_nfos || 0 }}</span>
          <span>命中 {{ jellyfinActorRefreshResult.matched_items || 0 }}</span>
          <span>待刷新 {{ jellyfinActorRefreshResult.target_items || 0 }}</span>
          <span v-if="!jellyfinActorRefreshResult.dry_run">已触发 {{ jellyfinActorRefreshResult.refreshed_items || 0 }}</span>
          <span v-if="!jellyfinActorRefreshResult.dry_run && jellyfinActorRefreshFailedCount">失败 {{ jellyfinActorRefreshFailedCount }}</span>
        </div>
        <div v-if="jellyfinActorRefreshItems.length" class="nfo-repair-list">
          <div v-for="item in jellyfinActorRefreshItems" :key="item.item_id || item.video" class="nfo-repair-row">
            <strong>{{ item.av_id }}</strong>
            <span>{{ item.actors?.join('、') }}</span>
            <small>{{ item.item_name || 'Jellyfin 条目' }} · {{ item.video }}</small>
            <small v-if="repairResultText(item.results)" class="nfo-repair-result">{{ repairResultText(item.results) }}</small>
          </div>
        </div>
        <div v-else-if="jellyfinActorRefreshResult" class="empty-line">没有发现需要刷新演员关系的 Jellyfin 条目。</div>
      </div>
      <div class="panel-footer">
        <BaseButton type="button" :disabled="loading" @click="loadAll">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveAll">
          {{ saving ? '保存中' : '保存' }}
        </BaseButton>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'strategy'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>订阅与洗版</h2>
          <p>订阅策略和洗版策略合并到系统维护；定时任务仍在任务页统一执行。</p>
        </div>
      </div>
      <div class="form-grid">
        <FormField as="div" label="启用订阅轮询">
          <BaseSwitch v-model="subscription.poll_enabled" aria-label="启用订阅轮询" />
        </FormField>
        <FormField label="最大共演人数">
          <input v-model.number="subscription.max_coactors" type="number" min="1" max="2">
        </FormField>
        <FormField as="div" label="启用 JavDB 实时抓取">
          <BaseSwitch v-model="subscription.javdb_source_enabled" aria-label="启用 JavDB 实时抓取" />
        </FormField>
        <FormField as="div" label="洗版启用">
          <BaseSwitch v-model="subscription.wash.enabled" aria-label="洗版启用" />
        </FormField>
        <FormField label="洗版过期天数">
          <input v-model.number="subscription.wash.expire_days" type="number" min="1">
        </FormField>
        <FormField as="div" label="洗版检查中文">
          <BaseSwitch v-model="subscription.wash.check_chinese" aria-label="洗版检查中文" />
        </FormField>
        <FormField as="div" label="洗版检查 4K">
          <BaseSwitch v-model="subscription.wash.check_4k" aria-label="洗版检查 4K" />
        </FormField>
      </div>
      <div class="panel-footer">
        <BaseButton type="button" :disabled="loading" @click="loadAll">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveAll">
          {{ saving ? '保存中' : '保存' }}
        </BaseButton>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'makers'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>常驻厂牌</h2>
          <p>厂牌发售页会读取这里的列表。</p>
        </div>
        <BaseButton variant="primary" type="button" @click="addMaker">添加厂牌</BaseButton>
      </div>
      <div class="maker-list">
        <div v-for="(maker, index) in subscription.pinned_makers" :key="`${maker.name}-${index}`" class="maker-row">
          <input v-model.trim="maker.name" placeholder="厂牌">
          <input v-model.trim="maker.url" placeholder="JavDB 链接">
          <select v-model="maker.preferred_listing_source" title="发售首选源">
            <option value="javlibrary">JavLibrary</option>
            <option value="dmm">DMM/FANZA</option>
            <option value="javdb">JavDB</option>
            <option value="auto">自动</option>
          </select>
          <BaseButton type="button" @click="removeMaker(index)">删除</BaseButton>
        </div>
      </div>
      <div class="panel-footer">
        <BaseButton type="button" :disabled="loading" @click="loadAll">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveAll">
          {{ saving ? '保存中' : '保存' }}
        </BaseButton>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'identities'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>身份锚点</h2>
          <p>把 JavDB、DMM/FANZA、JavLibrary 和本地订阅里的同一女优锁定到同一个身份，订阅轮询会优先使用人工锚点。</p>
        </div>
        <div class="panel-actions">
          <BaseButton type="button" :disabled="loadingIdentities" @click="loadActorIdentities">
            {{ loadingIdentities ? '读取中' : '刷新身份' }}
          </BaseButton>
          <BaseButton type="button" @click="resetIdentityForm">新建锚点</BaseButton>
        </div>
      </div>
      <div class="identity-tools">
        <input v-model.trim="identityQuery" placeholder="搜索女优名、别名、JavDB ID、JavLibrary ID" @keyup.enter="loadActorIdentities">
        <BaseButton type="button" :disabled="loadingIdentities" @click="loadActorIdentities">搜索</BaseButton>
      </div>
      <div class="identity-layout">
        <div class="identity-list">
          <div v-if="loadingIdentities" class="empty-line">正在读取身份缓存...</div>
          <div v-else-if="!actorIdentities.length" class="empty-line">暂无身份缓存。</div>
          <template v-else>
            <button
              v-for="item in actorIdentities"
              :key="item.canonical_id"
              type="button"
              class="identity-row"
              :class="{ active: identityForm.canonical_id === item.canonical_id }"
              @click="editIdentity(item)"
            >
              <input
                type="checkbox"
                :checked="mergeSourceIds.includes(item.canonical_id)"
                @click.stop="toggleMergeSource(item)"
              >
              <span>
                <strong>{{ item.display_name || item.name || item.id }}</strong>
                <em>{{ identitySourceLabel(item) }}</em>
              </span>
              <small>{{ item.latest_av_id || item.javdb_id || item.javlibrary_star_id || '未绑定外部 ID' }}</small>
            </button>
          </template>
        </div>

        <div class="identity-editor">
          <div class="form-grid">
            <FormField label="主显示名">
              <input v-model.trim="identityForm.display_name" placeholder="涼森れむ">
            </FormField>
            <FormField label="首选数据源">
              <select v-model="identityForm.preferred_source">
                <option value="">自动</option>
                <option value="dmm">DMM/FANZA</option>
                <option value="javdb">JavDB</option>
                <option value="javlibrary">JavLibrary</option>
              </select>
            </FormField>
            <FormField label="JavDB ID">
              <input v-model.trim="identityForm.javdb_id" placeholder="例如 aeqfy 或 JavDB 演员 ID">
            </FormField>
            <FormField label="DMM 名称">
              <input v-model.trim="identityForm.dmm_name" placeholder="DMM/FANZA 女优名">
            </FormField>
            <FormField label="DMM 女优页" wide>
              <input v-model.trim="identityForm.dmm_url" placeholder="https://www.dmm.co.jp/mono/dvd/-/list/=/article=actress/id=.../sort=date/">
            </FormField>
            <FormField label="JavLibrary Star ID">
              <input v-model.trim="identityForm.javlibrary_star_id" placeholder="例如 aeqfy">
            </FormField>
            <FormField as="div" label="锁定人工锚点">
              <BaseSwitch v-model="identityForm.locked" aria-label="锁定人工锚点" />
            </FormField>
            <FormField label="别名" wide>
              <textarea v-model="identityAliasText" placeholder="每行一个别名，或用逗号分隔"></textarea>
            </FormField>
          </div>
          <div v-if="identityForm.canonical_id" class="identity-meta">
            <span>canonical_id: {{ identityForm.canonical_id }}</span>
            <span>来源: {{ identityForm.manual ? '人工' : '自动/订阅' }}</span>
          </div>
          <div class="panel-footer">
            <BaseButton variant="primary" type="button" :disabled="savingIdentity" @click="saveActorIdentity">
              {{ savingIdentity ? '保存中' : '保存锚点' }}
            </BaseButton>
            <BaseButton type="button" :disabled="savingIdentity || mergeSourceIds.length < 1" @click="mergeActorIdentity">
              合并所选
            </BaseButton>
            <BaseButton type="button" :disabled="savingIdentity || !identityForm.manual" @click="deleteActorIdentity">
              删除人工锚点
            </BaseButton>
          </div>
        </div>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'network'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>系统代理</h2>
          <p>Docker 当前代理和 JavDB 浏览器抓取共用这里的出口。未启用自定义代理时，会继续使用容器启动时已有的代理环境变量。</p>
        </div>
        <BaseButton type="button" :disabled="testingProxy" @click="testSystemProxy">
          {{ testingProxy ? '测试中' : '测试代理' }}
        </BaseButton>
        <BaseButton type="button" :disabled="testingFlareSolverr" @click="testFlareSolverr">
          {{ testingFlareSolverr ? '测试中' : '测试 FlareSolverr' }}
        </BaseButton>
      </div>
      <div class="proxy-status" v-if="proxyStatus">
        <span>当前有效代理</span>
        <strong>{{ proxyStatus.effective_proxy || '未检测到代理' }}</strong>
        <span>FlareSolverr</span>
        <strong>{{ proxyStatus.flaresolverr_url || '未配置' }}</strong>
      </div>
      <div class="form-grid">
        <FormField as="div" label="启用自定义代理">
          <BaseSwitch v-model="system.network.proxy_enabled" aria-label="启用自定义代理" />
        </FormField>
        <FormField as="div" label="JavDB 使用代理">
          <BaseSwitch v-model="system.network.apply_to_javdb" aria-label="JavDB 使用代理" />
        </FormField>
        <FormField label="HTTP 代理">
          <input v-model.trim="system.network.http_proxy" placeholder="http://host.docker.internal:7897">
        </FormField>
        <FormField label="HTTPS 代理">
          <input v-model.trim="system.network.https_proxy" placeholder="http://host.docker.internal:7897">
        </FormField>
        <FormField label="NO_PROXY" wide>
          <input v-model.trim="system.network.no_proxy" placeholder="localhost,127.0.0.1">
        </FormField>
        <FormField label="FlareSolverr URL" wide>
          <input v-model.trim="system.network.flaresolverr_url" placeholder="http://host.docker.internal:8281/v1">
        </FormField>
      </div>
      <div class="panel-footer">
        <BaseButton type="button" :disabled="loading" @click="loadAll">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveAll">
          {{ saving ? '保存中' : '保存' }}
        </BaseButton>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'cache'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>缓存维护</h2>
          <p>管理订阅抓取产生的封面、剧照、女优头像和直链预告资产；已发售番号会在维护时冻结封面。</p>
        </div>
        <div class="panel-actions">
          <BaseButton type="button" :disabled="loadingAssetCache" @click="loadAssetCache">
            {{ loadingAssetCache ? '刷新中' : '刷新统计' }}
          </BaseButton>
          <BaseButton variant="primary" type="button" :disabled="maintainingAssetCache" @click="runAssetMaintenance">
            {{ maintainingAssetCache ? '维护中' : '立即维护' }}
          </BaseButton>
          <BaseButton type="button" :disabled="maintainingAssetCache" @click="cleanupAssetCache">
            清理非冻结缓存
          </BaseButton>
        </div>
      </div>
      <div class="cache-summary">
        <div>
          <span>资产数量</span>
          <strong>{{ assetCache.asset_cache?.total || 0 }}</strong>
        </div>
        <div>
          <span>本地占用</span>
          <strong>{{ formatBytes(assetCache.asset_cache?.bytes || 0) }}</strong>
        </div>
        <div>
          <span>容量上限</span>
          <strong>{{ formatBytes((assetMaxMb || 0) * 1024 * 1024) }}</strong>
        </div>
        <div v-for="(kind, name) in assetCache.asset_cache?.kinds || {}" :key="name">
          <span>{{ assetKindLabel(name) }}</span>
          <strong>{{ kind.count || 0 }} / {{ formatBytes(kind.bytes || 0) }}</strong>
        </div>
      </div>
      <div class="form-grid">
        <FormField label="维护定时">
          <input v-model.trim="subscription.asset_cron" placeholder="15 3 * * *">
        </FormField>
        <FormField label="容量上限 MB">
          <input v-model.number="assetMaxMb" type="number" min="0">
        </FormField>
      </div>
      <div v-if="assetMaintenanceResult" class="maintenance-result">
        <span>冻结：{{ assetMaintenanceResult.freeze?.frozen || 0 }} / 检查 {{ assetMaintenanceResult.freeze?.checked || 0 }}</span>
        <span>清理：{{ assetMaintenanceResult.cleanup?.deleted || 0 }} 项，{{ formatBytes(assetMaintenanceResult.cleanup?.deleted_bytes || 0) }}</span>
        <span>缺失记录：{{ assetMaintenanceResult.cleanup?.removed_missing || 0 }} 项</span>
        <span>当前占用：{{ formatBytes(assetMaintenanceResult.asset_cache?.bytes || 0) }}</span>
      </div>
      <div class="panel-footer">
        <BaseButton type="button" :disabled="loading" @click="loadAll">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveAll">
          {{ saving ? '保存中' : '保存' }}
        </BaseButton>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'account'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>用户设置</h2>
          <p>修改控制台登录账号。新环境首次密码会写入启动日志和数据目录提示文件，建议首次登录后立即修改。</p>
        </div>
        <BaseButton type="button" @click="logout">退出登录</BaseButton>
      </div>
      <div class="form-grid">
        <FormField label="用户名">
          <input v-model.trim="system.auth.username" autocomplete="username">
        </FormField>
        <FormField label="新密码" hint="留空则只修改用户名。">
          <SecretInput v-model="system.auth.password" autocomplete="new-password" />
        </FormField>
        <FormField label="确认新密码">
          <SecretInput v-model="system.auth.confirm_password" autocomplete="new-password" />
        </FormField>
      </div>
      <div class="panel-footer">
        <BaseButton type="button" :disabled="loading" @click="loadAll">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveAll">
          {{ saving ? '保存中' : '保存账号' }}
        </BaseButton>
      </div>
    </BaseCard>

    <NotificationsView v-else ref="notificationsView" embedded />
  </section>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, postJson } from '../lib/api'
import { useDemoStore } from '../stores/demo'
import NotificationsView from './NotificationsView.vue'
import { BaseButton, BaseCard, BaseSwitch, FormField, NoticeBanner, PageHeader, SecretInput } from '../components/ui'

const tabs = [
  { key: 'demo', label: '演示模式' },
  { key: 'mteam', label: 'MTeam' },
  { key: 'qb', label: 'qBittorrent' },
  { key: 'jellyfin', label: 'Jellyfin' },
  { key: 'strategy', label: '订阅与洗版' },
  { key: 'makers', label: '常驻厂牌' },
  { key: 'identities', label: '身份锚点' },
  { key: 'network', label: '系统代理' },
  { key: 'account', label: '用户设置' },
  { key: 'notifications', label: '通知' }
]
tabs.splice(Math.max(0, tabs.length - 1), 0, { key: 'cache', label: '缓存维护' })
const tabKeys = new Set(tabs.map((tab) => tab.key))

const defaultMakers = [
  { name: 'S1 NO.1 STYLE', url: 'https://javdb.com/makers/7R?f=download', preferred_listing_source: 'javlibrary' },
  { name: 'PRESTIGE', url: 'https://javdb.com/makers/6M?f=download', preferred_listing_source: 'javlibrary' },
  { name: 'IDEA POCKET', url: 'https://javdb.com/makers/ZXX?f=download', preferred_listing_source: 'javlibrary' },
  { name: 'Madonna', url: 'https://javdb.com/makers/zKW?f=download', preferred_listing_source: 'javlibrary' },
  { name: 'SOD Create', url: 'https://javdb.com/makers/q6?f=download', preferred_listing_source: 'javlibrary' }
]

const route = useRoute()
const router = useRouter()
const demo = useDemoStore()
const activeTab = ref(normalizeTab(route.query.tab) || normalizeTab(localStorage.getItem('systemActiveTab')) || 'jellyfin')
const loading = ref(false)
const saving = ref(false)
const loadingLibraries = ref(false)
const testingProxy = ref(false)
const testingFlareSolverr = ref(false)
const message = ref('')
const errorMessage = ref('')
const jellyfinLibraries = ref([])
const selectedJellyfinLibrary = ref('')
const notificationsView = ref(null)
const proxyStatus = ref(null)
const repairingNfo = ref(false)
const nfoRepairResult = ref(null)
const refreshingJellyfinActors = ref(false)
const jellyfinActorRefreshResult = ref(null)
const loadingAssetCache = ref(false)
const maintainingAssetCache = ref(false)
const assetCache = ref({})
const assetMaintenanceResult = ref(null)
const assetMaxMb = ref(2048)
const loadingIdentities = ref(false)
const savingIdentity = ref(false)
const identityQuery = ref('')
const actorIdentities = ref([])
const mergeSourceIds = ref([])
const identityAliasText = ref('')
const nfoRepairItems = computed(() => Array.isArray(nfoRepairResult.value?.items) ? nfoRepairResult.value.items.slice(0, 20) : [])
const nfoRepairFailedCount = computed(() => nfoRepairItems.value.reduce((total, item) => {
  const results = Array.isArray(item.results) ? item.results : []
  return total + results.filter((result) => result.status === 'failed').length
}, 0))
const jellyfinActorRefreshItems = computed(() => (
  Array.isArray(jellyfinActorRefreshResult.value?.items) ? jellyfinActorRefreshResult.value.items.slice(0, 20) : []
))
const jellyfinActorRefreshFailedCount = computed(() => jellyfinActorRefreshItems.value.reduce((total, item) => {
  const results = Array.isArray(item.results) ? item.results : []
  return total + results.filter((result) => result.status === 'failed').length
}, 0))

const emptyIdentityForm = () => ({
  canonical_id: '',
  id: '',
  display_name: '',
  name: '',
  aliases: [],
  preferred_source: '',
  javdb_id: '',
  dmm_name: '',
  dmm_url: '',
  javlibrary_star_id: '',
  cover: '',
  latest_cover: '',
  latest_av_id: '',
  latest_title: '',
  latest_date: '',
  locked: true,
  manual: true
})
const identityForm = reactive(emptyIdentityForm())

const system = reactive({
  mteam: { site_url: '', mode: 'rss', rss_url: '', api_url: '', api_key: '', enabled: false },
  qbittorrent: { url: '', api_key: '', username: '', password: '', save_path: '', category: '', tags: '' },
  jellyfin: { url: '', api_key: '', username: '', library_id: '', library_name: '', dedupe_enabled: true },
  network: { proxy_enabled: false, http_proxy: '', https_proxy: '', no_proxy: 'localhost,127.0.0.1', apply_to_javdb: true, flaresolverr_url: '' },
  auth: { username: 'admin', password: '', confirm_password: '' }
})

const subscription = reactive({
  poll_enabled: true,
  max_coactors: 2,
  javdb_source_enabled: false,
  asset_cron: '15 3 * * *',
  asset_cache_max_mb: 2048,
  wash: { enabled: true, expire_days: 90, check_chinese: true, check_4k: true },
  pinned_makers: [...defaultMakers]
})

loadAll()
syncTabQuery(activeTab.value)

watch(
  () => route.query.tab,
  (tab) => {
    const nextTab = normalizeTab(tab)
    if (nextTab && nextTab !== activeTab.value) {
      activeTab.value = nextTab
      localStorage.setItem('systemActiveTab', nextTab)
    }
  }
)

function normalizeTab(value) {
  const key = String(value || '')
  return tabKeys.has(key) ? key : ''
}

function setActiveTab(key) {
  const nextTab = normalizeTab(key) || 'jellyfin'
  activeTab.value = nextTab
  localStorage.setItem('systemActiveTab', nextTab)
  syncTabQuery(nextTab)
}

function syncTabQuery(key) {
  if (route.query.tab === key) return
  router.replace({ path: route.path, query: { ...route.query, tab: key } })
}

async function loadAll() {
  loading.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const [systemPayload, subPayload] = await Promise.all([
      api('/api/system-settings'),
      api('/api/subscriptions/settings')
    ])
    demo.applySettings(systemPayload.settings?.demo || {})
    Object.assign(system.mteam, systemPayload.settings?.mteam || {})
    Object.assign(system.qbittorrent, systemPayload.settings?.qbittorrent || {})
    Object.assign(system.jellyfin, systemPayload.settings?.jellyfin || {})
    Object.assign(system.network, systemPayload.settings?.network || {})
    Object.assign(system.auth, { username: systemPayload.settings?.auth?.username || 'admin', password: '', confirm_password: '' })
    await loadProxyStatus()
    await loadAssetCache()
    await loadActorIdentities()
    selectedJellyfinLibrary.value = system.jellyfin.library_id || ''
    Object.assign(subscription, {
      poll_enabled: subPayload.settings?.poll_enabled ?? true,
      max_coactors: subPayload.settings?.max_coactors ?? 2,
      javdb_source_enabled: !!subPayload.settings?.javdb_source_enabled,
      asset_cron: subPayload.settings?.asset_cron || '15 3 * * *',
      asset_cache_max_mb: subPayload.settings?.asset_cache_max_mb ?? 2048,
      wash: { ...subscription.wash, ...(subPayload.settings?.wash || {}) },
      pinned_makers: Array.isArray(subPayload.settings?.pinned_makers) && subPayload.settings.pinned_makers.length
        ? subPayload.settings.pinned_makers.map((item) => ({
          name: item.name || '',
          url: item.url || '',
          preferred_listing_source: item.preferred_listing_source || 'javlibrary'
        }))
        : [...defaultMakers]
    })
    assetMaxMb.value = subscription.asset_cache_max_mb
  } catch (err) {
    errorMessage.value = err.message || '读取设置失败'
  } finally {
    loading.value = false
  }
}

async function saveSystemSettings() {
  if (activeTab.value === 'account') {
    if (!system.auth.username.trim()) {
      throw new Error('用户名不能为空')
    }
    if (system.auth.password || system.auth.confirm_password) {
      if (system.auth.password !== system.auth.confirm_password) {
        throw new Error('两次输入的新密码不一致')
      }
    }
  }
  await postJson('/api/system-settings', {
    mteam: { ...system.mteam },
    qbittorrent: { ...system.qbittorrent },
    jellyfin: { ...system.jellyfin },
    network: { ...system.network },
    auth: {
      username: system.auth.username,
      password: activeTab.value === 'account' ? system.auth.password : ''
    }
  })
}

async function saveSubscriptionSettings() {
  await postJson('/api/subscriptions/settings', {
    poll_enabled: subscription.poll_enabled,
    max_coactors: subscription.max_coactors,
    javdb_source_enabled: subscription.javdb_source_enabled,
    asset_cron: subscription.asset_cron,
    asset_cache_max_mb: assetMaxMb.value,
    wash: { ...subscription.wash },
    pinned_makers: subscription.pinned_makers
      .filter((item) => item.name || item.url)
      .map((item) => ({
        name: item.name,
        url: item.url,
        preferred_listing_source: item.preferred_listing_source || 'javlibrary'
      }))
  })
}

async function saveAll() {
  saving.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    if (activeTab.value === 'notifications' && notificationsView.value?.saveNotifications) {
      await notificationsView.value.saveNotifications()
      return
    }
    if (activeTab.value === 'demo') {
      await demo.save()
      message.value = demo.enabled ? '演示模式已开启' : '演示模式已关闭'
      return
    }
    syncJellyfinLibrary()
    await Promise.all([saveSystemSettings(), saveSubscriptionSettings()])
    if (activeTab.value === 'account') {
      system.auth.password = ''
      system.auth.confirm_password = ''
      message.value = '用户设置已保存'
    } else {
      message.value = '系统设置已保存'
    }
  } catch (err) {
    errorMessage.value = err.message || '保存设置失败'
  } finally {
    saving.value = false
  }
}

async function testIntegration(name) {
  message.value = ''
  errorMessage.value = ''
  try {
    syncJellyfinLibrary()
    await saveSystemSettings()
    const result = await postJson(`/api/integrations/test/${name}`)
    message.value = result.message || result.detail?.message || `${name} 测试完成`
  } catch (err) {
    errorMessage.value = err.message || `${name} 测试失败`
  }
}

async function logout() {
  try {
    await postJson('/api/auth/logout', {})
  } finally {
    window.location.href = '/login'
  }
}

async function loadProxyStatus() {
  try {
    proxyStatus.value = await api('/api/system-proxy/status')
  } catch {
    proxyStatus.value = null
  }
}

async function testSystemProxy() {
  testingProxy.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    await saveSystemSettings()
    const result = await postJson('/api/system-proxy/test', {})
    proxyStatus.value = result.proxy || null
    if (result.status === 'ok') {
      message.value = `代理测试成功：${result.body || result.status_code}`
    } else {
      errorMessage.value = result.message || '代理测试失败'
    }
  } catch (err) {
    errorMessage.value = err.message || '代理测试失败'
  } finally {
    testingProxy.value = false
  }
}

async function testFlareSolverr() {
  testingFlareSolverr.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    await saveSystemSettings()
    const result = await postJson('/api/system-flaresolverr/test', {})
    proxyStatus.value = result.proxy || null
    if (result.status === 'ok') {
      message.value = result.message || 'FlareSolverr 可用'
    } else {
      errorMessage.value = result.message || 'FlareSolverr 测试失败'
    }
  } catch (err) {
    errorMessage.value = err.message || 'FlareSolverr 测试失败'
  } finally {
    testingFlareSolverr.value = false
  }
}

async function loadAssetCache() {
  loadingAssetCache.value = true
  try {
    assetCache.value = await api('/api/subscriptions/asset-cache/status')
    if (assetCache.value?.max_mb !== undefined) {
      assetMaxMb.value = assetCache.value.max_mb
    }
  } catch {
    assetCache.value = {}
  } finally {
    loadingAssetCache.value = false
  }
}

async function runAssetMaintenance() {
  maintainingAssetCache.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = await postJson('/api/subscriptions/asset-cache/maintenance', { max_mb: assetMaxMb.value })
    assetMaintenanceResult.value = payload.result || null
    assetCache.value = { status: 'ok', asset_cache: payload.result?.asset_cache || {}, max_mb: assetMaxMb.value }
    message.value = '资产缓存维护完成'
  } catch (err) {
    errorMessage.value = err.message || '资产缓存维护失败'
  } finally {
    maintainingAssetCache.value = false
  }
}

async function cleanupAssetCache() {
  const ok = window.confirm('将清理所有非冻结资产缓存。已冻结的已发售封面会保留，继续吗？')
  if (!ok) return
  maintainingAssetCache.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = await postJson('/api/subscriptions/asset-cache/cleanup', { max_mb: 0, freeze: true })
    assetMaintenanceResult.value = payload.result || null
    assetCache.value = { status: 'ok', asset_cache: payload.result?.asset_cache || {}, max_mb: assetMaxMb.value }
    message.value = '非冻结资产缓存已清理'
  } catch (err) {
    errorMessage.value = err.message || '资产缓存清理失败'
  } finally {
    maintainingAssetCache.value = false
  }
}

function formatBytes(value) {
  const bytes = Number(value || 0)
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function assetKindLabel(kind) {
  return {
    actor: '女优头像',
    cover: '封面',
    screenshot: '剧照',
    trailer: '预告',
    image: '普通图片'
  }[kind] || kind
}

async function loadJellyfinLibraries() {
  loadingLibraries.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    await saveSystemSettings()
    const payload = await api('/api/jellyfin/libraries')
    jellyfinLibraries.value = Array.isArray(payload.libraries) ? payload.libraries : []
    selectedJellyfinLibrary.value = system.jellyfin.library_id || ''
    if (!selectedJellyfinLibrary.value && jellyfinLibraries.value.length === 1) {
      selectedJellyfinLibrary.value = jellyfinLibraries.value[0].id
      syncJellyfinLibrary()
    }
    message.value = `已读取 ${jellyfinLibraries.value.length} 个 Jellyfin 媒体库`
  } catch (err) {
    errorMessage.value = err.message || '读取 Jellyfin 媒体库失败'
  } finally {
    loadingLibraries.value = false
  }
}

async function previewNfoActorRepair() {
  repairingNfo.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = await api('/api/jellyfin/nfo-actor-repair')
    nfoRepairResult.value = payload.result || null
    const count = Number(nfoRepairResult.value?.target_files || 0)
    message.value = count ? `发现 ${count} 个可修复 NFO` : '没有发现可自动修复的 NFO'
  } catch (err) {
    errorMessage.value = err.message || '扫描 NFO 失败'
  } finally {
    repairingNfo.value = false
  }
}

async function applyNfoActorRepair() {
  const count = Number(nfoRepairResult.value?.target_files || 0)
  if (!count) return
  const ok = window.confirm(`将备份并修复 ${count} 个 NFO 文件。继续吗？`)
  if (!ok) return
  repairingNfo.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = await postJson('/api/jellyfin/nfo-actor-repair', {})
    nfoRepairResult.value = payload.result || null
    message.value = `NFO 修复完成：${nfoRepairResult.value?.repaired_files || 0} 个文件`
  } catch (err) {
    errorMessage.value = err.message || '执行 NFO 修复失败'
  } finally {
    repairingNfo.value = false
  }
}

function folderName(path) {
  return String(path || '').split(/[\\/]/).filter(Boolean).pop() || ''
}

function repairResultText(results) {
  if (!Array.isArray(results) || !results.length) return ''
  return results.map((result) => {
    const file = result.file || '条目'
    if (result.status === 'updated') return `${file} 已修复`
    if (result.status === 'refreshed') return '已触发 Jellyfin 刷新'
    if (result.status === 'skipped') return `${file} 跳过：${result.reason || '无需修复'}`
    if (result.status === 'failed') return `${file} 失败：${result.reason || '未知错误'}`
    return `${file} ${result.status || ''}`.trim()
  }).join('；')
}

async function previewJellyfinActorRefresh() {
  refreshingJellyfinActors.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = await api('/api/jellyfin/actor-refresh')
    jellyfinActorRefreshResult.value = payload.result || null
    const count = Number(jellyfinActorRefreshResult.value?.target_items || 0)
    message.value = count ? `发现 ${count} 个需要刷新演员关系的 Jellyfin 条目` : '没有发现需要刷新演员关系的 Jellyfin 条目'
  } catch (err) {
    errorMessage.value = err.message || '扫描 Jellyfin 演员关系失败'
  } finally {
    refreshingJellyfinActors.value = false
  }
}

async function applyJellyfinActorRefresh() {
  const count = Number(jellyfinActorRefreshResult.value?.target_items || 0)
  if (!count) return
  const ok = window.confirm(`将触发 ${count} 个 Jellyfin 条目的单独元数据刷新。继续吗？`)
  if (!ok) return
  refreshingJellyfinActors.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = await postJson('/api/jellyfin/actor-refresh', {})
    jellyfinActorRefreshResult.value = payload.result || null
    message.value = `Jellyfin 刷新已触发：${jellyfinActorRefreshResult.value?.refreshed_items || 0} 个条目`
  } catch (err) {
    errorMessage.value = err.message || '执行 Jellyfin 演员刷新失败'
  } finally {
    refreshingJellyfinActors.value = false
  }
}

function syncJellyfinLibrary() {
  if (jellyfinLibraries.value.length) {
    const library = jellyfinLibraries.value.find((item) => item.id === selectedJellyfinLibrary.value)
    system.jellyfin.library_id = selectedJellyfinLibrary.value || ''
    system.jellyfin.library_name = library?.name || system.jellyfin.library_name || ''
  } else {
    selectedJellyfinLibrary.value = system.jellyfin.library_id || ''
  }
}

function addMaker() {
  subscription.pinned_makers.push({ name: '', url: '', preferred_listing_source: 'javlibrary' })
}

function removeMaker(index) {
  subscription.pinned_makers.splice(index, 1)
}

function splitAliases(value) {
  return String(value || '')
    .split(/[\n,，、]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function resetIdentityForm() {
  Object.assign(identityForm, emptyIdentityForm())
  identityAliasText.value = ''
  mergeSourceIds.value = []
}

function editIdentity(item) {
  Object.assign(identityForm, emptyIdentityForm(), item || {}, {
    locked: item?.manual ? !!item.locked : true,
    manual: !!item?.manual
  })
  identityAliasText.value = Array.isArray(item?.aliases) ? item.aliases.join('\n') : ''
}

function identityPayload() {
  const aliases = splitAliases(identityAliasText.value)
  return {
    ...identityForm,
    aliases,
    name: identityForm.display_name || identityForm.name || identityForm.dmm_name || identityForm.id,
    locked: !!identityForm.locked
  }
}

function identitySourceLabel(item) {
  const parts = []
  if (item.manual) parts.push('人工')
  else parts.push(item.origin === 'subscription' ? '订阅' : '自动')
  if (item.locked) parts.push('锁定')
  if (item.source_chain?.length) parts.push(item.source_chain.join('+'))
  return parts.join(' · ')
}

function toggleMergeSource(item) {
  const id = item?.canonical_id
  if (!id) return
  if (mergeSourceIds.value.includes(id)) {
    mergeSourceIds.value = mergeSourceIds.value.filter((value) => value !== id)
  } else {
    mergeSourceIds.value = [...mergeSourceIds.value, id]
  }
}

async function loadActorIdentities() {
  loadingIdentities.value = true
  try {
    const params = new URLSearchParams({ limit: '500' })
    if (identityQuery.value) params.set('q', identityQuery.value)
    const payload = await api(`/api/subscriptions/actor-identities?${params.toString()}`)
    actorIdentities.value = Array.isArray(payload.identities) ? payload.identities : []
  } catch (err) {
    errorMessage.value = err.message || '读取身份锚点失败'
  } finally {
    loadingIdentities.value = false
  }
}

async function saveActorIdentity() {
  savingIdentity.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = await postJson('/api/subscriptions/actor-identities', identityPayload())
    editIdentity(payload.identity || {})
    await loadActorIdentities()
    message.value = '身份锚点已保存'
  } catch (err) {
    errorMessage.value = err.message || '保存身份锚点失败'
  } finally {
    savingIdentity.value = false
  }
}

async function mergeActorIdentity() {
  savingIdentity.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = await postJson('/api/subscriptions/actor-identities/merge', {
      target: identityPayload(),
      source_ids: mergeSourceIds.value
    })
    editIdentity(payload.identity || {})
    await loadActorIdentities()
    message.value = '身份锚点已合并'
  } catch (err) {
    errorMessage.value = err.message || '合并身份锚点失败'
  } finally {
    savingIdentity.value = false
  }
}

async function deleteActorIdentity() {
  if (!identityForm.canonical_id) return
  const ok = window.confirm('只删除人工锚点，自动缓存仍会保留。继续吗？')
  if (!ok) return
  savingIdentity.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    await api(`/api/subscriptions/actor-identities/${encodeURIComponent(identityForm.canonical_id)}`, { method: 'DELETE' })
    resetIdentityForm()
    await loadActorIdentities()
    message.value = '人工身份锚点已删除'
  } catch (err) {
    errorMessage.value = err.message || '删除身份锚点失败'
  } finally {
    savingIdentity.value = false
  }
}
</script>

<style scoped>
.settings-view {
  display: grid;
  gap: 18px;
  --mm-input-radius: 14px;
}

.demo-guard {
  min-height: 280px;
}

.demo-guard-body {
  display: grid;
  gap: 8px;
  padding: 22px;
  border: 1px dashed color-mix(in srgb, var(--mm-warning) 44%, var(--mm-border));
  border-radius: var(--mm-radius-md);
  background: var(--mm-warning-soft);
}

.demo-guard-body strong {
  color: var(--mm-text);
  font-size: 18px;
  font-weight: var(--mm-font-weight-semibold);
}

.demo-guard-body span {
  color: var(--mm-muted);
  line-height: 1.7;
}

.demo-preview {
  display: grid;
  grid-template-columns: auto 160px;
  align-items: center;
  gap: 18px;
  width: min(360px, 100%);
  padding: 14px;
  border: 1px solid var(--mm-border);
  border-radius: var(--mm-radius-md);
  background: var(--mm-surface);
}

.demo-preview span {
  color: var(--mm-muted);
  font-size: 14px;
}

.demo-preview img {
  width: 160px;
  max-width: 100%;
  aspect-ratio: 3 / 2;
  border-radius: var(--mm-radius-sm);
  background: var(--mm-image-bg);
  object-fit: contain;
}

.setting-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px;
}

.setting-tabs button {
  min-height: 40px;
  padding: 0 16px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: var(--mm-control-bg);
  color: var(--mm-muted);
  font: inherit;
  font-weight: var(--mm-font-weight-medium);
  cursor: pointer;
}

.setting-tabs button.active,
.setting-tabs button:hover {
  border-color: rgba(255, 56, 92, .35);
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
}

.setting-panel {
  display: grid;
  gap: 18px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.panel-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: flex-end;
}

.panel-footer {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: flex-start;
  padding-top: 4px;
}

.panel-head h2 {
  margin: 0;
  color: var(--mm-text);
  font-size: 22px;
  font-weight: var(--mm-font-weight-semibold);
}

.panel-head p {
  margin: 6px 0 0;
  color: var(--mm-muted);
  line-height: 1.6;
}

.proxy-status {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  min-height: 42px;
  padding: 10px 14px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: var(--mm-control-muted-bg);
  color: var(--mm-muted);
}

.proxy-status strong {
  color: var(--mm-text);
  font-weight: var(--mm-font-weight-semibold);
}

.cache-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.cache-summary > div,
.maintenance-result {
  display: grid;
  gap: 6px;
  min-height: 74px;
  padding: 14px;
  border: 1px solid var(--mm-border);
  border-radius: 12px;
  background: var(--mm-control-muted-bg);
}

.cache-summary span,
.maintenance-result span {
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
}

.cache-summary strong {
  color: var(--mm-text);
  font-weight: var(--mm-font-weight-semibold);
}

.nfo-repair-panel {
  position: relative;
  overflow: hidden;
  display: grid;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: var(--mm-control-muted-bg);
}

.nfo-repair-panel.is-busy {
  border-color: color-mix(in srgb, var(--mm-primary) 34%, var(--mm-border));
}

.busy-button-content {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  white-space: nowrap;
}

.busy-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 999px;
  animation: busy-spin 0.8s linear infinite;
}

.busy-strip {
  position: relative;
  height: 4px;
  overflow: hidden;
  border-radius: 999px;
  background: color-mix(in srgb, var(--mm-primary) 12%, var(--mm-control-bg));
}

.busy-strip i {
  position: absolute;
  top: 0;
  bottom: 0;
  left: -35%;
  width: 35%;
  border-radius: inherit;
  background: linear-gradient(
    90deg,
    transparent,
    color-mix(in srgb, var(--mm-primary) 72%, white),
    transparent
  );
  animation: busy-scan 1.15s ease-in-out infinite;
}

@keyframes busy-spin {
  to {
    transform: rotate(360deg);
  }
}

@keyframes busy-scan {
  0% {
    transform: translateX(0);
  }

  100% {
    transform: translateX(385%);
  }
}

.nfo-repair-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.nfo-repair-head h3 {
  margin: 0;
  color: var(--mm-text);
  font-size: 18px;
  font-weight: var(--mm-font-weight-semibold);
}

.nfo-repair-head p {
  margin: 6px 0 0;
  color: var(--mm-muted);
  line-height: 1.6;
}

.compact-grid {
  gap: 14px;
}

.span-2 {
  grid-column: span 2;
}

.nfo-repair-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.nfo-repair-summary span {
  padding: 6px 10px;
  border: 1px solid var(--mm-border);
  border-radius: 999px;
  background: var(--mm-control-bg);
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
}

.nfo-repair-list {
  display: grid;
  gap: 8px;
  max-height: 260px;
  overflow: auto;
}

.nfo-repair-row {
  display: grid;
  gap: 4px;
  padding: 12px;
  border: 1px solid var(--mm-border);
  border-radius: 12px;
  background: var(--mm-control-bg);
}

.nfo-repair-row strong {
  color: var(--mm-text);
  font-weight: var(--mm-font-weight-semibold);
}

.nfo-repair-row span,
.nfo-repair-row small {
  overflow: hidden;
  color: var(--mm-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.maintenance-result {
  min-height: 0;
}

.form-grid,
.maker-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.form-grid :deep(.mm-field:first-child:has(input[type="checkbox"])) {
  align-self: end;
}

.maker-list {
  display: grid;
  gap: 12px;
}

.maker-row {
  grid-template-columns: minmax(160px, 220px) minmax(0, 1fr) minmax(130px, 160px) auto;
  align-items: end;
}

.identity-tools {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
}

.identity-layout {
  display: grid;
  grid-template-columns: minmax(280px, 380px) minmax(0, 1fr);
  gap: 18px;
  align-items: start;
}

.identity-list {
  display: grid;
  gap: 8px;
  max-height: 620px;
  overflow: auto;
  padding-right: 4px;
}

.identity-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 10px;
  width: 100%;
  padding: 12px;
  border: 1px solid var(--mm-border);
  border-radius: 12px;
  background: var(--mm-control-bg);
  color: var(--mm-text);
  text-align: left;
  cursor: pointer;
}

.identity-row.active {
  border-color: rgba(255, 56, 92, .5);
  background: var(--mm-primary-soft);
}

.identity-row input {
  width: 18px;
  min-height: 18px;
  margin-top: 3px;
}

.identity-row span {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.identity-row strong,
.identity-row small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.identity-row em,
.identity-row small,
.identity-meta {
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
  font-style: normal;
}

.identity-row small {
  grid-column: 2;
}

.identity-editor {
  display: grid;
  gap: 14px;
}

.identity-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.empty-line {
  padding: 24px;
  border: 1px solid var(--mm-border);
  border-radius: 12px;
  color: var(--mm-muted);
  text-align: center;
}

input,
select,
textarea {
  width: 100%;
  min-height: 44px;
  padding: 0 14px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: var(--mm-control-bg);
  color: var(--mm-text);
  font: inherit;
}

textarea {
  min-height: 112px;
  padding: 12px 14px;
  resize: vertical;
}

input[type="checkbox"] {
  width: 22px;
  min-height: 22px;
  accent-color: var(--mm-primary);
}

@media (max-width: 760px) {
  .panel-head,
  .nfo-repair-head,
  .form-grid,
  .maker-row,
  .identity-tools,
  .identity-layout,
  .cache-summary {
    grid-template-columns: 1fr;
    display: grid;
  }

  .span-2 {
    grid-column: auto;
  }
}
</style>
