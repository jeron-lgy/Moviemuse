# JavLibrary probe

这个目录是临时实验目录，没有被当前项目引用。

## 结论

- 当前环境下，`requests` 直连 `https://www.javlibrary.com/...` 返回 Cloudflare challenge 页面，状态码是 `403`，不是可解析的 JavLibrary 正文。
- Playwright headless 打开同一 URL 也停在挑战页，页面标题是 `请稍候...`。
- FlareSolverr + Docker 可以抓到 JavLibrary 正文。本机 `8191/8192` 端口被 Windows 保留，所以临时容器映射在 `127.0.0.1:8281`。

## 页面结构

女优作品页通常是：

```text
https://www.javlibrary.com/cn/vl_star.php?s=<star_id>
```

可解析的作品列表结构一般是：

```text
div.video
  a[href*='?v=']
  .id       -> 番号
  .title    -> 标题
```

页面右侧显示 `所有的影片（依发行日）`，所以第一页第一个 `div.video` 可作为该女优页当前按发行日排序的最新可见番号。

当前 HTML 链接形态是：

```text
div.video
  a[href$='.html']
  .id       -> 番号
  .title    -> 标题
```

## 运行

直连请求版：

```powershell
python .tmp-javlibrary-probe\probe_javlibrary.py --url "https://www.javlibrary.com/cn/vl_star.php?s=<star_id>" --limit 5
```

浏览器会话版：

```powershell
python .tmp-javlibrary-probe\probe_javlibrary_browser.py --url "https://www.javlibrary.com/cn/vl_star.php?s=<star_id>" --limit 5
```

如果浏览器出现验证页，手动完成验证后回到终端按 Enter，脚本会继续解析当前 DOM。

FlareSolverr 版：

```powershell
docker run -d --name tmp-javlibrary-flaresolverr -p 127.0.0.1:8281:8191 ghcr.io/flaresolverr/flaresolverr:latest
python .tmp-javlibrary-probe\probe_javlibrary_flaresolverr.py --star-id aagbe --limit 5 --timeout-ms 120000
```

默认避让策略：

- `429/520/522/524`：按可重试错误处理。
- 每次重试都会换一个 FlareSolverr session，并先访问首页预热。
- 默认 `--retries 3 --base-delay 8 --max-delay 90 --cooldown 2`，即指数退避、随机抖动、成功后冷却。
- 保持串行抓取，不并发打 JavLibrary。

成功样例：

```text
DVMM-315
SPE-006
TKD-058
SUJI-280
HUNTC-242
```

涼森れむ反查样例：

```powershell
# 已知旧作 BGN-054 -> 详情页演员链接 -> star_id aeqfy
python .tmp-javlibrary-probe\probe_javlibrary_flaresolverr.py --star-id aeqfy --limit 10

# 最新作品 ABF-358 的发行商 ABSOLUTELY FANTASIA
python .tmp-javlibrary-probe\probe_javlibrary_flaresolverr.py --url "https://www.javlibrary.com/cn/vl_label.php?l=aqmuc" --limit 20
```

停止临时容器：

```powershell
docker stop tmp-javlibrary-flaresolverr
docker rm tmp-javlibrary-flaresolverr
```
