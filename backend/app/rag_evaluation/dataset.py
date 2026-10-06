from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from backend.app.rag_evaluation.models import RagEvaluationCase, RelevantRule


DATASET_VERSION = "policy-rag-v1.0.0"
EXPECTED_CASE_COUNT = 142
EXPECTED_CATEGORY_COUNTS = {
    "standard": 24,
    "paraphrase": 24,
    "colloquial": 24,
    "product_boundary": 10,
    "multi_policy": 10,
    "exception": 10,
    "no_answer": 30,
    "confusing": 10,
}


def _rule(filename: str, section: str, relevance: int = 3) -> RelevantRule:
    return RelevantRule(
        source_filename=filename,
        section=section,
        relevance=relevance,
    )


@dataclass(frozen=True, slots=True)
class _Scenario:
    slug: str
    queries: tuple[str, str, str]
    rules: tuple[RelevantRule, ...]
    policy_category: str = "general"
    product_category: str | None = None
    tags: tuple[str, ...] = ()


_SCENARIOS = (
    _Scenario(
        "return-overview",
        ("七天无理由退货的基本规则是什么？", "普通商品签收后不想要了，退货条件是什么？", "东西买回来后悔了，七天内能退不？"),
        (_rule("七天无理由退货规则.md", "1. 政策概述"),),
        "refund", tags=("退款", "退货"),
    ),
    _Scenario(
        "return-conditions",
        ("七天无理由退货需要满足哪些商品完好条件？", "商品拆了外包装还能按无理由退货吗？", "盒子拆了但东西没用，能退吗？"),
        (_rule("七天无理由退货规则.md", "2. 适用条件"),),
        "refund", tags=("退款", "退货"),
    ),
    _Scenario(
        "return-process",
        ("七天无理由退货的办理流程是什么？", "退货申请审核后需要多久寄出？", "申请通过了，我几天内把货寄回去？"),
        (_rule("七天无理由退货规则.md", "4. 办理流程"),),
        "refund", tags=("退款", "流程"),
    ),
    _Scenario(
        "return-freight",
        ("七天无理由退货的返程运费由谁承担？", "无质量问题退货时商家承担运费吗？", "我自己不想要了，寄回去的快递费算谁的？"),
        (_rule("七天无理由退货规则.md", "5. 运费承担规则"),),
        "refund", tags=("退款", "运费"),
    ),
    _Scenario(
        "fresh-refund",
        ("生鲜食品签收后破损的退款规则是什么？", "水果到货变质，需要在多长时间内举证？", "车厘子坏了，多久内找客服还能退？"),
        (_rule("特殊商品退款例外规则.md", "1. 生鲜食品类"),),
        "refund", "生鲜食品", ("退款", "生鲜"),
    ),
    _Scenario(
        "custom-refund",
        ("定制商品在哪些情况下可以退款？", "个性化商品制作错误是否支持全额退款？", "刻字刻错了，定制款能退钱吗？"),
        (_rule("特殊商品退款例外规则.md", "2. 定制商品类"),),
        "refund", "定制商品", ("退款", "定制"),
    ),
    _Scenario(
        "virtual-refund",
        ("虚拟商品充值成功后是否支持退款？", "游戏充值未到账可以退款或者更正吗？", "话费充错账号了，这钱还能退吗？"),
        (_rule("特殊商品退款例外规则.md", "3. 虚拟商品类"),),
        "refund", "虚拟商品", ("退款", "虚拟商品"),
    ),
    _Scenario(
        "personal-items",
        ("贴身用品拆封后的退货规则是什么？", "内衣存在质量问题时多久内可以申请退换？", "袜子拆包后发现有问题，还能换吗？"),
        (_rule("特殊商品退款例外规则.md", "4. 贴身用品类"),),
        "refund", "贴身用品", ("退款", "贴身用品"),
    ),
    _Scenario(
        "warranty-scope",
        ("商品保修服务覆盖哪些质量故障？", "正常使用产生的功能异常属于保修范围吗？", "家电正常用着坏了，管保修不？"),
        (_rule("商品保修服务规范.md", "1. 保修范围"),),
        "after_sale", "家居家电", ("维修", "保修"),
    ),
    _Scenario(
        "warranty-period",
        ("数码电子产品的保修期限是多久？", "家电整机和主要部件分别保修多长时间？", "耳机从哪天开始算保修，保几年？"),
        (_rule("商品保修服务规范.md", "2. 保修期限"),),
        "after_sale", "数码电器", ("维修", "保修"),
    ),
    _Scenario(
        "warranty-process",
        ("商品申请保修维修的流程是什么？", "寄修前需要向客服提供什么信息？", "电器坏了要怎么送去修？"),
        (_rule("商品保修服务规范.md", "3. 保修流程"),),
        "after_sale", "数码电器", ("维修", "流程"),
    ),
    _Scenario(
        "warranty-exclusion",
        ("哪些情况不予保修？", "商品进水或私自拆卸后还能免费保修吗？", "耳机让我摔坏了，还给免费修吗？"),
        (_rule("商品保修服务规范.md", "5. 不予保修情形"),),
        "after_sale", "数码电器", ("维修", "例外"),
    ),
    _Scenario(
        "reship-scope",
        ("哪些商品破损情况可以申请补发？", "原厂包装内缺少配件是否属于补发范围？", "开箱少了个配件，能再发一个吗？"),
        (_rule("商品破损补发处理规范.md", "1. 适用范围"),),
        "after_sale", tags=("补发",),
    ),
    _Scenario(
        "reship-evidence",
        ("商品破损申请补发需要提供哪些材料？", "配件缺失申请补发是否需要开箱视频？", "少件了，我得拍什么证明？"),
        (_rule("商品破损补发处理规范.md", "2. 举证要求"),),
        "after_sale", tags=("补发", "材料"),
    ),
    _Scenario(
        "reship-handling",
        ("商品主体严重破损时有哪些处理方式？", "主体破损无法使用可以选择补发还是退款？", "东西摔烂没法用了，重发还是退钱？"),
        (_rule("商品破损补发处理规范.md", "3. 处理方式"),),
        "after_sale", tags=("补发", "退款"),
    ),
    _Scenario(
        "logistics-delay",
        ("物流延迟如何认定和处理？", "超过承诺时效三天可以申请退款吗？", "快递晚了三天还没到，能直接退钱吗？"),
        (_rule("物流异常处理办法.md", "1. 物流延迟"),),
        "shipping", tags=("物流", "延迟"),
    ),
    _Scenario(
        "logistics-loss",
        ("物流丢件的认定标准和处理方式是什么？", "快递信息七天没有更新是否按丢件处理？", "物流一周没动静，是不是能按丢件赔？"),
        (_rule("物流异常处理办法.md", "2. 物流丢件"),),
        "shipping", tags=("物流", "丢件"),
    ),
    _Scenario(
        "transport-damage",
        ("运输破损的处理政策是什么？", "已签收商品运输破损需要多久内举证？", "快递把东西压坏了，签收后还能退或补发吗？"),
        (_rule("物流异常处理办法.md", "3. 运输破损"),),
        "shipping", tags=("物流", "破损"),
    ),
    _Scenario(
        "invoice-application",
        ("电子发票如何申请？", "订单完成后可以在多少天内补开发票？", "买完东西忘了开票，过后还能补吗？"),
        (_rule("发票开具规则.md", "3. 申请方式"),),
        "general", tags=("发票",),
    ),
    _Scenario(
        "invoice-timing",
        ("普通电子发票和专用发票的开具时效分别多久？", "增值税专用发票申请后几天寄出？", "发票申请了要等多久？"),
        (_rule("发票开具规则.md", "4. 开票时效"),),
        "general", tags=("发票", "时效"),
    ),
    _Scenario(
        "shipment-timing",
        ("普通商品的发货时效标准是什么？", "当天十六点后付款什么时候发货？", "我晚上下单，商家啥时候发？"),
        (_rule("发货时效与配送范围规范.md", "1. 发货时效标准"),),
        "shipping", tags=("发货",),
    ),
    _Scenario(
        "address-change",
        ("订单在什么条件下可以修改收货地址？", "商品发货后还能跨城市修改地址吗？", "快递已经发出了，地址还能改不？"),
        (_rule("发货时效与配送范围规范.md", "5. 截单与改址"),),
        "shipping", tags=("地址修改",),
    ),
    _Scenario(
        "price-protection",
        ("普通商品和数码家电的价保周期分别多久？", "数码电器签收后十五天内降价可以退差价吗？", "刚买的手机降价了，几天内能补差价？"),
        (_rule("价保服务规则.md", "2. 价保周期"),),
        "general", "数码电器", ("价保",),
    ),
    _Scenario(
        "quality-remedy",
        ("商品出现质量问题可以选择哪些处理方式？", "功能故障可以申请全额退款、部分退款还是换货？", "买来的东西坏了，能换一个还是只能退钱？"),
        (_rule("质量问题退款处理规范.md", "4. 处理方式"),),
        "after_sale", tags=("退款", "换货", "质量问题"),
    ),
)


