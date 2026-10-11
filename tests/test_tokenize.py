"""CJK 分词纯函数单测（text-cjk-tokenization 1.1/1.5）。"""
from fd_text_core.tokenize import cjk_tokens, cjk_tokens_text, cjk_tsquery


class TestCjkTokens:
    def test_bigram_and_unigram_produced(self):
        toks = cjk_tokens("竇娥冤")
        assert "竇" in toks and "娥" in toks and "冤" in toks  # unigram
        assert "竇娥" in toks and "娥冤" in toks  # bigram

    def test_single_char_run(self):
        assert cjk_tokens("冤") == ["冤"]

    def test_english_words_kept(self):
        toks = cjk_tokens("Hamlet is a tragedy")
        assert "Hamlet" in toks and "tragedy" in toks

    def test_mixed_script(self):
        toks = cjk_tokens("读 Hamlet 记")
        assert "Hamlet" in toks and "读" in toks and "记" in toks

    def test_punctuation_not_a_token(self):
        toks = cjk_tokens("好，坏。")
        assert "，" not in toks and "。" not in toks

    def test_empty(self):
        assert cjk_tokens("") == []
        assert cjk_tokens_text("") == ""

    def test_tokens_text_space_joined(self):
        s = cjk_tokens_text("竇娥")
        assert " " in s and "竇娥" in s.split()

    def test_numbers_kept(self):
        assert "1802" in cjk_tokens("写于 1802 年")


class TestCjkTsquery:
    def test_multibyte_makes_bigrams(self):
        q = cjk_tsquery("竇娥冤")
        assert "竇娥" in q and "娥冤" in q and " | " in q

    def test_single_char_uses_unigram(self):
        assert cjk_tsquery("冤") == "冤"

    def test_english_kept(self):
        assert "Hamlet" in cjk_tsquery("Hamlet")

    def test_dedup(self):
        # 同一 bigram 重复出现只留一个
        q = cjk_tsquery("好好好好")
        assert q.count("好好") == 1

    def test_injection_chars_filtered(self):
        """tsquery 元字符不得穿过 token（安全面）。`|` 是我们自己的 OR 分隔符。"""
        q = cjk_tsquery("a&b|c!d:e*f(g)h")
        tokens = [t.strip() for t in q.split("|")]
        for tok in tokens:
            assert not any(m in tok for m in "&|!():*")
        assert tokens == ["a", "b", "c", "d", "e", "f", "g", "h"]

    def test_injection_produces_valid_tsquery_syntax(self):
        """过滤后产物=字词 token 由 ` | ` 连接，交给 PG 不会语法错。"""
        import re

        q = cjk_tsquery("竇娥 & 冤 | :*")
        assert re.fullmatch(r"[^&!():*]+(?: \| [^&!():*]+)*", q)

    def test_empty(self):
        assert cjk_tsquery("") == ""
        assert cjk_tsquery("   ") == ""
