# 濯掍綋宸ュ叿绠憋細鍘婚噸 + 瀛楀箷绠楀姏鏈嶅姟

杩欐槸涓€涓潰鍚?Windows 涓绘満鍜屽眬鍩熺綉璁惧鐨?FastAPI 宸ュ叿銆傜幇鏈夌殑鐢靛奖鍘婚噸鍔熻兘浼氫繚鐣欙紝鏂板鐨勫瓧骞曠畻鍔涙湇鍔″彲浠ヨ Unraid/NAS 鎶婅棰戜换鍔℃彁浜ょ粰杩欏彴 Windows 鏈哄櫒锛岀敱鏈満 CPU/GPU 璺?Whisper 鐢熸垚瀛楀箷骞剁炕璇戙€?
## 鍔熻兘

- Web 椤甸潰锛歚/` 鏄數褰卞幓閲嶅伐鍏凤紝`/subtitles` 鏄瓧骞曚换鍔￠〉闈€?- 灞€鍩熺綉 API锛歎nraid 鍙互閫氳繃 HTTP 鎻愪氦瑙嗛璺緞鎴栦笂浼犺棰戙€?- Whisper 杞啓锛氶粯璁や娇鐢?`faster-whisper`锛屾敮鎸?CUDA銆?- 瀛楀箷杈撳嚭锛氬師鏂?`.srt` / `.vtt`锛岀炕璇?`.srt` / `.vtt`锛屽弻璇?`.srt`銆?- 璺緞鏄犲皠锛氭敮鎸佹妸 Unraid 鐨?`/mnt/user/...` 鏄犲皠涓?Windows 鍙闂殑 `Z:\...` 鎴?`\\192.168.2.9\...`銆?- 缈昏瘧鍚庣锛氭敮鎸?OpenAI 鍏煎鎺ュ彛銆丩ibreTranslate锛屾垨鏈湴 Argos Translate銆?
## 鏈満寮€鍙戞ā寮?
寮€鍙戦樁娈靛缓璁袱涓閮借窇鍦ㄨ繖鍙?Windows 涓婏細

- 鎺у埗鍙?UI锛歚http://127.0.0.1:18180`
- 绠楀姏鍚庣锛歚http://127.0.0.1:18181`

鎺у埗鍙颁細閫氳繃 `SUBTITLE_BACKEND_URL=http://127.0.0.1:18181` 璋冪敤鏈満绠楀姏鍚庣銆傝繖涓ā寮忓拰浠ュ悗 Docker 鎺у埗鍙拌皟鐢?Windows 5090 鍚庣鐨勯摼璺竴鑷达紝浣嗚皟璇曟洿绠€鍗曘€?
棣栨瀹夎渚濊禆锛?
```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

涓€閿惎鍔ㄤ袱涓锛?
```text
.\start_local_dev.bat
```

鍗曠嫭鍚姩锛?
```text
.\start_local_backend.bat
.\start_local_console.bat
```

鏈満寮€鍙戦粯璁ゆ壂鎻忥細

```text
sample-media
```

榛樿鏁版嵁鐩綍浼氬垎寮€锛?
```text
data\local-console
data\local-backend
```

杩欐牱 UI 鐘舵€佸拰鍚庣瀛楀箷浠诲姟璁板綍涓嶄細娣峰湪涓€璧枫€?
## Windows 鍗曠蹇€熷惎鍔?
`start_dev.bat` 浠嶇劧淇濈暀锛岀敤浜庡彧鍚姩涓€涓寘鍚?UI 鍜屾湰鍦板瓧骞曞悗绔殑鏈嶅姟锛?
```powershell
.\start_dev.bat
```

鎵撳紑锛?
```text
http://127.0.0.1:18180
```

## 鎺ㄨ崘閰嶇疆锛?3900K + 5090

寤鸿浣跨敤 CUDA锛?
```powershell
$env:WHISPER_MODEL='large-v3'
$env:WHISPER_DEVICE='cuda'
$env:WHISPER_COMPUTE_TYPE='float16'
$env:SUBTITLE_MAX_WORKERS='1'
.\start_dev.bat
```

`5090` 鏄惧瓨寰堝己锛屼絾瑙嗛杞啓閫氬父鍗曚换鍔″凡缁忚兘鍚冩弧妯″瀷鎺ㄧ悊璧勬簮銆傚缓璁厛淇濇寔 `SUBTITLE_MAX_WORKERS=1`锛岀‘璁ょǔ瀹氬悗鍐嶅皾璇曞苟鍙戙€?
## 缁?Unraid 璋冪敤

### 鏂瑰紡涓€锛氬叡浜矾寰勬彁浜わ紝鎺ㄨ崘

鍋囪 Unraid 閲岀殑瑙嗛璺緞鏄細

```text
/mnt/user/media/Inception.mkv
```

Windows 涓婂悓涓€涓叡浜洰褰曟槧灏勪负锛?
```text
\\UNRAID\media\Inception.mkv
```

鍚姩鏈嶅姟鍓嶉厤缃細

```powershell
$env:SUBTITLE_PATH_MAP='/mnt/user/media=\\UNRAID\media'
$env:SUBTITLE_API_TOKEN='change-me'
.\start_dev.bat
```

Unraid 璋冪敤锛?
```bash
curl -X POST "http://WINDOWS_IP:18181/api/subtitle/jobs" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: change-me" \
  -d '{
    "video_path": "/mnt/user/media/Inception.mkv",
    "target_language": "zh",
    "translate": true,
    "model": "large-v3"
  }'
