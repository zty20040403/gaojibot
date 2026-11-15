"""Sticker lexical ranking: several words, synonyms, and the asker's own word first."""
from types import SimpleNamespace
import unittest

from src.plugins.ai_chat.media_library import MediaLibrary


def sticker(summary, emotions=(), subjects=(), usage=()):
    return SimpleNamespace(summary=summary, description="", extracted_text="", subjects=list(subjects),
                           actions=[], emotions=list(emotions), usage=list(usage))


def score(record, query):
    if not MediaLibrary._record_matches_sticker_groups(record, MediaLibrary._sticker_required_alias_groups(query)):
        return 0.0
    return MediaLibrary._sticker_relevance(record, MediaLibrary._sticker_query_terms(query),
                                           MediaLibrary._sticker_own_terms(query))


class StickerSearchTermTests(unittest.TestCase):
    def test_a_sentence_of_words_matches_each_word(self):
        cool = sticker("红发墨镜卡通头像", emotions=["酷", "搞怪"], subjects=["墨镜"])
        baby = sticker("宝宝露出调皮微笑", emotions=["开心", "得意"])
        query = "酷炫 墨镜 帅气 装逼"
        self.assertIn("墨镜", MediaLibrary._sticker_query_terms(query))
        self.assertGreater(score(cool, query), 0.5)
        self.assertEqual(score(baby, query), 0.0)

    def test_synonyms_find_cool_stickers_for_slang(self):
        cool = sticker("戴墨镜的灰发卡通头像", emotions=["酷", "冷淡"])
        for query in ("拽", "帅", "装逼", "酷炫"):
            with self.subTest(query=query):
                self.assertGreater(score(cool, query), 0.0)

    def test_the_askers_own_word_beats_a_synonym(self):
        exact = sticker("粉色猫娘开心回应")
        synonym = sticker("猫耳少女可爱卖萌特写", subjects=["猫耳少女"])
        self.assertGreater(score(exact, "猫娘"), score(synonym, "猫娘"))
        self.assertGreater(score(synonym, "猫娘"), 0.0)

    def test_several_words_are_alternatives(self):
        tired = sticker("懒得吐槽的蘑菇表情", emotions=["嫌弃", "无奈"])
        self.assertGreater(score(tired, "嫌弃 无语"), 0.0)
        # One word still needs every synonym group it names.
        self.assertEqual(score(sticker("开心的小狗", emotions=["开心"]), "开心猫"), 0.0)


if __name__ == "__main__":
    unittest.main()
