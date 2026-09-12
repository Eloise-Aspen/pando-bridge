"""界面偏好前端侧（fix-settings-persistence Task 2/3，裁决 2/3/7/8/9）。

三部分（第三部分见文件末尾：默认模型行的档位选择与行内文案规则）：
1. 源码不变量 —— `fetch('/config')` 已不复存在（那个端点从来没有过，一直 404），
   助手名改自 `/settings` 取；会话级临时切换的键一字未动（裁决 3）。
2. **离线替身** —— 把 index.html 里的偏好同步块原样抠出来丢进 node 跑，
   用桩件模拟「服务端不可达 / 服务端有值 / 服务端空库」三种现场，
   断言离线时页面照常拿到本地缓存的昵称与默认模型，且不弹任何错。
"""

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
NODE = shutil.which("node")


def _source() -> str:
    return INDEX.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 源码不变量

def test_config_endpoint_gone():
    """完成标准 5：全文不再有 /config 这个幽灵端点。"""
    src = _source()
    assert "fetch('/config')" not in src
    assert '"/config"' not in src and "'/config'" not in src


def test_assistant_name_comes_from_settings():
    """助手名走 /settings 同步后的本地缓存，boot 里先 await syncUiPrefs()。"""
    src = _source()
    boot = src.split("async function boot(", 1)[1]
    head = boot[: boot.index("loadModels()")]
    assert "await syncUiPrefs()" in head
    assert "ASST=localStorage.getItem('assistantName')||'Claude'" in head


def test_ui_pref_keys_are_the_four():
    src = _source()
    m = re.search(r"const UI_PREF_KEYS=\[(.*?)\];", src)
    assert m, "UI_PREF_KEYS 未定义"
    keys = [k.strip().strip("'\"") for k in m.group(1).split(",")]
    assert keys == ["defaultModel", "defaultEffort", "userNickname", "assistantName"]


def test_session_level_prefs_untouched():
    """裁决 3：单窗口临时换模型/换档仍只写会话级键，不进 UI_PREF_KEYS 那条管道。"""
    src = _source()
    assert "const _mKey = sid => 'sessionModel:'+sid" in src
    body = src.split("function saveSessionPrefs(", 1)[1].split("\n}", 1)[0]
    assert "savePref(" not in body


def test_nickname_write_is_debounced():
    """裁决 8：昵称 500ms 防抖；模型点选立即写。"""
    src = _source()
    ident = src.split("function saveIdentity(", 1)[1].split("\n}", 1)[0]
    assert "savePref('userNickname',nick,500)" in ident
    assert "savePref('assistantName',asst,500)" in ident
    sel = src.split("function selectDefModel(", 1)[1].split("\n}", 1)[0]
    assert "savePref('defaultModel', id, 0)" in sel


# ---------------------------------------------------------------- 离线替身

def _pref_block() -> str:
    """抠出 index.html 里的偏好同步块（UI_PREF_KEYS … syncUiPrefs 结尾）。"""
    src = _source()
    start = src.index("const UI_PREF_KEYS=")
    end = src.index("  return true;\n}", start) + len("  return true;\n}")
    return src[start:end]


_HARNESS = """
// ---- 桩件：只提供被抠出的块所依赖的外部符号 ----
const _store = __STORE__;
const localStorage = {
  getItem: k => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
  removeItem: k => { delete _store[k]; },
};
const posts = [];
let errorsShown = 0;
const alert = () => { errorsShown++; };
const toast = () => { errorsShown++; };
const REMOTE = __REMOTE__;          // null = 服务端不可达
const fetch = async (url, opt) => {
  if (REMOTE === null) throw new Error('offline');
  if (opt && opt.method === 'POST') { posts.push(JSON.parse(opt.body)); return { ok: true }; }
  return { ok: true, json: async () => REMOTE };
};
let currentSessionId = __SID__;
let currentModel = localStorage.getItem('defaultModel') || 'claude-sonnet-5';
let currentEffort = localStorage.getItem('defaultEffort') || '';
const EFFORTS = [['','默认'],['low','Low'],['medium','Medium'],['high','High'],['xhigh','XHigh'],['max','Max']];
const _mKey = sid => 'sessionModel:' + sid;
const _eKey = sid => 'sessionEffort:' + sid;
// adoptSessionPrefs 在本次加载里是否拿全局默认现盖的章（缓存被清空的设备＝true）
let _prefsStamped = __STAMPED__;
const _defModel = () => localStorage.getItem('defaultModel') || currentModel;
const _defEffort = () => { const e = localStorage.getItem('defaultEffort') || ''; return EFFORTS.some(x => x[0] === e) ? e : ''; };
let btnSyncs = 0;
const syncModelBtn = () => { btnSyncs++; };

__BLOCK__

// ---- 剧本 ----
(async () => {
  const ok = await syncUiPrefs();
  __SCRIPT__
  await new Promise(r => setTimeout(r, 700));   // 等防抖落地
  console.log(JSON.stringify({
    ok, store: _store, posts, errorsShown, btnSyncs,
    nick: localStorage.getItem('userNickname') || '',
    defModel: _defModel(), defEffort: _defEffort(),
  }));
})();
"""


