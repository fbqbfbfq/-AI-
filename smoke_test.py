# -*- coding: utf-8 -*-
"""临时冒烟测试(无 API 调用),跑完即删。"""
import pathlib
import shutil

import numpy as np

import emotion
import memory
import tool_kit

ok = 0
fail = 0

# 先清掉上次运行残留的临时数据,保证用例可重复执行
shutil.rmtree(pathlib.Path(__file__).parent / "_smoke_data", ignore_errors=True)


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS {name} {detail}")
    else:
        fail += 1
        print(f"  FAIL {name} {detail}")


def _close_store(st):
    """尽力释放 ChromaDB 句柄,便于 Windows 下清理临时目录。"""
    try:
        st._client.clear_system_cache()
    except Exception:
        pass
    try:
        st._client._system.stop()
    except Exception:
        pass


print("[1] 情绪数学")
check("label 生气", emotion.label_of(-0.8, 0.9) == "生气", emotion.label_of(-0.8, 0.9))
check("label 兴奋", emotion.label_of(0.8, 0.9) == "兴奋")
check("label 平静", emotion.label_of(0.1, 0.2) == "平静")
check("label 难过", emotion.label_of(-0.8, 0.2) == "难过")
v, a = emotion.settle({"valence": 0.5, "arousal": 0.8}, None,
                      {"valence": -0.5, "arousal": 0.9},
                      decay=0.85, mem_w=0.3, user_w=0.7)
check("settle 值域", -1 <= v <= 1 and 0 <= a <= 1, f"v={v:.3f} a={a:.3f}")
check("settle 受消息主导", v < 0, f"v={v:.3f}")
t_hot = emotion.emotion_temperature(0.5, 0.9, 0.7)
t_calm = emotion.emotion_temperature(0.0, 0.2, 0.7)
check("高唤醒温度更高", t_hot > t_calm, f"{t_hot:.3f} vs {t_calm:.3f}")
p = emotion.parse_emotion('{"emotion":"开心","valence":0.6,"arousal":0.8}')
check("情绪解析", p and p["valence"] == 0.6 and p["emotion"] == "开心", str(p))

print("[2] ChromaDB 存储:去重 / 检索加权 / 主动话题")
testdir = pathlib.Path(__file__).parent / "_smoke_data" / "store"
shutil.rmtree(testdir, ignore_errors=True)
st = memory.MemoryStore(testdir)
v1 = np.array([1, 0, 0, 0], dtype=np.float32)
v2 = np.array([0, 1, 0, 0], dtype=np.float32)
st.add_items([{"text": "用户喜欢猫", "importance": 0.8, "emotion": "开心",
               "emotion_value": 0.5, "topic": "宠物"}], [v1], dedup_threshold=0.85)
check("新增 1 条", st.count() == 1)
st.add_items([{"text": "用户喜欢猫,还养了一只", "importance": 0.9,
               "emotion": "开心", "emotion_value": 0.5, "topic": "宠物"}],
             [v1], dedup_threshold=0.85)
check("高度重叠→更新不新增", st.count() == 1, f"n={st.count()}")
items = st.items()
check("更新重算重要度", abs(items[0]["importance"] - 0.9) < 1e-6,
      f"imp={items[0]['importance']}")
check("更新合并文本", "养了一只" in items[0]["text"], items[0]["text"])
st.add_items([{"text": "用户喜欢狗", "importance": 0.6, "emotion": "平静",
               "emotion_value": 0.0, "topic": "宠物"}], [v2], dedup_threshold=0.85)
check("低重叠→新增", st.count() == 2, f"n={st.count()}")
r = st.search(v1, k=2)
check("检索带新字段", all(k in r[0] for k in
      ("importance", "emotion", "emotion_value", "dynamic_importance",
       "similarity", "topic")), str(list(r[0].keys())))
check("相似度高者排前", r[0]["text"].startswith("用户喜欢猫"), r[0]["text"])
check("话题字段正确", r[0]["topic"] == "宠物", str(r[0]["topic"]))
check("all_topics 去重", st.all_topics() == ["宠物"], str(st.all_topics()))
t1 = st.pick_proactive_topic()
t2 = st.pick_proactive_topic()
check("主动话题防重复(两次取不同条)", t1["id"] != t2["id"],
      f"{t1['text']} / {t2['text']}")
_close_store(st)

print("[3] 压缩解析(含话题)")
c = memory.Compressor(lambda msgs: "x")
items3 = c._parse('[{"text":"要点A","topic":"科技","importance":4,'
                  '"emotion":"平静","emotion_value":0.0},'
                  '{"text":"要点B","topic":"美食","importance":0.7,'
                  '"emotion":"开心","emotion_value":0.6}]')
check("结构化解析", len(items3) == 2 and items3[0]["text"] == "要点A",
      str(items3))