def _base_cases() -> list[RagEvaluationCase]:
    cases: list[RagEvaluationCase] = []
    categories = ("standard", "paraphrase", "colloquial")
    for scenario in _SCENARIOS:
        for category, query in zip(categories, scenario.queries, strict=True):
            cases.append(
                RagEvaluationCase(
                    id=f"rag-{category}-{scenario.slug}",
                    category=category,
                    query=query,
                    policy_category=scenario.policy_category,
                    product_category=scenario.product_category,
                    relevant_rules=list(scenario.rules),
                    tags=list(scenario.tags),
                )
            )
    return cases


def _case(
    case_id: str,
    category: str,
    query: str,
    *rules: RelevantRule,
    policy_category: str = "general",
    product_category: str | None = None,
    tags: tuple[str, ...] = (),
    expect_no_answer: bool = False,
) -> RagEvaluationCase:
    return RagEvaluationCase(
        id=case_id,
        category=category,
        query=query,
        policy_category=policy_category,
        product_category=product_category,
        relevant_rules=list(rules),
        expect_no_answer=expect_no_answer,
        tags=list(tags),
    )


def _product_boundary_cases() -> list[RagEvaluationCase]:
    c = "product_boundary"
    return [
        _case("rag-boundary-fresh-no-reason", c, "生鲜水果可以七天无理由退货吗？", _rule("特殊商品退款例外规则.md", "1. 生鲜食品类"), _rule("七天无理由退货规则.md", "3. 不适用情形", 2), policy_category="refund", product_category="生鲜食品", tags=("退款", "生鲜")),
        _case("rag-boundary-custom-no-reason", c, "刻有名字的定制商品支持无理由退货吗？", _rule("特殊商品退款例外规则.md", "2. 定制商品类"), _rule("七天无理由退货规则.md", "3. 不适用情形", 2), policy_category="refund", product_category="定制商品", tags=("退款", "定制")),
        _case("rag-boundary-virtual-used", c, "已经充值成功的游戏点卡还能退款吗？", _rule("特殊商品退款例外规则.md", "3. 虚拟商品类"), policy_category="refund", product_category="虚拟商品", tags=("退款", "虚拟商品")),
        _case("rag-boundary-personal-opened", c, "拆封试穿过的内衣可以无理由退吗？", _rule("特殊商品退款例外规则.md", "4. 贴身用品类"), policy_category="refund", product_category="贴身用品", tags=("退款", "贴身用品")),
        _case("rag-boundary-digital-activated", c, "已经激活的数码产品还能七天无理由退货吗？", _rule("七天无理由退货规则.md", "2. 适用条件"), policy_category="refund", product_category="数码电器", tags=("退款", "数码电器")),
        _case("rag-boundary-appliance-warranty", c, "冰箱整机和主要部件保修期一样吗？", _rule("商品保修服务规范.md", "2. 保修期限"), policy_category="after_sale", product_category="家居家电", tags=("维修", "家居家电")),
        _case("rag-boundary-fresh-cold-chain", c, "生鲜订单默认采用什么配送方式？", _rule("发货时效与配送范围规范.md", "2. 配送方式与承运商"), policy_category="shipping", product_category="生鲜食品", tags=("物流", "生鲜")),
        _case("rag-boundary-digital-price", c, "数码家电和普通商品的价保天数有什么区别？", _rule("价保服务规则.md", "2. 价保周期"), product_category="数码电器", tags=("价保", "数码电器")),
        _case("rag-boundary-remote-delivery", c, "新疆、西藏等偏远地区是否可以配送？", _rule("发货时效与配送范围规范.md", "3. 配送范围"), policy_category="shipping", tags=("物流", "配送范围")),
        _case("rag-boundary-personal-quality", c, "贴身用品有质量问题时是不是也完全不能退换？", _rule("特殊商品退款例外规则.md", "4. 贴身用品类"), policy_category="refund", product_category="贴身用品", tags=("退款", "质量问题")),
    ]