```

鏌ヨ浠诲姟锛?
```bash
curl -H "X-API-Key: change-me" "http://WINDOWS_IP:18181/api/subtitle/jobs/JOB_ID"
```

涓嬭浇鍙岃瀛楀箷锛?
```bash
curl -L -H "X-API-Key: change-me" \
  "http://WINDOWS_IP:18181/api/subtitle/jobs/JOB_ID/files/bilingual_srt" \
  -o Inception.bilingual.srt
```

### 鏂瑰紡浜岋細HTTP 涓婁紶瑙嗛

```bash
curl -X POST "http://WINDOWS_IP:18181/api/subtitle/upload" \
  -H "X-API-Key: change-me" \
  -F "file=@/mnt/user/media/Inception.mkv" \
  -F "target_language=zh" \
  -F "translate=true"
```

澶ц棰戞洿寤鸿浣跨敤鈥滃叡浜矾寰勬彁浜も€濓紝閬垮厤灞€鍩熺綉閲嶅浼犺緭銆?
## 缈昏瘧閰嶇疆

榛樿浼樺厛浣跨敤 Google 鍏嶈垂缈昏瘧鎺ュ彛锛屼笉闇€瑕?API Key锛涗篃鍙互鍦ㄧ畻鍔涚璁剧疆椤靛垏鎹负 DeepSeek/OpenAI銆佺櫨搴︺€丱llama銆丩ibreTranslate 鎴栧彧鐢熸垚鍘熸枃 SRT銆?
### OpenAI 鍏煎缈昏瘧鎺ュ彛

閫傚悎鎺ユ湰鍦?LLM 缃戝叧鎴栧吋瀹?`/chat/completions` 鐨勬湇鍔★細

```powershell
$env:TRANSLATE_OPENAI_BASE_URL='http://127.0.0.1:1234/v1'
$env:TRANSLATE_OPENAI_API_KEY='local-key'
$env:TRANSLATE_OPENAI_MODEL='your-model'
```

### LibreTranslate

```powershell
$env:LIBRETRANSLATE_URL='http://127.0.0.1:5000'
$env:LIBRETRANSLATE_API_KEY=''
```

### Argos Translate

瀹夎 `argostranslate` 鍜屽搴旇瑷€鍖呭悗浼氳嚜鍔ㄥ皾璇曚娇鐢ㄣ€?
## 涓昏鐜鍙橀噺

| 鍙橀噺 | 榛樿鍊?| 璇存槑 |
|---|---|---|
| `HOST` | `0.0.0.0` | 鐩戝惉鍦板潃锛屽眬鍩熺綉璋冪敤闇€瑕?`0.0.0.0` |
| `PORT` | `18180` | 鏈嶅姟绔彛 |
| `MEDIA_DIRS` | `sample-media` | 鍘婚噸宸ュ叿鎵弿鐩綍锛屽涓洰褰曠敤 `;` 鍒嗛殧 |
| `TRASH_DIR` | `trash` | 鍘婚噸宸ュ叿鍥炴敹绔欑洰褰?|
| `APP_DATA_DIR` | `data` | SQLite銆佷换鍔¤褰曘€佷笂浼犵紦瀛樼洰褰?|
| `WHISPER_MODEL` | `large-v3` | Whisper 妯″瀷鍚嶆垨鏈湴妯″瀷鏂囦欢澶瑰悕 |
| `WHISPER_MODEL_DIR` | `APP_DATA_DIR\whisper-models` | faster-whisper 妯″瀷涓嬭浇鍜屾浛鎹㈢洰褰?|
| `WHISPER_DEVICE` | `cuda` | `cuda` 鎴?`cpu` |
| `WHISPER_COMPUTE_TYPE` | `float16` | CUDA 鎺ㄨ崘 `float16` |
| `SUBTITLE_OUTPUT_DIR` | 绌?| 绌鸿〃绀哄瓧骞曞啓鍒拌棰戝悓鐩綍 |
| `SUBTITLE_PATH_MAP` | 绌?| 渚嬶細`/mnt/user/media=\\UNRAID\media` |
| `SUBTITLE_API_TOKEN` | 绌?| 璁剧疆鍚?API 闇€瑕?`X-API-Key` 鎴?Bearer token |
| `SUBTITLE_MAX_WORKERS` | `1` | 瀛楀箷浠诲姟骞跺彂鏁?|
| `GOOGLE_TRANSLATE_URL` | `https://translate.googleapis.com/translate_a/single` | 榛樿 Google 鍏嶈垂缈昏瘧鎺ュ彛 |
| `BAIDU_TRANSLATE_APP_ID` / `BAIDU_TRANSLATE_SECRET` | 绌?| 鐧惧害缈昏瘧 API |
| `TRANSLATE_OPENAI_BASE_URL` / `TRANSLATE_OPENAI_API_KEY` / `TRANSLATE_OPENAI_MODEL` | 绌?| DeepSeek/OpenAI 鍏煎缈昏瘧 |
| `OLLAMA_URL` / `OLLAMA_TRANSLATE_MODEL` | 绌?/ `qwen2.5:7b` | 鏈湴 Ollama 缈昏瘧 |

