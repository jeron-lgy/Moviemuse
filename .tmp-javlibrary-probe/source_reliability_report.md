# Data source reliability comparison

Run date: 2026-06-07

## Test targets

- Actresses: `桜空もも`, `野々浦暖`
- Makers: `IDEA POCKET`, `PRESTIGE`
- Sources:
  - JavLibrary through FlareSolverr
  - Local `DMMService`
  - Local `JavDBService`
  - Product-level SQLite metadata cache path

## Local cache state

SQLite `data/subscriptions.sqlite3`:

- `subscription_metadata_cache`: 1434 rows
- `av_summary`: 809
- `av_detail`: 515
- `actress_avs`: 34
- `listing`: 25
- `subscription_search`: 39
- `actress_profile`: 12

Service file caches:

- `data/dmm-cache`: 442 JSON files
- `data/javdb-cache`: 1 JSON file

## Raw source test

| Source | Success | Typical latency | Result |
| --- | ---: | ---: | --- |
| JavLibrary + FlareSolverr | 4/4 | ~16s/request | Worked for actress and maker pages. Requires Docker + FlareSolverr. |
| DMMService | 4/4 | ~0-13ms | All tested paths were warm file-cache hits. No network request was needed. |
| JavDBService | 0/4 | <1s fail-fast | Current environment is access-limited by JavDB. Service detected the ban and skipped retry. |

## Important findings

- DMM is currently the easiest and most reliable source in this workspace because it has a large warm disk cache and official-style maker/actress listing URLs.
- JavLibrary works as a fallback, but it has no project-integrated cache yet. It needs FlareSolverr and stable stored IDs such as `star_id`, `vl_maker.php?m=...`, or `vl_label.php?l=...`.
- JavDB is already designed defensively, but the current environment is blocked. With only 1 disk-cache file, it provides little fallback value until the proxy/browser access issue is fixed.
- Product-level SQLite cache is the strongest reliability layer: cached actress search returned in 1-5ms; high-level actress AV loading returned in ~43-46ms from DMM/file cache and filters.

## High-level cache path sample

`cached_subscription_search("桜空もも", "actress")`:

- 0.005s
- latest: `IPZZ-850`

`cached_subscription_search("野々浦暖", "actress")`:

- 0.0012s
- latest: `ABF-359`

`subscription_avs_for_actress(...)` after cached identity:

- `桜空もも`: 0.0455s, 9 filtered items
- `野々浦暖`: 0.0428s, 8 filtered items

## Recommendation

Use source priority:

1. SQLite `subscription_metadata_cache`
2. DMMService file cache / DMM live
3. JavLibrary + FlareSolverr fallback
4. JavDB only when access is healthy or a matching disk/SQLite cache exists

For JavLibrary integration, add a small SQLite-backed namespace such as:

- `javlibrary_actor_map`: name/code -> `star_id`
- `javlibrary_actor_avs`: `star_id` -> parsed list
- `javlibrary_maker_avs`: `vl_maker`/`vl_label` URL -> parsed list

Use conservative TTLs:

- Actress and maker latest lists: 6-12h
- Detail pages and actor/maker ID mapping: 30d
- On 429/520/challenge: return stale cache if available, otherwise rotate session and retry with exponential backoff.
