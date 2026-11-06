"""寫入前的三平台字寬：純函式部分，不依賴字型檔。"""
from csdview.platform_fit import _block_message, _clear_note, _detail, _related


def test_related_includes_descendants():
    assert _related("Layer/title", ["Layer/title"])
    assert _related("Layer/panel/title", ["Layer/panel"])
    assert not _related("Layer/other", ["Layer/panel"])


def test_block_message_names_the_width_and_the_escape():
    text = _block_message([{
        "path": "Layer/title", "kind": "box", "suggestWidth": 96,
        "box": [73, 30], "windows": {"lines": 1},
        "platforms": {"android": {"lines": 2, "height": 48, "width": 70}},
    }])
    assert "沒有寫入" in text
    assert "Layer/title" in text
    assert "Android" in text
    assert "96" in text
    assert "allowPlatformOverflow" in text
    assert "不是預設" in text


def test_ios_rule_matches_pingfang_tc_regular():
    from csdview.platform_fit import PlatformFonts, _IOS_ASCII_PCT, _IosRule
    assert len(_IOS_ASCII_PCT) == 95
    rule = _IosRule(100)
    assert rule.getlength("開通三大優惠") == 600
    assert rule.getlength("1") == 40
    assert rule.getlength("NT$100") == 354
    fonts = PlatformFonts.get()
    ios = fonts.coverage()["ios"]
    assert ios["measured"] is True
    assert "不讀字型檔" in ios["note"]
    android = fonts.coverage()["android"]
    assert "沿用" in android["note"] or "沒得沿用" in android["note"]
    assert android["measured"] is fonts.coverage()["windows"]["measured"]
    from csdview.platform_fit import text_widths
    widths = text_widths("NT$100 (已過期)", 20)
    assert widths["android"] == widths["windows"]
    note = _clear_note({
        "windows": {"label": "Windows", "measured": True, "note": "中文 msjh.ttc"},
        "android": {"label": "Android", "measured": True, "note": "中文 Noto"},
        "ios": {"label": "iOS", "measured": False, "note": "沒有字型可量"},
    })
    assert "iOS 沒量到" in note
    assert "不擋寫檔" in note


def test_parent_suggests_widening_the_parent():
    detail = _detail([{
        "path": "Layer/tip", "kind": "parent",
        "frame": [0, 100], "suggestWidth": 128,
        "platforms": {"android": {"over": 28, "width": 128}},
    }])
    assert "父層加寬到 128" in detail