def _run(store, remote, sid="null", script="", stamped=False):
    js = (_HARNESS
          .replace("__STORE__", json.dumps(store))
          .replace("__REMOTE__", json.dumps(remote) if remote is not None else "null")
          .replace("__SID__", sid)
          .replace("__STAMPED__", "true" if stamped else "false")
          .replace("__BLOCK__", _pref_block())
          .replace("__SCRIPT__", textwrap.dedent(script)))
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 跑前端离线替身")


def test_offline_falls_back_to_local_cache():
    """完成标准 6：服务端不可达时，昵称与默认模型照常来自本地缓存，且不弹错。"""
    r = _run({"userNickname": "小猫", "assistantName": "Caelum",
              "defaultModel": "claude-opus-5", "defaultEffort": "high"}, None)
    assert r["ok"] is False
    assert r["nick"] == "小猫"
    assert r["defModel"] == "claude-opus-5"
    assert r["defEffort"] == "high"
    assert r["errorsShown"] == 0
    assert r["posts"] == []            # 离线时不产生任何写请求残留


def test_server_value_wins_and_is_cached_back():
    """裁决 2：服务端值覆盖本地缓存并回写本地。"""
    r = _run({"userNickname": "旧名", "defaultModel": "claude-sonnet-5"},
             {"userNickname": "小猫", "assistantName": "Caelum",
              "defaultModel": "claude-opus-5", "defaultEffort": "high"})
    assert r["ok"] is True
    assert r["store"]["userNickname"] == "小猫"
    assert r["store"]["assistantName"] == "Caelum"
    assert r["store"]["defaultModel"] == "claude-opus-5"
    assert r["defEffort"] == "high"
    assert r["posts"] == []            # 服务端四项都有值 → 无需种子


def test_empty_server_gets_seeded_from_local_once():
    """裁决 9：服务端该键为空而本地有值 → 一次性种子上传，不做迁移脚本。"""
    r = _run({"userNickname": "小猫", "defaultModel": "claude-opus-5"},
             {"userNickname": "", "assistantName": "", "defaultModel": "", "defaultEffort": ""})
    assert r["posts"] == [{"userNickname": "小猫", "defaultModel": "claude-opus-5"}]
    assert r["store"]["userNickname"] == "小猫"      # 空库不会把已有昵称冲掉


def test_sync_does_not_clobber_this_window_choice():
    """裁决 3：本会话已有自己的模型选择时，同步不回头改它。"""
    r = _run({"defaultModel": "claude-sonnet-5", "sessionModel:s1": "claude-fable-5"},
             {"defaultModel": "claude-opus-5", "defaultEffort": "", "userNickname": "", "assistantName": ""},
             sid="'s1'")
    assert r["store"]["sessionModel:s1"] == "claude-fable-5"
    assert r["btnSyncs"] == 0          # 没碰运行态，连按钮都不用重刷


def test_stamped_default_is_corrected_by_server_value():
    """核账补洞（2026-09-12 主窗口）：缓存被清空的设备上，adoptSessionPrefs 会在
    syncUiPrefs 之前拿「尚未与服务端对账的回落默认」给本会话盖章。那枚章不是用户的
    选择，服务端值到了必须改写它——否则本 bug 的原始现场（手机清站点数据后重开）下，
    这个会话会一直用错模型。"""
    r = _run({}, {"defaultModel": "claude-opus-5", "defaultEffort": "high",
                  "userNickname": "", "assistantName": ""},
             sid="'s1'", stamped=True)
    assert r["store"]["sessionModel:s1"] == "claude-opus-5"
    assert r["store"]["sessionEffort:s1"] == "high"
    assert r["btnSyncs"] == 1


