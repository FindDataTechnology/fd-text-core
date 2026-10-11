"""CJK 分词纯函数单测（text-cjk-tokenization 1.1/1.5）。

t2s 归一（text-t2s-normalization）落地后，默认路径把繁体折简——本文件既
断言新行为，也用 `t2s=False` 断言关闭开关时字面保持（两条路径都要有据可查）。
"""
from fd_text_core.tokenize import cjk_tokens, cjk_tokens_text, cjk_tsquery, fold_t2s


class TestCjkTokens:
    def test_bigram_and_unigram_produced(self):
        # 默认 t2s=True：繁入简出（归一在分词内层，见 tokenize 模块 docstring）
        toks = cjk_tokens("竇娥冤")
        assert "窦" in toks and "娥" in toks and "冤" in toks  # unigram（已折简）
        assert "窦娥" in toks and "娥冤" in toks  # bigram

    def test_t2s_false_keeps_literal(self):
        # 关闭归一：字面保持（版本学/训诂比对垂域用）
        toks = cjk_tokens("竇娥冤", t2s=False)
        assert "竇" in toks and "竇娥" in toks
        assert "窦" not in toks

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
        assert " " in s and "窦娥" in s.split()  # 默认折简

    def test_numbers_kept(self):
        assert "1802" in cjk_tokens("写于 1802 年")


class TestCjkTsquery:
    def test_multibyte_makes_bigrams(self):
        q = cjk_tsquery("竇娥冤")
        assert "窦娥" in q and "娥冤" in q and " | " in q  # 查询侧同折简

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


class TestFoldT2s:
    """繁简归一（text-t2s-normalization 2.1）：折简正确性、幂等性、英文零回归。"""

    def test_traditional_folded(self):
        assert fold_t2s("竇娥冤") == "窦娥冤"
        assert fold_t2s("西廂記") == "西厢记"
        assert fold_t2s("牡丹亭 長生殿") == "牡丹亭 长生殿"

    def test_simplified_passthrough(self):
        # 已是简体：查表不命中，原样通过
        assert fold_t2s("窦娥冤") == "窦娥冤"

    def test_idempotent(self):
        once = fold_t2s("竇娥張驢兒鶯鶯麗娘夢梅")
        assert fold_t2s(once) == once

    def test_ascii_passthrough(self):
        s = "Hamlet is a tragedy, 1802."
        assert fold_t2s(s) == s

    def test_mixed_script(self):
        assert fold_t2s("读 竇娥冤 记") == "读 窦娥冤 记"

    def test_empty(self):
        assert fold_t2s("") == ""

    def test_extended_punctuation_untouched(self):
        # 归一不触碰标点（token 过滤另有其责）
        assert fold_t2s("「翫」") == "「玩」"  # 翫→玩 在表中；引号原样


class TestT2sIsomorphism:
    """归一后写入/查询 token 相交——这是本 change 要修的那个「静默 0 命中」。"""

    def test_simplified_query_traditional_corpus_tokens_intersect(self):
        write_side = set(cjk_tokens("竇娥冤"))  # 繁体语料（写入侧）
        query_side = set(cjk_tsquery("窦娥").split(" | "))  # 简体查询
        assert write_side & query_side, "繁简互查必须相交（对照：修复前 0 命中）"

    def test_traditional_query_simplified_corpus_tokens_intersect(self):
        write_side = set(cjk_tokens("窦娥冤"))  # 简体语料
        query_side = set(cjk_tsquery("竇娥").split(" | "))  # 繁体查询
        assert write_side & query_side

    def test_t2s_off_is_literal_both_sides(self):
        # 关闭归一：两侧都字面，繁查询对简语料不相交（行为如实，非缺陷）
        write_side = set(cjk_tokens("窦娥冤", t2s=False))
        query_side = set(cjk_tsquery("竇娥", t2s=False).split(" | "))
        assert not (write_side & query_side)