def _multi_policy_cases() -> list[RagEvaluationCase]:
    c = "multi_policy"
    return [
        _case("rag-multi-fresh-damage", c, "生鲜签收时破损，退款和补发分别适用什么规定？", _rule("特殊商品退款例外规则.md", "1. 生鲜食品类"), _rule("商品破损补发处理规范.md", "3. 处理方式"), policy_category="after_sale", product_category="生鲜食品", tags=("退款", "补发")),
        _case("rag-multi-signed-damage", c, "商品签收后发现运输破损，需要什么材料并能怎么处理？", _rule("物流异常处理办法.md", "3. 运输破损"), _rule("商品破损补发处理规范.md", "2. 举证要求"), _rule("商品破损补发处理规范.md", "3. 处理方式", 2), policy_category="after_sale", tags=("物流", "补发", "材料")),
        _case("rag-multi-quality-freight", c, "质量问题退货可以怎么处理，退货运费由谁承担？", _rule("质量问题退款处理规范.md", "4. 处理方式"), _rule("质量问题退款处理规范.md", "5. 费用与时效"), policy_category="refund", tags=("退款", "运费")),
        _case("rag-multi-return-process-freight", c, "无理由退货要怎么操作，寄回商品的运费谁出？", _rule("七天无理由退货规则.md", "4. 办理流程"), _rule("七天无理由退货规则.md", "5. 运费承担规则"), policy_category="refund", tags=("退货", "运费")),
        _case("rag-multi-warranty-invoice", c, "没有发票能保修吗，电子发票是否可以作为保修凭证？", _rule("商品保修服务规范.md", "6. 常见问题"), _rule("发票开具规则.md", "7. 常见问题"), policy_category="after_sale", product_category="数码电器", tags=("维修", "发票")),
        _case("rag-multi-loss-timing", c, "快递确认丢件后有哪些解决方式，客服多久处理？", _rule("物流异常处理办法.md", "2. 物流丢件"), _rule("物流异常处理办法.md", "5. 处理时效"), policy_category="shipping", tags=("物流", "丢件")),
        _case("rag-multi-shipment-address", c, "今天十六点后付款什么时候发货，发货前还能改地址吗？", _rule("发货时效与配送范围规范.md", "1. 发货时效标准"), _rule("发货时效与配送范围规范.md", "5. 截单与改址"), policy_category="shipping", tags=("发货", "地址修改")),
        _case("rag-multi-invoice-refund", c, "订单退款后已经开具的发票如何处理，发票信息错误还能换开吗？", _rule("发票开具规则.md", "5. 换开与红冲"), policy_category="refund", tags=("发票", "退款")),
        _case("rag-multi-repair-exchange", c, "同一个故障维修两次还没修好，可以换新吗？", _rule("商品保修服务规范.md", "6. 常见问题"), _rule("质量问题退款处理规范.md", "4. 处理方式", 2), policy_category="after_sale", product_category="数码电器", tags=("维修", "换货")),
        _case("rag-multi-wrong-missing", c, "收到的商品发错且少了配件，应该拒收还是补发？", _rule("商品签收与拒收规则.md", "2. 正常拒收条件"), _rule("物流异常处理办法.md", "4. 错发/漏发"), policy_category="after_sale", tags=("拒收", "补发")),
    ]