check("话题字段解析", items3[0]["topic"] == "科技" and items3[1]["topic"] == "美食")
check("重要度 4→0.8 归一化", abs(items3[0]["importance"] - 0.8) < 1e-6,
      str(items3[0]["importance"]))
check("0~1 重要度原样保留", abs(items3[1]["importance"] - 0.7) < 1e-6)
check("缺话题补默认", c._parse('[{"text":"x"}]')[0]["topic"] == "未分类")
check("坏 JSON 不崩返回空", c._parse("不是json") == [])
check("缺字段补默认", c._parse('[{"text":"x"}]')[0]["importance"] == 0.5)

print("[4] 工具")
found = tool_kit.discover_tools()
names = [n for n, _, _ in found]
check("发现内置工具", {"web_search", "calculate", "get_time"} <= set(names), str(names))
safe = [n for n, s, _ in found if s.get("safe")]
check("白名单标记", {"web_search", "calculate", "get_time"} <= set(safe), str(safe))
kit = tool_kit.ToolKit(allow_generated_code=False)
check("默认无 run_python_code", "run_python_code" not in kit.tools)
kit2 = tool_kit.ToolKit(allow_generated_code=True)
check("开启后有 run_python_code", "run_python_code" in kit2.tools)
tools = kit.openai_tools()
check("openai_tools 结构", all(t["type"] == "function" for t in tools), str(len(tools)))
r4 = kit.execute("calculate", {"expr": "3*4+2"})
check("执行计算器", r4.strip() == "14", r4)
r4 = kit2.execute("run_python_code", {"code": "print(sum(range(11)))"})
check("沙箱执行", "55" in r4, r4)
r4 = kit2.execute("run_python_code", {"code": "import os; os.system('dir')"})
check("沙箱拦截危险代码", "沙箱拦截" in r4, r4)
check("未注册工具报错", "未注册" in kit.execute("nope", {}))
# 自定义白名单工具:code 在沙箱执行,args 变量注入模型参数
kit3 = tool_kit.ToolKit(allow_generated_code=False)
kit3.tools["double_it"] = {
    "spec": {"name": "double_it", "description": "翻倍", "safe": True,
             "parameters": {"type": "object", "properties": {}},
             "code": "print(int(args['x']) * 2)"},
    "run": None,
}
r4 = kit3.execute("double_it", {"x": 21})
check("自定义工具沙箱执行(args 注入)", "42" in r4, r4)
kit3.tools["empty_tool"] = {"spec": {"name": "empty_tool", "description": "x",
                                     "safe": True, "parameters": {}, "code": ""},
                            "run": None}
check("无 code 自定义工具报不可执行", "没有 code" in kit3.execute("empty_tool", {}))

print("[5] 旧数据自动迁移到 ChromaDB")
testdir2 = pathlib.Path(__file__).parent / "_smoke_data" / "old"
shutil.rmtree(testdir2, ignore_errors=True)
testdir2.mkdir(parents=True)
(testdir2 / "memory.json").write_text(
    '[{"id":"l2-1","time":"2026-08-17 周一 10:00:00","text":"旧记忆"}]',
    encoding="utf-8")
np.save(testdir2 / "vectors.npy", np.array([[1, 0, 0, 0]], dtype=np.float16))
st2 = memory.MemoryStore(testdir2)
items2 = st2.items()
check("旧数据迁移进 ChromaDB", len(items2) == 1 and items2[0]["text"] == "旧记忆",
      str(items2))
check("旧数据补字段", items2[0].get("importance") == 0.5
      and items2[0].get("topic") == "未分类"
      and items2[0].get("last_proactive_used_at") == "", str(items2[0]))
check("旧文件已清理", not (testdir2 / "memory.json").exists()
      and not (testdir2 / "vectors.npy").exists())
_close_store(st2)

print("[6] 逐条删除 / 清空")
testdir3 = pathlib.Path(__file__).parent / "_smoke_data" / "del"
shutil.rmtree(testdir3, ignore_errors=True)
st3 = memory.MemoryStore(testdir3)
st3.add_items([{"text": "条目A", "topic": "测试"}, {"text": "条目B", "topic": "测试"}],
              [np.array([1, 0, 0, 0], dtype=np.float32),
               np.array([0, 1, 0, 0], dtype=np.float32)], dedup_threshold=0.85)
check("L2 先有 2 条", st3.count() == 2)
check("删除存在的 id 返回 True", st3.remove_by_id(st3.items()[0]["id"]) is True)
check("删后 1 条", st3.count() == 1)
check("删除不存在的 id 返回 False", st3.remove_by_id("不存在") is False)
st3.clear()
check("clear 清空", st3.count() == 0)
l1f = pathlib.Path(__file__).parent / "_smoke_data" / "l1.json"
l1s = memory.L1Store(l1f, maxlen=40)
l1s.add("u1", "a1", "t1")
l1s.add("u2", "a2", "t2")
l1s.remove_at(0)
check("L1 删除第 0 轮", len(l1s.all()) == 1 and l1s.all()[0]["user"] == "u2")
l1s.add("u3", "a3", "t3")
l1s.reload()
check("L1 reload 后读到落盘数据", len(l1s.all()) == 2)
_close_store(st3)

