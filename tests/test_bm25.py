from backend.app.knowledge.bm25 import bm25_scores, tokenize


def test_chinese_tokenizer_normalizes_numbers_and_synonyms():
    assert tokenize("物流七天没有更新怎么办") == tokenize("物流7天无更新")


def test_bm25_ranks_matching_policy_first():
    documents = [
        "物流异常处理办法 物流丢件 物流信息超过7天无更新",
        "发票开具规则 订单完成后可以申请电子发票",
        "七天无理由退货规则 商品完好可以申请退货",
    ]
    scores = bm25_scores("物流七天没有更新怎么办", documents)
    assert scores[0] == max(scores)
    assert scores[0] > scores[1]