def _exception_cases() -> list[RagEvaluationCase]:
    c = "exception"
    return [
        _case("rag-exception-custom-error", c, "定制商品不能无理由退，但商家把定制文字做错了怎么办？", _rule("特殊商品退款例外规则.md", "2. 定制商品类"), policy_category="refund", product_category="定制商品", tags=("退款", "例外")),
        _case("rag-exception-virtual-failed", c, "虚拟商品通常不能退，但充值一直没有成功怎么办？", _rule("特殊商品退款例外规则.md", "3. 虚拟商品类"), policy_category="refund", product_category="虚拟商品", tags=("退款", "例外")),
        _case("rag-exception-voucher-used", c, "已经核销使用的电子卡券还能申请退款吗？", _rule("特殊商品退款例外规则.md", "3. 虚拟商品类"), policy_category="refund", product_category="虚拟商品", tags=("退款", "例外")),
        _case("rag-exception-personal-quality", c, "内衣拆封后发现开线，质量问题还能申请退换吗？", _rule("特殊商品退款例外规则.md", "4. 贴身用品类"), policy_category="refund", product_category="贴身用品", tags=("退款", "例外")),
        _case("rag-exception-fresh-delay", c, "生鲜因为我自己延迟收货而变质，可以退款吗？", _rule("特殊商品退款例外规则.md", "1. 生鲜食品类"), policy_category="refund", product_category="生鲜食品", tags=("退款", "例外")),
        _case("rag-exception-quality-after-48", c, "耳机用了几天才发现自身质量故障，超过48小时就完全不能处理了吗？", _rule("质量问题退款处理规范.md", "7. 常见问题"), _rule("商品保修服务规范.md", "1. 保修范围", 2), policy_category="after_sale", product_category="数码电器", tags=("维修", "例外")),
        _case("rag-exception-human-damage", c, "商品是我摔坏的，可以按照质量问题免费维修或退款吗？", _rule("商品保修服务规范.md", "5. 不予保修情形"), _rule("质量问题退款处理规范.md", "6. 不支持情形", 2), policy_category="after_sale", product_category="数码电器", tags=("维修", "例外")),
        _case("rag-exception-price-coupon", c, "订单使用了新人专属优惠券，商品降价后还能价保吗？", _rule("价保服务规则.md", "3. 差价计算"), _rule("价保服务规则.md", "5. 不适用情形", 2), tags=("价保", "例外")),
        _case("rag-exception-activated-return", c, "数码设备已经激活，但是还在七天内，支持无理由退货吗？", _rule("七天无理由退货规则.md", "2. 适用条件"), policy_category="refund", product_category="数码电器", tags=("退货", "例外")),
        _case("rag-exception-light-package", c, "快递外包装只是轻微挤压但商品完好，可以当场拒收吗？", _rule("商品签收与拒收规则.md", "3. 不予拒收情形"), policy_category="shipping", tags=("拒收", "例外")),
    ]


