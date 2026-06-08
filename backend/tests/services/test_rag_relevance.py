from app.services.rag import is_visual_summary_relevant


def test_visual_summary_relevance_uses_same_fault_object():
    assert is_visual_summary_relevant("这个空调不太给力，吹半天都不凉", "空调出风口异常")


def test_visual_summary_relevance_allows_generic_fault_with_image_detail():
    assert is_visual_summary_relevant("这里漏水", "天花板水渍渗漏")


def test_visual_summary_relevance_rejects_different_fault_object():
    assert not is_visual_summary_relevant("空调不制冷", "天花板水渍渗漏")


def test_visual_summary_relevance_rejects_different_object_even_same_fault():
    assert not is_visual_summary_relevant("水龙头漏水", "天花板水渍渗漏")
