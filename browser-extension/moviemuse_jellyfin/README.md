# moviemuse_jellyfin

Chrome/Edge MV3 插件初版，在 Jellyfin 媒体详情页注入 `发送字幕` 和 `发送转码` 两个按钮。

## 本地加载

1. 打开 Chrome/Edge 的扩展管理页。
2. 开启开发者模式。
3. 选择“加载已解压的扩展程序”。
4. 选择本目录：`browser-extension/moviemuse_jellyfin`。

## 设置

- API 地址默认是 `http://127.0.0.1:18183`。
- 如果 MovieMuse 配置了 `SUBTITLE_API_TOKEN`，在插件设置页填写同一个 API Key。
- 在 Jellyfin 媒体详情页可以点扩展图标发送任务，也可以使用页面右下角的浮动按钮。

## 调用的 MovieMuse API

- `POST /api/integrations/jellyfin/resolve`
- `POST /api/integrations/jellyfin/subtitle`
- `POST /api/integrations/jellyfin/transcode`

插件只发送 Jellyfin item id 和页面标题，不直接移动、删除或修改媒体文件。