def _no_answer_cases() -> list[RagEvaluationCase]:
    prompts = (
        "怎么注销我的平台账号？",
        "登录手机号在哪里修改？",
        "我忘记账户密码了怎么办？",
        "会员等级升级需要多少成长值？",
        "积分可以兑换哪些礼品？",
        "根据我的喜好推荐几款手机。",
        "直播间优惠券几点发放？",
        "你们线下门店的营业地址在哪里？",
        "平台最近有没有招聘客服？",
        "我想投诉直播间的主播。",
        "实名认证一直失败应该怎么处理？",
        "如何解绑银行卡？",
        "支付密码在哪里修改？",
        "会员生日有什么专属权益？",
        "昨天的直播在哪里看回放？",
        "新人优惠券应该从哪里领取？",
        "可以根据预算给我推荐一款电脑吗？",
        "怎么验证平台上的商品是不是正品？",
        "人工客服每天几点下班？",
        "已经发布的商品评价怎么删除？",
        "我不想看到这家店，怎么屏蔽商家？",
        "个人如何申请成为平台商家？",
        "你们的手机应用从哪里下载？",
        "品牌想在平台投放广告应该联系谁？",
        "平台会保存我的哪些个人数据？",
        "账户被冻结了如何解除？",
        "一直收不到短信验证码怎么办？",
        "平台社区如何发布帖子？",
        "礼品卡余额在哪里查询？",
        "下单后可以改成门店自提吗？",
    )
    return [
        _case(
            f"rag-no-answer-{index:03d}",
            "no_answer",
            prompt,
            expect_no_answer=True,
            tags=("无答案",),
        )
        for index, prompt in enumerate(prompts, start=1)
    ]


