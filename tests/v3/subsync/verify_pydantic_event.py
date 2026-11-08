"""SubSync · MP v3 事件负载形态适配测试（v1.9.2 回归）。

**背景 —— 这个 bug 是怎么变成「MP 反应慢」的：**

MP v3 把 `TransferComplete` 事件里的 `transferinfo` 从 dict 换成了 Pydantic
模型（`app.schemas.transfer.TransferInfo`）。插件在事件处理器里写的是
`transfer.get("target_diritem")`，遇到模型直接抛
`AttributeError: 'TransferInfo' object has no attribute 'get'`。

单看只是一条报错，实际后果是链式的：

```
事件处理器抛异常
  → MP 把它丢进 outbox，30 秒后重试
  → 5 次都失败 → 进死信表 outboxmessage
  → 每一次整理完成都往死信表塞一条
  → 死信表涨到几十万行、几百 MB
  → outboxmessage 没有 created_at 索引，取「最近 N 条」= 全表扫描
  → MP 写锁竞争 → 用户感知：MP 反应慢
```

所以这里测的不只是「能不能取到值」，还要保证**事件处理器永不抛异常**，
不让插件的任何问题外溢到 MP 的事件总线。

测试用**行为等价的替身**（有属性、没有 `.get`）来复刻 Pydantic 模型 ——
不依赖真的装 pydantic，语义与真模型一致：属性访问可用、`.get()` 会抛
AttributeError。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Tree, load_plugin, make_plugin, strm, subtitle  # noqa: E402

mod = load_plugin()
failures = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}" + ("" if cond else f"   {detail}"))
    if not cond:
        failures.append(name)


def section(title):
    print(f"\n=== {title} ===")


# --------------------------------------------------------------- 替身模型
class FakePydanticBase:
    """Pydantic BaseModel 的行为替身。

    只复刻与本 bug 相关的两点：
      1. 字段用属性访问（`obj.field`）；
      2. **没有** `.get()` 方法 —— 真 Pydantic 模型也没有，调用即
         `AttributeError: 'X' object has no attribute 'get'`。
    """

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class FakeFileItem(FakePydanticBase):
    """对应 MP v3 的 `app.schemas.file.FileItem`（继承 FileURI，含 path）。"""


class FakeTransferInfo(FakePydanticBase):
    """对应 MP v3 的 `app.schemas.transfer.TransferInfo`。"""


# --------------------------------------------------- 1. 原始 bug 的复现
section("1. 复现原始 bug：Pydantic 形态的 transferinfo")

# 先确认替身真的能复现原始症状 —— 否则后面的测试没有意义
try:
    FakeTransferInfo(target_diritem=FakeFileItem(path="/tmp/x")).get("target_diritem")
    check("替身复现原始症状（.get 抛 AttributeError）", False,
          "替身没能复现，测试前提不成立")
except AttributeError as exc:
    check("替身复现原始症状（.get 抛 AttributeError）", True, str(exc))

check("field_of 从 Pydantic 对象取字段",
      mod.field_of(FakeTransferInfo(target_diritem=1), "target_diritem") == 1)
check("field_of 从 dict 取字段",
      mod.field_of({"target_diritem": 2}, "target_diritem") == 2)
check("field_of 取不存在的字段返回默认值",
      mod.field_of(FakeTransferInfo(), "nope", "fallback") == "fallback")
check("field_of 对 None 安全",
      mod.field_of(None, "x", "d") == "d")
check("field_of 值为 None 时回落到默认值",
      mod.field_of(FakeTransferInfo(target_diritem=None), "target_diritem", "d") == "d")


# --------------------------------- 2. 事件处理器在 Pydantic 形态下不崩
section("2. 事件处理器在 Pydantic 形态下正常工作")

with Tree() as t:
    lib = t / "电影/外语电影/测试片 (2020)"
    strm(lib / "测试片 (2020) - 2160p.strm")
    dl = t / "下载"
    subtitle(dl / "测试片.2020.2160p.WEB-DL.chs.srt")

    inst = make_plugin(mod,
                       download_movie=str(dl),
                       library_movie=str(t / "电影/外语电影"))

    # 关键：transferinfo 是 Pydantic 替身，不是 dict
    event = mod.Event(event_data={"transferinfo": FakeTransferInfo(
        target_diritem=FakeFileItem(path=str(lib)),
    )})
    try:
        inst.on_transfer_complete(event)
        check("Pydantic 形态的 transferinfo 不再抛异常", True)
    except Exception as exc:
        check("Pydantic 形态的 transferinfo 不再抛异常", False,
              f"{type(exc).__name__}: {exc}")

    # 旧版 dict 形态必须继续能用（不能为了修新版本把老版本改坏）
    event2 = mod.Event(event_data={"transferinfo": {
        "target_diritem": {"path": str(lib)},
    }})
    try:
        inst.on_transfer_complete(event2)
        check("dict 形态的 transferinfo 仍然可用（向后兼容）", True)
    except Exception as exc:
        check("dict 形态的 transferinfo 仍然可用（向后兼容）", False,
              f"{type(exc).__name__}: {exc}")


# ------------------------------------- 3. 事件处理器绝不把异常抛出去
section("3. 事件处理器兜底：任何异常都不外溢到 MP 事件总线")

with Tree() as t:
    lib = t / "电影/外语电影/测试片 (2020)"
    strm(lib / "测试片 (2020) - 2160p.strm")
    dl = t / "下载"
    subtitle(dl / "测试片.2020.2160p.WEB-DL.chs.srt")

    inst = make_plugin(mod,
                       download_movie=str(dl),
                       library_movie=str(t / "电影/外语电影"))

    # 故意塞一个一读取就炸的 transferinfo：属性访问本身抛异常，
    # 模拟「宿主给了一个插件完全不认识的结构」这类情况。
    class Hostile:
        @property
        def target_diritem(self):
            raise RuntimeError("boom")

    event = mod.Event(event_data={"transferinfo": Hostile()})
    try:
        inst.on_transfer_complete(event)
        check("畸形 transferinfo 被兜底捕获，不抛异常", True)
    except Exception as exc:
        check("畸形 transferinfo 被兜底捕获，不抛异常", False,
              f"{type(exc).__name__}: {exc}")

    # 兜底时应该留下日志，便于用户排查
    warns = [m for lvl, m in getattr(mod.logger, "records", []) if lvl == "WARNING"]
    check("兜底时留下 WARNING 日志", bool(warns), f"实际日志: {warns}")


# ------------------------------------------- 4. 边界：target 缺失/为空
section("4. 边界情况")

inst = make_plugin(mod)

for label, payload in [
    ("transferinfo 缺失", {}),
    ("transferinfo 为 None", {"transferinfo": None}),
    ("target_diritem 为 None", {"transferinfo": FakeTransferInfo(target_diritem=None)}),
    ("target_diritem 为空 dict", {"transferinfo": {"target_diritem": {}}}),
    ("path 为空串", {"transferinfo": FakeTransferInfo(
        target_diritem=FakeFileItem(path=""))}),
    ("path 指向不存在的目录", {"transferinfo": FakeTransferInfo(
        target_diritem=FakeFileItem(path="/nowhere/zzz"))}),
]:
    event = mod.Event(event_data=payload)
    try:
        inst.on_transfer_complete(event)
        check(f"{label}：安全返回，不抛异常", True)
    except Exception as exc:
        check(f"{label}：安全返回，不抛异常", False,
              f"{type(exc).__name__}: {exc}")


print()
if failures:
    print(f"❌ 失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("✅ 全部通过")
