"""執行緒安全的記憶化。

Renderer 與 AssetLibrary 都會被多執行緒同時使用：
- 批次指令（frames／gif／sheet）用執行緒池平行渲染
- 網頁伺服器每個請求一個執行緒，且共用同一個 Renderer

這些快取全部走同一個函式，就不會出現「某個快取忘了保護」的情況。

實際發生過的例子（8 執行緒下 12 次有 7 次畫出別張圖），要兩個問題同時存在才會觸發：

1. `AssetLibrary.image()` 沒有同步，兩個執行緒各自載入同一張圖，產生了重複的
   PIL 物件。其中一份被覆蓋後就會被回收。
2. `_scale9` 的快取鍵用 `id(image)`。上一步釋放掉的 id 會被新物件重複使用，
   於是九宮格快取回傳了完全不相干的另一張圖。

任一個修好都能讓症狀消失，但兩個都要修：只修其中一個，另一個仍是等著被觸發的地雷。
`tests/test_concurrency.py` 會把兩個 bug 都注回去，確認測試真的抓得到。

規則：
1. **鍵必須穩定**——用資源路徑之類的內容識別，絕對不要用 `id()`。
2. **值必須建立後就不再修改**——快取回傳的是共用物件，任何一方改動都會影響其他人。
   PIL 的 crop／resize／transform 都會產生新物件，是安全的；paste／alpha_composite
   會就地修改，只能用在自己剛建立的圖上。
"""
from __future__ import annotations

import threading
from typing import Any, Callable

_MISSING = object()


def memoize(cache: dict, key: Any, lock: threading.Lock,
            build: Callable[[], Any], limit: int = 0) -> Any:
    """取快取；沒有就建立。

    命中時不進鎖（絕大多數情況），未命中才鎖起來並重新確認一次——
    否則兩個執行緒會同時建立，其中一份被丟棄，浪費之外也可能讓呼叫端
    拿到兩個不同的物件。

    `build()` 回傳 None 也會被記住（用來記「這個資源找不到」），
    所以用哨兵值判斷是否命中，不能只看 `is not None`。

    `limit > 0` 時，寫入後若超過上限就整份清掉再只留這一筆。呼叫端不必
    自己在 build() 裡喊 bounded——那個時機快取還沒寫入，上限會差一筆。
    """
    hit = cache.get(key, _MISSING)
    if hit is not _MISSING:
        return hit
    with lock:
        hit = cache.get(key, _MISSING)
        if hit is not _MISSING:
            return hit
        value = build()
        cache[key] = value
        if limit and len(cache) > limit:
            # 整份清掉再只留這一筆：上限是防止無界成長，不是 LRU。
            cache.clear()
            cache[key] = value
        return value


def bounded(cache: dict, limit: int) -> None:
    """快取太大時整個清掉。

    呼叫端必須已經持有該快取的鎖，否則清除與寫入會交錯。
    """
    if len(cache) > limit:
        cache.clear()
