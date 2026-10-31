from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch



from src.plugins.ai_chat.media_library import (
    MediaLibrary,
    MediaLibraryError,
    MediaRecord,
    choose_sticker_candidate,
    requests_sticker_variation,
)


class MediaLibraryParsingTests(unittest.TestCase):
    def test_parses_structured_vision_response(self) -> None:
        result = MediaLibrary._parse_analysis(
            json.dumps(
                {
                    "summary": "卷毛小狗悠闲躺窝",
                    "description": "棕色卷毛小狗四脚朝天地躺在宠物窝中。",
                    "text": "",
                    "subjects": ["卷毛小狗", "宠物窝"],
                    "actions": ["躺着"],
                    "emotion": ["放松", "可爱"],
                    "usage": ["卖萌", "表达惬意"],
                    "is_sticker": False,
                    "contains_person": False,
                    "contains_private_info": False,
                    "safety": "safe",
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(result["summary"], "卷毛小狗悠闲躺窝")
        self.assertEqual(result["emotion"], ["放松", "可爱"])
        self.assertEqual(result["subjects"], ["卷毛小狗", "宠物窝"])
        self.assertEqual(result["actions"], ["躺着"])
        self.assertEqual(result["safety"], "safe")

    def test_invalid_or_incomplete_response_is_rejected(self) -> None:
        with self.assertRaises(MediaLibraryError):
            MediaLibrary._parse_analysis("not-json")
        with self.assertRaises(MediaLibraryError):
            MediaLibrary._parse_analysis('{"summary":"只有标题"}')

    def test_unknown_safety_is_downgraded_to_review(self) -> None:
        result = MediaLibrary._parse_analysis(
            '{"summary":"测试图片标签","description":"一张测试图片。",'
            '"safety":"maybe"}'
        )
        self.assertEqual(result["safety"], "review")

    def test_storage_path_cannot_escape_root(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "outside-media-test.jpg"
            outside.write_bytes(b"test")
            library = object.__new__(MediaLibrary)
            library.root = root.resolve()
            try:
                with self.assertRaises(MediaLibraryError):
                    library._resolve_storage_path(str(outside))
            finally:
                outside.unlink(missing_ok=True)

    def test_missing_hot_media_is_restored_from_verified_archive(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            hot_root = root / "hot"
            archive_root = root / "archive"
            content = b"archived image bytes"
            archive_relative = "media/blobs/aa/example.png"
            archive = archive_root / archive_relative
            archive.parent.mkdir(parents=True)
            archive.write_bytes(content)

            library = object.__new__(MediaLibrary)
            library.root = hot_root.resolve()
            library.archive_root = archive_root.resolve()
            restored = library._resolve_storage_path(
                "blobs/aa/example.png",
                archive_relative=archive_relative,
                expected_sha256=hashlib.sha256(content).hexdigest(),
                expected_size=len(content),
            )

            self.assertEqual(restored.read_bytes(), content)

    def test_mime_sniffing_accepts_known_images_only(self) -> None:
        self.assertEqual(MediaLibrary._sniff_mime(b"\xff\xd8\xffx"), "image/jpeg")
        self.assertEqual(
            MediaLibrary._sniff_mime(b"\x89PNG\r\n\x1a\n"),
            "image/png",
        )
        self.assertEqual(
            MediaLibrary._sniff_mime(b"plain text"),
            "application/octet-stream",
        )

    def test_only_qq_image_hosts_are_downloadable(self) -> None:
        self.assertTrue(
            MediaLibrary._supported_source("https://multimedia.nt.qq.com.cn/a.jpg")
        )
        self.assertTrue(
            MediaLibrary._supported_source("https://gchat.qpic.cn/a.jpg")
        )
        self.assertFalse(
            MediaLibrary._supported_source("http://127.0.0.1/private")
        )
        self.assertFalse(
            MediaLibrary._supported_source("https://qq.com.example.test/a.jpg")
        )

    def test_only_platform_stickers_enter_the_durable_library(self) -> None:
        self.assertTrue(MediaLibrary._is_sticker("mface", {}))
        self.assertTrue(MediaLibrary._is_sticker("image", {"subType": "1"}))
        self.assertFalse(MediaLibrary._is_sticker("image", {"subType": "0"}))
        self.assertFalse(MediaLibrary._is_sticker("image", {"summary": "普通照片"}))

    def test_catgirl_alias_ranks_cat_ears_above_unrelated_cute_sticker(self) -> None:
        def record(media_id: int, summary: str) -> MediaRecord:
            return MediaRecord(
                media_id=media_id,
                handle=f"media#{media_id}",
                summary=summary,
                description="",
                extracted_text="",
                emotions=(),
                usage=(),
                is_sticker=True,
                safety="safe",
                storage_path=Path("unused"),
                mime_type="image/png",
            )

        terms = MediaLibrary._sticker_query_terms("发个猫娘卖萌表情")
        catgirl = MediaLibrary._sticker_relevance(
            record(1, "可爱猫耳精灵少女卖萌"),
            terms,
        )
        rabbit = MediaLibrary._sticker_relevance(
            record(2, "粉色兔耳少女闭眼卖萌"),
            terms,
        )
        required = MediaLibrary._sticker_required_alias_groups(
            "发个猫娘卖萌表情"
        )

        self.assertGreater(catgirl, rabbit)
        self.assertTrue(
            MediaLibrary._record_matches_sticker_groups(
                record(1, "可爱猫耳精灵少女卖萌"),
                required,
            )
        )
        self.assertFalse(
            MediaLibrary._record_matches_sticker_groups(
                record(2, "粉色兔耳少女闭眼卖萌"),
                required,
            )
        )

    def test_generic_sticker_request_is_treated_as_random(self) -> None:
        self.assertEqual(
            MediaLibrary._sticker_query_terms("给我发个表情包"),
            (),
        )

    def test_matching_candidates_are_not_forced_to_the_highest_score(self) -> None:
        top = MediaRecord(
            media_id=1,
            handle="media#1",
            summary="最相关",
            description="",
            extracted_text="",
            emotions=(),
            usage=(),
            is_sticker=True,
            safety="safe",
            storage_path=Path("unused"),
            mime_type="image/png",
            score=1.0,
        )
        alternative = MediaRecord(
            **{
                **top.__dict__,
                "media_id": 2,
                "handle": "media#2",
                "summary": "同样匹配的候选",
                "score": 0.72,
            }
        )

        with patch(
            "src.plugins.ai_chat.media_library.random.choices",
            return_value=[alternative],
        ) as choices:
            selected = choose_sticker_candidate([top, alternative], now=1000)

        self.assertIs(selected, alternative)
        self.assertEqual(choices.call_args.args[0], [top, alternative])

    def test_recent_sticker_is_avoided_when_an_alternative_matches(self) -> None:
        recent = MediaRecord(
            media_id=1,
            handle="media#1",
            summary="刚发过",
            description="",
            extracted_text="",
            emotions=(),
            usage=(),
            is_sticker=True,
            safety="safe",
            storage_path=Path("unused"),
            mime_type="image/png",
            score=1.0,
            last_sent_at=900,
        )
        alternative = MediaRecord(
            **{
                **recent.__dict__,
                "media_id": 2,
                "handle": "media#2",
                "summary": "没有刚发过",
                "score": 0.7,
                "last_sent_at": None,
            }
        )

        selected = choose_sticker_candidate([recent, alternative], now=1000)

        self.assertIs(selected, alternative)

    def test_explicit_variation_does_not_reuse_only_recent_candidate(self) -> None:
        recent = MediaRecord(
            media_id=1,
            handle="media#1",
            summary="刚发过",
            description="",
            extracted_text="",
            emotions=(),
            usage=(),
            is_sticker=True,
            safety="safe",
            storage_path=Path("unused"),
            mime_type="image/png",
            score=1.0,
            last_sent_at=900,
        )

        selected = choose_sticker_candidate(
            [recent],
            now=1000,
            allow_recent_fallback=False,
        )

        self.assertIsNone(selected)

    def test_variation_request_and_query_fillers_are_recognized(self) -> None:
        self.assertTrue(requests_sticker_variation("再换一个抽象的"))
        self.assertTrue(requests_sticker_variation("来张别的"))
        self.assertFalse(requests_sticker_variation("发个抽象表情"))
        self.assertEqual(
            MediaLibrary._sticker_query_terms("换个猫猫表情"),
            ("猫猫", "猫", "猫咪", "橘猫"),
        )
        self.assertEqual(
            MediaLibrary._sticker_query_terms("再换一个更抽象的"),
            ("抽象",),
        )

    def test_exact_sticker_label_ranks_above_broader_alias(self) -> None:
        def record(media_id: int, summary: str) -> MediaRecord:
            return MediaRecord(
                media_id=media_id,
                handle=f"media#{media_id}",
                summary=summary,
                description="",
                extracted_text="",
                emotions=(),
                usage=(),
                is_sticker=True,
                safety="safe",
                storage_path=Path("unused"),
                mime_type="image/png",
            )

        terms = MediaLibrary._sticker_query_terms("橘猫")
        orange_cat = MediaLibrary._sticker_relevance(
            record(1, "震惊张嘴的橘猫表情"),
            terms,
        )
        generic_cat = MediaLibrary._sticker_relevance(
            record(2, "猫咪挥拳挑衅表情包"),
            terms,
        )

        self.assertGreater(orange_cat, generic_cat)

    def test_structured_subject_and_action_tags_have_highest_weight(self) -> None:
        exact = MediaRecord(
            media_id=1,
            handle="media#1",
            summary="聊天表情",
            description="",
            extracted_text="",
            emotions=(),
            usage=(),
            is_sticker=True,
            safety="safe",
            storage_path=Path("unused"),
            mime_type="image/png",
            subjects=("橘猫",),
            actions=("挥拳",),
        )
        broad = MediaRecord(
            media_id=2,
            handle="media#2",
            summary="橘猫在画面里",
            description="",
            extracted_text="",
            emotions=(),
            usage=(),
            is_sticker=True,
            safety="safe",
            storage_path=Path("unused"),
            mime_type="image/png",
        )
        terms = MediaLibrary._sticker_query_terms("橘猫挥拳")

        self.assertGreater(
            MediaLibrary._sticker_relevance(exact, terms),
            MediaLibrary._sticker_relevance(broad, terms),
        )


if __name__ == "__main__":
    unittest.main()