print("[7] 回复前缀剥离(模型模仿 '[时间] AI:' 格式)")
import service
check("剥 [日期] AI:", service.strip_context_prefix(
    "[2026-08-18 周二 19:00:35] AI: 你好") == "你好")
check("剥 [时间] 用户:(全角冒号)", service.strip_context_prefix(
    "[19:00] 用户： 在吗") == "在吗")
check("剥 年月日 格式", service.strip_context_prefix(
    "[2026年8月18日] AI: x") == "x")
check("多层前缀剥净", service.strip_context_prefix(
    "[2026-08-18 19:00] AI: [19:01] AI: 内容") == "内容")
check("普通方括号不动", service.strip_context_prefix(
    "[重点] 你好") == "[重点] 你好")
check("无前缀不动", service.strip_context_prefix("正常回复") == "正常回复")
check("clean_reply 逐段剥", service.clean_reply(
    "[2026-08-18 19:00] AI: 第一段|||[19:00] AI: 第二段") == "第一段|||第二段")

print("[8] 动态检索条数 K=floor(ln(N))(1~10)")
check("ln(1)=0 → 下限 1", memory.retrieval_k(1) == 1)
check("ln(2)≈0.69 → 1", memory.retrieval_k(2) == 1)
check("ln(20)≈2.99 → 2", memory.retrieval_k(20) == 2)
check("ln(21)≈3.04 → 3", memory.retrieval_k(21) == 3)
check("ln(30000)≈10.3 → 上限 10", memory.retrieval_k(30000) == 10)
check("空库返回 0", memory.retrieval_k(0) == 0)

print("[9] 检索相似度门槛(45% 以上才检索)+ 用户消息缓冲")
testdir4 = pathlib.Path(__file__).parent / "_smoke_data" / "gate"
shutil.rmtree(testdir4, ignore_errors=True)
st4 = memory.MemoryStore(testdir4)
st4.add_items([{"text": "条目A", "topic": "测试"}, {"text": "条目B", "topic": "测试"}],
              [np.array([1, 0, 0, 0], dtype=np.float32),
               np.array([0, 1, 0, 0], dtype=np.float32)], dedup_threshold=0.85)
r5 = st4.search(np.array([1, 0, 0, 0], dtype=np.float32), k=2,
                min_similarity=0.45)
check("门槛过滤:正交向量(sim=0)不召回", len(r5) == 1 and r5[0]["text"] == "条目A",
      str([x["text"] for x in r5]))
r6 = st4.search(np.array([0, 1, 0, 0], dtype=np.float32), k=2,
                min_similarity=0.45)
check("反向查询命中条目B", len(r6) == 1 and r6[0]["text"] == "条目B",
      str([x["text"] for x in r6]))
r7 = st4.search(np.array([1, 0, 0, 0], dtype=np.float32), k=2,
                min_similarity=0.0)
check("门槛调 0 时全部召回", len(r7) == 2, str([x["text"] for x in r7]))
_close_store(st4)

buf_path = pathlib.Path(__file__).parent / "_smoke_data" / "user_messages.json"
buf = memory.UserMessageBuffer(buf_path, batch=3)
for i in range(3):
    buf.add(f"消息{i}", f"t{i}")
check("攒满 3 条 claim 成功", buf.claim() == ["消息0", "消息1", "消息2"])
check("claim 期间再次 claim 返回 None(防重复)", buf.claim() is None)
buf.add("消息3", "t3")
buf.release(True)
check("release(True) 只清已认领部分", buf.count() == 1)
buf.release(False)
check("release(False) 保留缓冲", buf.count() == 1)
buf2 = memory.UserMessageBuffer(buf_path, batch=3)
check("缓冲 JSON 落盘可重读", buf2.count() == 1)
buf3 = memory.UserMessageBuffer(pathlib.Path(__file__).parent
                                / "_smoke_data" / "user_messages2.json", batch=3)
buf3.add("m", "t")
check("不满批 claim 返回 None", buf3.claim() is None)
check("单例共享同一份内存", memory.get_user_message_buffer(buf_path, batch=3) is
      memory.get_user_message_buffer(buf_path, batch=3))

shutil.rmtree(pathlib.Path(__file__).parent / "_smoke_data", ignore_errors=True)
print(f"\n===== 结果: {ok} PASS / {fail} FAIL =====")
raise SystemExit(1 if fail else 0)