def _confusing_cases() -> list[RagEvaluationCase]:
    c = "confusing"
    return [
        _case("rag-confusing-price-refund", c, "刚买完就降价了，我不是退货，只想退差价。", _rule("价保服务规则.md", "1. 价保定义"), tags=("价保", "易混淆")),
        _case("rag-confusing-resend", c, "商品主体被压坏了，我不想退钱，重新发一个行吗？", _rule("商品破损补发处理规范.md", "3. 处理方式"), policy_category="after_sale", tags=("补发", "易混淆")),
        _case("rag-confusing-digital-charge", c, "我的耳机充不上电，不是话费充值问题，可以保修吗？", _rule("商品保修服务规范.md", "1. 保修范围"), policy_category="after_sale", product_category="数码电器", tags=("维修", "易混淆")),
        _case("rag-confusing-virtual-account", c, "游戏充值充错账号了，不是实体商品发错货，能更正吗？", _rule("特殊商品退款例外规则.md", "3. 虚拟商品类"), policy_category="refund", product_category="虚拟商品", tags=("退款", "易混淆")),
        _case("rag-confusing-delay-three", c, "物流超过承诺时间三天没到，但还没有确认丢件。", _rule("物流异常处理办法.md", "1. 物流延迟"), policy_category="shipping", tags=("物流", "易混淆")),
        _case("rag-confusing-loss-seven", c, "物流信息已经七天没有更新，应按延迟还是丢件处理？", _rule("物流异常处理办法.md", "2. 物流丢件"), policy_category="shipping", tags=("物流", "易混淆")),
        _case("rag-confusing-package-only", c, "只是外包装有点皱，里面商品没坏，能拒收吗？", _rule("商品签收与拒收规则.md", "3. 不予拒收情形"), policy_category="shipping", tags=("拒收", "易混淆")),
        _case("rag-confusing-invoice-amount", c, "发票信息和金额开错了，我是换开发票，不是申请退款。", _rule("发票开具规则.md", "5. 换开与红冲"), tags=("发票", "易混淆")),
        _case("rag-confusing-color", c, "商品没有损坏，只是我觉得有色差不喜欢，这算质量问题吗？", _rule("质量问题退款处理规范.md", "6. 不支持情形"), policy_category="refund", tags=("退款", "易混淆")),
        _case("rag-confusing-used-days", c, "耳机用了几天出现故障，不走无理由退货，应该走什么售后？", _rule("质量问题退款处理规范.md", "7. 常见问题"), _rule("商品保修服务规范.md", "1. 保修范围", 2), policy_category="after_sale", product_category="数码电器", tags=("维修", "易混淆")),
    ]


def load_rag_evaluation_cases() -> list[RagEvaluationCase]:
    cases = (
        _base_cases()
        + _product_boundary_cases()
        + _multi_policy_cases()
        + _exception_cases()
        + _no_answer_cases()
        + _confusing_cases()
    )
    ids = [case.id for case in cases]
    if len(cases) != EXPECTED_CASE_COUNT:
        raise RuntimeError(f"RAG dataset expected {EXPECTED_CASE_COUNT} cases, got {len(cases)}")
    if len(ids) != len(set(ids)):
        raise RuntimeError("RAG dataset contains duplicate case ids")
    actual_counts = Counter(case.category for case in cases)
    if actual_counts != Counter(EXPECTED_CATEGORY_COUNTS):
        raise RuntimeError(
            f"RAG category distribution mismatch: expected {EXPECTED_CATEGORY_COUNTS}, "
            f"got {dict(actual_counts)}"
        )
    return cases
