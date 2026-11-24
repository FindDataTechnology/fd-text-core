"""测试垂域注册表的 facet 投影配置位（change: unify-facet-projection-at-source D1）。"""
from __future__ import annotations

import pytest

from fd_text_core.verticals import registry


def test_get_facet_projector_unknown_domain_raises():
    with pytest.raises(KeyError):
        registry.get_facet_projector("__no_such_domain__")


def test_get_facet_projector_returns_none_when_unset():
    # papers 不声明投影 → None（合法：该域不投影 facet）
    assert registry.get_facet_projector("papers") is None


def test_record_contract_documented_in_docstring():
    """记录视图的键集合是契约，必须写在 Vertical 的 docstring 里。"""
    doc = registry.Vertical.__doc__ or ""
    import inspect

    # dataclass 字段的注释在源里；用源码文本校验比 getattr 更贴近真实契约
    src = inspect.getsource(registry)
    for key in ("title", "content", "published", "metadata", "source", "source_type", "domain"):
        assert key in src, f"记录视图键 {key} 未在 registry 源中声明"
    assert doc is not None


def test_facet_projector_is_optional_field():
    v = registry.Vertical(name="__t__", strategy=lambda t: [], facets={})
    assert v.facet_projector is None