## Windows 绠楀姏绔缃〉

鍚姩 Windows 鍚庣鍚庢墦寮€锛?
```text
http://127.0.0.1:18181/terminal
```

杩欓噷鍙互璁剧疆 Whisper 妯″瀷銆佹ā鍨嬬洰褰曘€丆UDA 璁＄畻绫诲瀷銆佸苟鍙戞暟鍜岀炕璇戝悗绔€傝缃細淇濆瓨鍒帮細

```text
data\local-backend\compute_settings.json
```

Whisper 妯″瀷榛樿闆嗕腑鏀惧湪锛?
```text
data\local-backend\whisper-models
```

鍙互璁?`faster-whisper` 棣栨杩愯鏃惰嚜鍔ㄤ笅杞斤紝涔熷彲浠ユ墜鍔ㄤ笅杞?Systran 鐨?faster-whisper 妯″瀷鏂囦欢澶瑰悗鏀惧埌杩欎釜鐩綍銆傚父鐢ㄩ摼鎺ワ細

- [faster-whisper GitHub](https://github.com/SYSTRAN/faster-whisper)
- [faster-whisper large-v3](https://huggingface.co/Systran/faster-whisper-large-v3)
- [faster-whisper large-v3-turbo](https://huggingface.co/Systran/faster-whisper-large-v3-turbo)

## FastAPI 鎵嬪姩杩愯

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 18180
```

## Docker 鎵撳寘

寮€鍙戞祴璇曞畬鎴愬悗锛屽啀鎶婃帶鍒跺彴/UI 鍗曠嫭鎵撳寘鎴?Docker 闀滃儚鏀惧埌 Unraid銆侱ocker 鎺у埗鍙板彧璐熻矗鎵弿銆佸睍绀恒€佹彁浜や换鍔″拰浠ｇ悊涓嬭浇瀛楀箷锛屽瓧骞曠畻鍔涗粛鐒朵氦缁?Windows 5090 鍚庣銆?
```bash
docker compose -f deploy/unraid-frontend/docker-compose.yml up -d --build
```

Unraid 瀹瑰櫒鐜鍙橀噺锛?
| 鍙橀噺 | 璇存槑 |
|---|---|
| `SUBTITLE_BACKEND_URL` | Windows 5090 鍚庣鍦板潃锛屼緥濡?`http://WINDOWS-IP:18181` |
| `SUBTITLE_BACKEND_TOKEN` | 濡傛灉 Windows 鍚庣璁剧疆浜?`SUBTITLE_API_TOKEN`锛岃繖閲屽～鍚屼竴涓?token |
| `SUBTITLE_API_TOKEN` | Unraid 瀹瑰櫒鑷韩瀵瑰 API token锛屽彲鐣欑┖浠呭唴缃戜娇鐢?|

Unraid 鐩稿叧鏂囦欢闆嗕腑鍦細

```text
deploy/unraid-frontend
```

Windows 鍚庣鍚姩绀轰緥闆嗕腑鍦細

```text
deploy/windows-backend
```

Windows 鍚庣浠嶇劧闇€瑕侀厤缃矾寰勬槧灏勶紝璁╁畠鑳芥妸 Unraid 璺緞鎹㈡垚 Windows 鍙闂矾寰勶細

```powershell
$env:SUBTITLE_PATH_MAP='/mnt/user/media=\\UNRAID\media'
.\start_local_backend.bat
```

鎴栬€呬娇鐢ㄦ槧灏勭洏锛?
```powershell
$env:SUBTITLE_PATH_MAP='/mnt/user/media=Z:\media'
.\start_local_backend.bat
```