def test_nickname_debounced_into_one_post():
    """裁决 8：逐字符输入合并成一次 POST，值是最后一次的。"""
    r = _run({}, {"userNickname": "", "assistantName": "", "defaultModel": "", "defaultEffort": ""},
             script="""
             savePref('userNickname', '小', 500);
             savePref('userNickname', '小猫', 500);
             savePref('assistantName', 'Cae', 500);
             """)
    assert r["posts"] == [{"userNickname": "小猫", "assistantName": "Cae"}]
    assert r["store"]["userNickname"] == "小猫"      # 本地先落，不等网络


def test_point_select_writes_immediately():
    """模型/档位是点选 → debounce 0，立刻打接口。"""
    r = _run({}, {"userNickname": "", "assistantName": "", "defaultModel": "", "defaultEffort": ""},
             script="savePref('defaultModel', 'claude-opus-5', 0);")
    assert r["posts"][0] == {"defaultModel": "claude-opus-5"}


def test_clearing_nickname_writes_empty_string():
    """清空昵称 = 本地删键 + 服务端写空串（不是留着旧值）。"""
    r = _run({"userNickname": "小猫"},
             {"userNickname": "小猫", "assistantName": "", "defaultModel": "", "defaultEffort": ""},
             script="savePref('userNickname', '', 0);")
    assert "userNickname" not in r["store"]
    assert {"userNickname": ""} in r["posts"]


# ---------------------------------------------------------------- Task 3：默认模型行加档位

def test_def_model_pop_has_effort_row():
    """裁决 7：默认模型弹层底部有档位行，与聊天侧同一套 EFFORTS 常量、同一套 .eff-pill 规格。"""
    src = _source()
    body = src.split("function renderDefModelPop(", 1)[1].split("\n}", 1)[0]
    assert 'class="eff-row"' in body
    assert "EFFORTS.map(" in body
    assert "selectDefEffort(" in body
    # 高亮跟的是**默认**档位，不是当前窗口的临时档位（裁决 3）
    assert "id===_defEffort()" in body
    assert "currentEffort" not in body


def test_def_effort_select_writes_default_only():
    """选档 = 写 defaultEffort（立即），不碰会话级键。"""
    src = _source()
    body = src.split("function selectDefEffort(", 1)[1].split("\n}", 1)[0]
    assert "savePref('defaultEffort'" in body
    assert ", 0)" in body
    assert "saveSessionPrefs" not in body and "applyEffort" not in body


def test_row_label_uses_shared_helper_everywhere():
    """三处行内文案（渲染设置页 / 模型列表刷新 / 选中后）都走同一个 defModelRowLabel。"""
    src = _source()
    assert src.count("defModelRowLabel()") >= 4        # 1 处定义 + 3 处使用
    assert 'id="s-model">${defModelRowLabel()}' in src


def test_row_label_formula():
    """选了档显示「模型 · 档位」，选「默认」档不追加后缀（与输入区按钮同规则）。"""
    src = _source()
    line = re.search(r"const defModelRowLabel = .*", src).group(0)
    js = """
    const MODELS=[['claude-sonnet-5','Sonnet 5',true]];
    const EFFORTS=[['','默认'],['low','Low'],['high','High']];
    const modelLabel=id=>(MODELS.find(m=>m[0]===id)||['',id])[1]||id;
    const effortLabel=id=>(EFFORTS.find(e=>e[0]===id)||['',''])[1];
    let _M='claude-sonnet-5', _E='';
    const _defModel=()=>_M, _defEffort=()=>_E;
    %s
    const out=[];
    out.push(defModelRowLabel());
    _E='high'; out.push(defModelRowLabel());
    _M='my-custom'; out.push(defModelRowLabel());
    console.log(JSON.stringify(out));
    """ % line
    r = subprocess.run([NODE, "-e", textwrap.dedent(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip()) == ["Sonnet 5", "Sonnet 5 · High", "my-custom · High"]
