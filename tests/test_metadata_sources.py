from __future__ import annotations

import unittest
from unittest.mock import patch


class MetadataSourceAlgorithmTest(unittest.TestCase):
    def setUp(self) -> None:
        import app.main as main

        self.main = main

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


if __name__ == "__main__":
    unittest.main()
