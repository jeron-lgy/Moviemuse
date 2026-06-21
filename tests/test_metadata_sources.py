from __future__ import annotations

import unittest
from unittest.mock import patch


class MetadataSourceAlgorithmTest(unittest.TestCase):
    def setUp(self) -> None:
        import app.main as main

        self.main = main

    def test_subscribe_av_endpoint_queues_download_without_sync_mteam(self) -> None:
        import asyncio
        from unittest.mock import Mock

        class Request:
            async def json(self) -> dict[str, object]:
                return {"id": "ABF-358", "title": "cached", "actresses": [{"name": "A"}]}

        class Service:
            def get_settings(self) -> dict[str, object]:
                return {"max_coactors": 2, "javdb_source_enabled": False}

            def subscribe_av(self, payload: dict[str, object]) -> dict[str, object]:
                return dict(payload)

            def get_subscribed_av(self) -> list[dict[str, object]]:
                return []

        background = Mock()
        with patch.object(self.main, "get_subscription_service", return_value=Service()), \
            patch.object(self.main, "download_av_from_mteam", side_effect=AssertionError("download should be background only")), \
            patch.object(self.main, "send_notification_event", return_value=None), \
            patch.object(self.main, "cached_av_detail", return_value={}):
            result = asyncio.run(self.main.api_subscribe_av(Request(), background))

        self.assertEqual(result["download"]["status"], "queued")
        background.add_task.assert_called_once()

    def test_subscribe_actress_endpoint_queues_latest_scan_without_sync_fetch(self) -> None:
        import asyncio
        from unittest.mock import Mock

        class Request:
            async def json(self) -> dict[str, object]:
                return {"id": "7BX1", "name": "西宮夢"}

        class Service:
            def get_settings(self) -> dict[str, object]:
                return {"max_coactors": 2, "javdb_source_enabled": False}

            def get_metadata_cache(self, *_args: object, **_kwargs: object) -> object:
                return None

            def set_metadata_cache(self, *_args: object, **_kwargs: object) -> None:
                return None

            def subscribe_actress(self, payload: dict[str, object]) -> dict[str, object]:
                return dict(payload)

        background = Mock()
        with patch.object(self.main, "get_subscription_service", return_value=Service()), \
            patch.object(self.main, "subscribe_latest_for_actress", side_effect=AssertionError("latest scan should be background only")):
            result = asyncio.run(self.main.api_subscribe_actress(Request(), background))

        self.assertEqual(result["latest"]["status"], "queued")
        background.add_task.assert_called_once()

    def test_subscription_lists_are_newest_first(self) -> None:
        import tempfile
        from pathlib import Path
        from app.subscription_service import SubscriptionService

        with tempfile.TemporaryDirectory() as tmp:
            service = SubscriptionService(Path(tmp))
            clock = {"value": 100.0}

            def tick() -> float:
                clock["value"] += 100.0
                return clock["value"]

            with patch("app.subscription_service.time.time", side_effect=tick):
                service.subscribe_av({"id": "OLD-001", "title": "old"})
                service.subscribe_av({"id": "NEW-001", "title": "new"})
                service.subscribe_actress({"id": "old", "name": "old"})
                service.subscribe_actress({"id": "new", "name": "new"})

            self.assertEqual([item["id"] for item in service.get_subscribed_av()[:2]], ["NEW-001", "OLD-001"])
            self.assertEqual([item["id"] for item in service.get_subscribed_actresses()[:2]], ["new", "old"])

    def test_global_actor_limit_filters_three_actor_items(self) -> None:
        items = [
            {"id": "OK-001", "actresses": [{"name": "A"}, {"name": "B"}]},
            {"id": "NG-001", "actresses": [{"name": "A"}, {"name": "B"}, {"name": "C"}]},
            {"id": "NG-002", "title": "A B C 共演 BEST 総集編"},
        ]

        kept = self.main.filter_avs_by_actor_limit(items, context="unit", max_coactors=2)

        self.assertEqual([item["id"] for item in kept], ["OK-001"])

    def test_canonical_av_id_merges_dmm_suffix_variants(self) -> None:
        self.assertEqual(self.main.canonical_av_id("START579V"), "START-579")
        self.assertEqual(self.main.canonical_av_id("SNOS250BOD"), "SNOS-250")
        self.assertEqual(self.main.canonical_av_id("SNOS093"), "SNOS-093")
        self.assertEqual(self.main.canonical_av_id("SNOS-71"), "SNOS-071")
        self.assertEqual(self.main.canonical_av_id("FWAY085"), "FWAY-085")

    def test_detect_catalog_number_preserves_three_digit_number(self) -> None:
        from app.scanner import detect_catalog_number

        self.assertEqual(detect_catalog_number("SNOS-071-4K.mkv"), "SNOS-071")
        self.assertEqual(detect_catalog_number("SNOS-71.mp4"), "SNOS-071")

    def test_jellyfin_probe_log_does_not_emit_library_notification(self) -> None:
        with patch.object(self.main, "send_notification_event") as sender:
            self.main.notify_from_app_log("info", "jellyfin", "Jellyfin 查重命中，标记已入库", {
                "av_id": "SNOS-093",
                "path": "/media/study_h265/SNOS-093/SNOS-093.chinese.mp4",
            })

            sender.assert_not_called()

    def test_canonical_subscription_av_id_uses_dmm_cid_when_cached_id_lost_zero(self) -> None:
        item = {
            "id": "SNOS-93",
            "source": "dmm",
            "cover": "https://pics.dmm.co.jp/mono/movie/adult/snos093/snos093pl.jpg",
        }
        self.assertEqual(self.main.canonical_subscription_av_id(item), "SNOS-093")

        fway = {
            "id": "FWAY-85",
            "source": "dmm",
            "url": "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=fway085/",
        }
        self.assertEqual(self.main.canonical_subscription_av_id(fway), "FWAY-085")

        padded_internal = {
            "id": "IPZZ-895",
            "source": "dmm",
            "cover": "https://pics.dmm.co.jp/mono/movie/adult/ipzz00895/ipzz00895pl.jpg",
        }
        self.assertEqual(self.main.canonical_subscription_av_id(padded_internal), "IPZZ-895")

    def test_public_metadata_upgrades_dmm_small_cover_url(self) -> None:
        item = self.main.public_metadata_item({
            "id": "ABF-356",
            "cover": "https://pics.dmm.co.jp/mono/movie/adult/118abf356/118abf356ps.jpg",
        })

        self.assertEqual(item["cover"], "https://pics.dmm.co.jp/mono/movie/adult/118abf356/118abf356pl.jpg")
        self.assertIn("118abf356pl.jpg", item["cover_proxy"])

    def test_actor_limit_verification_uses_cached_detail_actors(self) -> None:
        detail = {
            "id": "TEST-001",
            "title": "cached detail",
            "actresses": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
        }

        with patch.object(self.main, "cached_av_detail", return_value=detail):
            result = self.main.actor_limit_verification({"id": "TEST-001", "title": "summary"}, max_coactors=2, context="unit")

        self.assertFalse(result["ok"])
        self.assertEqual(result["actor_count"], 3)
        self.assertIn("超过限制", result["reason"])

    def test_actor_limit_verification_merges_cached_detail_when_allowed(self) -> None:
        detail = {
            "id": "TEST-002",
            "title": "cached detail",
            "cover": "https://pics.dmm.co.jp/mono/movie/adult/test002/test002ps.jpg",
            "actresses": [{"name": "A"}, {"name": "B"}],
        }

        with patch.object(self.main, "cached_av_detail", return_value=detail):
            result = self.main.actor_limit_verification({"id": "TEST-002"}, max_coactors=2, context="unit")

        self.assertTrue(result["ok"])
        self.assertEqual(result["actor_count"], 2)
        self.assertEqual(result["payload"]["title"], "cached detail")
        self.assertEqual([actor["name"] for actor in result["payload"]["actresses"]], ["A", "B"])

    def test_merge_av_sources_preserves_source_chain(self) -> None:
        merged = self.main.merge_av_sources(
            [{"id": "ABF-358", "source": "dmm"}],
            [{"id": "ABF358", "source": "javlibrary"}],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "ABF-358")
        self.assertEqual(merged[0]["source_chain"], ["dmm", "javlibrary"])

    def test_remember_maker_identity_uses_configured_label_sources(self) -> None:
        writes: list[tuple[str, str, dict[str, object]]] = []

        with patch.object(self.main, "cache_set", side_effect=lambda ns, key, value, ttl: writes.append((ns, key, value))):
            payload = self.main.remember_maker_identity("PRESTIGE")

        self.assertEqual(payload["confidence"], "high")
        self.assertIn("ABSOLUTELY FANTASIA", payload["dmm_primary_labels"])
        self.assertIn("vl_label.php?l=aqmuc", payload["javlibrary_urls"][0])
        self.assertTrue(any(ns == "maker_identity" and key == "name:prestige" for ns, key, _ in writes))

    def test_dmm_primary_label_items_are_sorted_before_maker_fallback(self) -> None:
        cases = [
            (
                "PRESTIGE",
                [
                    {"id": "YRK-335", "date": "2026-07-01", "label": "PRESTIGE"},
                    {"id": "ABF-358", "date": "2026-06-10", "label": "ABSOLUTELY FANTASIA"},
                    {"id": "ABF-359", "date": "2026-06-11", "detail": {"label": "ABSOLUTELY FANTASIA"}},
                ],
                ["ABF-359", "ABF-358", "YRK-335"],
            ),
            (
                "S1 NO.1 STYLE",
                [
                    {"id": "OFES-046", "date": "2026-06-30", "label": "oppai"},
                    {"id": "SNOS-341", "date": "2026-06-23", "label": "S1 NO.1 STYLE"},
                ],
                ["SNOS-341", "OFES-046"],
            ),
            (
                "Madonna",
                [
                    {"id": "ROE-511", "date": "2026-06-23", "label": "MONROE"},
                    {"id": "JUR-783", "date": "2026-06-23", "label": "Madonna"},
                ],
                ["JUR-783", "ROE-511"],
            ),
            (
                "SOD Create",
                [
                    {"id": "SDNM-552", "date": "2026-07-09", "label": "青春時代"},
                    {"id": "START-596", "date": "2026-07-09", "label": "SODSTAR"},
                ],
                ["START-596", "SDNM-552"],
            ),
        ]

        for maker_name, items, expected in cases:
            with self.subTest(maker_name=maker_name):
                sorted_items = self.main.sort_maker_listing_items(items, maker_name)
                self.assertEqual([item["id"] for item in sorted_items], expected)

    def test_dmm_primary_label_filter_does_not_fill_with_maker_fallback(self) -> None:
        items = [
            {"id": "YRK-335", "date": "2026-07-01", "label": "PRESTIGE"},
            {"id": "ABF-358", "date": "2026-06-10", "label": "ABSOLUTELY FANTASIA"},
        ]

        filtered = self.main.prioritize_dmm_maker_labels(items, "PRESTIGE", 8)

        self.assertEqual([item["id"] for item in filtered], ["ABF-358"])

    def test_javlibrary_maker_label_scope_wins_dedup_and_filters_compilations(self) -> None:
        urls = [
            "https://www.javlibrary.com/cn/vl_maker.php?m=aa",
            "https://www.javlibrary.com/cn/vl_label.php?l=aqmuc",
        ]

        def fake_listing(url: str, limit: int) -> list[dict[str, object]]:
            if "vl_label" in url:
                return [
                    {"id": "ABF-358", "date": "2026-06-10", "title": "label copy"},
                    {"id": "ABF-359", "date": "2026-06-11", "title": "new label"},
                ]
            return [
                {"id": "ABF-358", "date": "2026-06-10", "title": "maker copy"},
                {"id": "BEST-001", "date": "2026-06-12", "title": "PRESTIGE BEST 総集編"},
            ]

        with patch.dict(self.main.JAVLIBRARY_MAKER_URLS, {"unit maker": urls}, clear=False), \
            patch.object(self.main, "cache_get", return_value=None), \
            patch.object(self.main, "cache_set"), \
            patch.object(self.main.javlibrary, "get_listing_avs", side_effect=fake_listing):
            results = self.main.javlibrary_maker_avs("unit maker", 10)

        self.assertEqual([item["id"] for item in results], ["ABF-359", "ABF-358"])
        self.assertTrue(all(item["source_scope"] == "label" for item in results))
        self.assertEqual(results[1]["title"], "label copy")

    def test_javlibrary_known_makers_have_label_first_fallback(self) -> None:
        expected_label_ids = {
            "S1 NO.1 STYLE": "bvla",
            "PRESTIGE": "aqmuc",
            "IDEA POCKET": "buwq",
            "Madonna": "bvkq",
            "SOD Create": "defa",
        }

        for maker_name, label_id in expected_label_ids.items():
            with self.subTest(maker_name=maker_name):
                urls = self.main.javlibrary_urls_for_maker(maker_name)
                self.assertGreaterEqual(len(urls), 2)
                self.assertIn("vl_label.php", urls[0])
                self.assertIn(f"l={label_id}", urls[0])

    def test_javlibrary_actor_id_can_be_discovered_from_seed_video(self) -> None:
        actors = [
            {"name": "Alice", "star_id": "alice-star"},
            {"name": "Bob", "star_id": "bob-star"},
        ]
        cached_names: list[tuple[str, str]] = []

        with patch.object(self.main, "cache_get", return_value=None), \
            patch.object(self.main, "cache_javlibrary_actor_map", side_effect=lambda name, star_id: cached_names.append((name, star_id))), \
            patch.object(self.main, "javlibrary_video_actresses", return_value=actors):
            star_id = self.main.javlibrary_actor_star_id({"name": "Alice"}, seed_avs=[{"id": "TEST-001", "title": "single work"}])

        self.assertEqual(star_id, "alice-star")
        self.assertIn(("Alice", "alice-star"), cached_names)

    def test_listing_sources_use_javlibrary_when_dmm_primary_label_is_short(self) -> None:
        dmm_items = [
            {"id": "ABF-358", "date": "2026-06-19", "label": "ABSOLUTELY FANTASIA"},
            {"id": "YRK-335", "date": "2026-07-03", "label": "PRESTIGE"},
        ]
        javlibrary_items = [
            {"id": "ABF-356", "date": "2026-06-05", "source": "javlibrary", "source_scope": "label", "label": "ABSOLUTELY FANTASIA"},
        ]

        with patch.object(self.main.dmm, "get_listing_avs", side_effect=[dmm_items, []]), \
            patch.object(self.main.dmm, "get_maker_avs", return_value=[]), \
            patch.object(self.main, "javlibrary_maker_avs", return_value=javlibrary_items), \
            patch.object(self.main.javdb, "get_listing", return_value=[]), \
            patch.object(self.main, "cache_set"), \
            patch.object(self.main, "cached_av_summary", return_value=None):
            results = self.main.fetch_listing_sources("https://example.invalid/list", "PRESTIGE", 3, force_refresh=True)

        self.assertEqual([item["id"] for item in results], ["ABF-358", "ABF-356"])
        self.assertTrue(all(item.get("source_scope") == "label" for item in results))
        self.assertTrue(all(item.get("match_reason") == "primary_label" for item in results))
        self.assertEqual(results[1]["source_chain"], ["javlibrary"])

    def test_listing_cache_fourteen_items_satisfies_first_screen_probe(self) -> None:
        cached = [{"id": f"TEST-{index:03d}", "title": f"cached {index}"} for index in range(14)]

        with patch.object(self.main, "cache_get", return_value=cached), \
            patch.object(self.main, "fetch_listing_sources", side_effect=AssertionError("should not fetch external")):
            results = self.main.cached_listing("https://javdb.com/makers/7R?f=download", limit=15, maker_name="S1 NO.1 STYLE")

        self.assertEqual(len(results), 14)
        self.assertTrue(all(item.get("match_reason") == "sqlite_cache" for item in results))

    def test_listing_cache_twenty_eight_items_satisfies_load_more_probe(self) -> None:
        cached = [{"id": f"TEST-{index:03d}", "title": f"cached {index}"} for index in range(28)]

        with patch.object(self.main, "cache_get", return_value=cached), \
            patch.object(self.main, "fetch_listing_sources", side_effect=AssertionError("should not fetch external")):
            results = self.main.cached_listing("https://javdb.com/makers/7R?f=download", limit=29, maker_name="S1 NO.1 STYLE")

        self.assertEqual(len(results), 28)
        self.assertTrue(all(item.get("match_reason") == "sqlite_cache" for item in results))

    def test_listing_cache_partial_items_still_return_without_external_fetch(self) -> None:
        cached = [{"id": f"TEST-{index:03d}", "title": f"cached {index}"} for index in range(5)]

        with patch.object(self.main, "cache_get", return_value=cached), \
            patch.object(self.main, "fetch_listing_sources", side_effect=AssertionError("should not fetch external")):
            results = self.main.cached_listing("https://javdb.com/makers/7R?f=download", limit=15, maker_name="S1 NO.1 STYLE")

        self.assertEqual(len(results), 5)
        self.assertTrue(all(item.get("match_reason") == "sqlite_cache" for item in results))

    def test_javlibrary_strategy_skips_dmm_when_listing_is_complete(self) -> None:
        javlibrary_items = [
            {"id": f"ABF-{index:03d}", "date": "2026-06-01", "source": "javlibrary", "source_scope": "label"}
            for index in range(1, 4)
        ]

        with patch.object(self.main, "maker_listing_source_strategy", return_value="javlibrary"), \
            patch.object(self.main, "javlibrary_maker_avs", return_value=javlibrary_items), \
            patch.object(self.main.dmm, "get_listing_avs", side_effect=AssertionError("DMM should not be called")), \
            patch.object(self.main.dmm, "get_maker_avs", side_effect=AssertionError("DMM should not be called")), \
            patch.object(self.main.javdb, "get_listing", side_effect=AssertionError("JavDB should not be called")):
            results = self.main.fetch_listing_sources("https://javdb.com/makers/6M?f=download", "PRESTIGE", 3, force_refresh=False)

        self.assertEqual([item["id"] for item in results], ["ABF-003", "ABF-002", "ABF-001"])

    def test_disabled_javdb_source_skips_subscription_search_fallback(self) -> None:
        with patch.object(self.main, "javdb_source_enabled", return_value=False), \
            patch.object(self.main.dmm, "get_actress_avs", return_value=[]), \
            patch.object(self.main, "javlibrary_actor_avs", return_value=[]), \
            patch.object(self.main.javdb, "search_actress", side_effect=AssertionError("JavDB should not be called")):
            results = self.main.fetch_subscription_search("Alice", "actress")

        self.assertEqual(results, [])

    def test_disabled_javdb_source_skips_maker_javdb_strategy(self) -> None:
        with patch.object(self.main, "javdb_source_enabled", return_value=False), \
            patch.object(self.main, "maker_listing_source_strategy", return_value="javdb"), \
            patch.object(self.main.javdb, "get_listing", side_effect=AssertionError("JavDB should not be called")), \
            patch.object(self.main, "javlibrary_maker_avs", return_value=[]), \
            patch.object(self.main.dmm, "get_listing_avs", return_value=[]), \
            patch.object(self.main.dmm, "get_maker_avs", return_value=[]):
            results = self.main.fetch_listing_sources("https://javdb.com/makers/7R?f=download", "S1 NO.1 STYLE", 3, force_refresh=False)

        self.assertEqual(results, [])

    def test_dmm_movie_ranking_html_parser_extracts_rows(self) -> None:
        from app.dmm_service import DMMService

        html = """
        <table>
          <tr><td class="bd-b">
            1
            <a href="/mono/dvd/-/detail/=/cid=h_068mxgs1437dl/"><img src="/digital/video/common/blank.gif" data-src="https://pics.dmm.co.jp/mono/movie/adult/h_068mxgs1437dl/h_068mxgs1437dlpl.jpg" alt="【数量限定】河北彩花の尊い美顔を心おきなく拝みたい。"></a>
            <a href="/mono/dvd/-/list/=/article=maker/id=123/">ナンバーワンスタイル</a> /
            <a href="/mono/dvd/-/list/=/article=actress/id=1044864/">河北彩花（河北彩伽）</a>
            2026/06/24発売
          </td></tr>
        </table>
        """

        rows = DMMService._parse_ranking_html(html, "movie")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rank"], 1)
        self.assertEqual(rows[0]["id"], "MXGS-1437")
        self.assertIn("h_068mxgs1437dlpl.jpg", rows[0]["cover"])
        self.assertEqual(rows[0]["release_date"], "2026-06-24")
        self.assertEqual(rows[0]["maker"], "ナンバーワンスタイル")
        self.assertEqual(rows[0]["actresses"][0]["name"], "河北彩花（河北彩伽）")

    def test_dmm_ranking_normalizes_prefixed_limited_cids(self) -> None:
        from app.dmm_service import DMMService

        cases = {
            "h_346rebd1046tk1": "REBD-1046",
            "n_709maraa244tk": "MARAA-244",
            "ipzz00895": "IPZZ-895",
            "k9snos275": "SNOS-275",
            "snos093": "SNOS-093",
            "fway085": "FWAY-085",
            "ipok026": "IPOK-026",
        }
        for cid, expected in cases.items():
            with self.subTest(cid=cid):
                self.assertEqual(DMMService.normalize_av_id_from_cid(cid), expected)

    def test_dmm_actress_ranking_html_parser_extracts_latest_work(self) -> None:
        from app.dmm_service import DMMService

        html = """
        <table>
          <tr><td class="bd-b">
            4
            <a href="/mono/dvd/-/list/=/article=actress/id=1044864/">河北彩花（河北彩伽）<img src="/digital/video/common/blank.gif" data-original="https://pics.dmm.co.jp/mono/actjpgs/medium/kawakita_saika.jpg" alt=""></a>
            最新作 ： <a href="/mono/dvd/-/detail/=/cid=k9snos275/">【予約】 河北彩花の尊い美顔を心おきなく拝みたい。</a>
            発売日 ： 2026/06/24
            商品数 ： 328
          </td></tr>
        </table>
        """

        rows = DMMService._parse_ranking_html(html, "actress")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rank"], 4)
        self.assertEqual(rows[0]["name"], "河北彩花（河北彩伽）")
        self.assertIn("kawakita_saika.jpg", rows[0]["cover"])
        self.assertEqual(rows[0]["latest_av_id"], "SNOS-275")
        self.assertEqual(rows[0]["latest_release_date"], "2026-06-24")
        self.assertEqual(rows[0]["product_count"], 328)


if __name__ == "__main__":
    unittest.main()
