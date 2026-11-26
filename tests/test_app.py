"""关键流程自测：掩膜入库、跨片合并/拆开、编号规则、并发与失败隔离。"""

import threading

import pytest

from app import create_app, mask_from_cells, parse_samples


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    app.config["TESTING"] = True
    return app.test_client()


def csv_of(cells):
    """由片内坐标集合生成 16x16 CSV 文本。"""
    grid = [[0] * 16 for _ in range(16)]
    for r, c in cells:
        grid[r][c] = 1
    return "\n".join(",".join(map(str, row)) for row in grid)


def upload(client, sr, sc, text):
    return client.post("/upload", data={"sheet_row": sr, "sheet_col": sc, "text": text})


def state(client):
    return client.get("/api/state").get_json()


def sheet(client, sr, sc):
    return client.get(f"/api/sheet/{sr}/{sc}").get_json()


# ---------------------------------------------------------------- 基本连通与编号

def test_single_sheet_patch_id_and_size(client):
    # L 形：最北一行有 (5,3)、(5,4)，编号取最西的 (5,3)
    upload(client, 0, 0, csv_of({(5, 3), (5, 4), (6, 3)}))
    st = state(client)
    assert st["version"] == 1
    assert st["patches"] == [{"id": "5,3", "size": 3}]
    sh = sheet(client, 0, 0)
    assert sh["version"] == st["version"]
    assert all(c["patch_id"] == "5,3" for c in sh["cells"])


def test_patch_id_prefers_north_over_west(client):
    # (7,1) 比 (6,8) 更西，但 (6,8) 所在行更北 -> 该块编号应为 6,8
    upload(client, 0, 0, csv_of({(6, 8), (7, 1), (6, 9)}))
    # 两片互不相连：{(6,8),(6,9)} 与 {(7,1)}
    ids = {p["id"] for p in state(client)["patches"]}
    assert ids == {"6,8", "7,1"}


def test_diagonal_not_connected(client):
    upload(client, 0, 0, csv_of({(3, 3), (4, 4)}))
    assert len(state(client)["patches"]) == 2


# ---------------------------------------------------------------- 跨片缝合并 / 拆开

def test_merge_across_vertical_seam(client):
    upload(client, 0, 0, csv_of({(8, c) for c in range(10, 16)}))  # 全图 (8,10)-(8,15)
    upload(client, 0, 1, csv_of({(8, c) for c in range(0, 6)}))    # 全图 (8,16)-(8,21)
    st = state(client)
    assert st["patches"] == [{"id": "8,10", "size": 12}]           # 跨缝共边 -> 同一斑块
    sh = sheet(client, 0, 1)
    assert sh["patches"] == [{"id": "8,10", "cells_in_sheet": 6, "total_size": 12}]


def test_merge_across_horizontal_seam(client):
    upload(client, 0, 0, csv_of({(r, 5) for r in range(10, 16)}))  # 全图 (10,5)-(15,5)
    upload(client, 1, 0, csv_of({(r, 5) for r in range(0, 4)}))    # 全图 (16,5)-(19,5)
    assert state(client)["patches"] == [{"id": "10,5", "size": 10}]


def test_replace_sheet_splits_patch_and_updates_totals(client):
    upload(client, 0, 0, csv_of({(8, c) for c in range(10, 16)}))
    upload(client, 0, 1, csv_of({(8, c) for c in range(0, 6)}))
    assert state(client)["patches"] == [{"id": "8,10", "size": 12}]

    # 替换右片：条带错到第 9 行 -> 拆成两块，编号与总格数随之更新
    upload(client, 0, 1, csv_of({(9, c) for c in range(0, 6)}))
    st = state(client)
    assert st["patches"] == [{"id": "8,10", "size": 6}, {"id": "9,16", "size": 6}]
    # 未改动的 (0,0) 片内连通关系沿用：仍是同一斑块、6 格
    sh0 = sheet(client, 0, 0)
    assert sh0["patches"] == [{"id": "8,10", "cells_in_sheet": 6, "total_size": 6}]
    assert all(c["patch_id"] == "8,10" for c in sh0["cells"])


def test_replace_sheet_merges_patches(client):
    # 先两片各自独立，再替换左片使两者跨缝相连 -> 并成一块
    upload(client, 0, 0, csv_of({(2, 2)}))
    upload(client, 0, 1, csv_of({(2, 0)}))                          # 全图 (2,16)
    assert len(state(client)["patches"]) == 2
    # 左片换成第 2 行从列 2 连到列 15 的整条，(2,15) 与右片 (2,16) 跨缝共边 -> 全并
    upload(client, 0, 0, csv_of({(2, c) for c in range(2, 16)}))
    # 最北行=2，最西格=(2,2)；14 + 1 = 15 格
    assert state(client)["patches"] == [{"id": "2,2", "size": 15}]


# ---------------------------------------------------------------- 失败隔离

def test_invalid_csv_rejected_and_state_untouched(client):
    upload(client, 0, 0, csv_of({(1, 1)}))
    before = state(client)

    bad_inputs = [
        "1,1,1",                                  # 行数不足
        "\n".join(["0,0"] * 16),                  # 列数不足
        csv_of({(2, 2)}).replace("1", "2", 1),    # 非法值
        "",
    ]
    for bad in bad_inputs:
        resp = upload(client, 0, 1, bad)
        assert resp.status_code == 400
    after = state(client)
    assert after["version"] == before["version"]      # 版本未推进
    assert after["sheets"] == before["sheets"]        # 已发布掩膜未变
    assert after["patches"] == before["patches"]      # 已发布成果未变


def test_out_of_range_sheet_pos_rejected(client):
    resp = client.post("/upload", data={"sheet_row": -1, "sheet_col": 0, "text": csv_of(set())})
    assert resp.status_code == 400
    assert state(client)["version"] == 0


# ---------------------------------------------------------------- 并发入库

def test_concurrent_uploads_apply_in_commit_order(client):
    # 8 个线程同时交 8 片不同图幅；最终应恰好 8 个版本、8 片、8 个单格斑块
    def worker(i):
        c = client.application.test_client()
        resp = c.post("/upload", data={"sheet_row": i, "sheet_col": 0,
                                       "text": csv_of({(0, 0)})})
        assert resp.status_code == 200 or resp.status_code == 302

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    st = state(client)
    assert st["version"] == 8
    assert len(st["sheets"]) == 8
    assert st["patches"] == [{"id": f"{i * 16},0", "size": 1} for i in range(8)]
    # 掩膜与成果同版本切换：逐片查询的版本号与全局一致
    for i in range(8):
        assert sheet(client, i, 0)["version"] == st["version"]


def test_concurrent_replace_same_sheet_is_serializable(client):
    upload(client, 0, 0, csv_of({(0, 0)}))

    def worker(cells):
        c = client.application.test_client()
        c.post("/upload", data={"sheet_row": 0, "sheet_col": 0, "text": csv_of(cells)})

    t1 = threading.Thread(target=worker, args=({(5, 5)},))
    t2 = threading.Thread(target=worker, args=({(6, 6)},))
    t1.start(); t2.start(); t1.join(); t2.join()

    st = state(client)
    assert st["version"] == 3
    # 最终结果必为两次替换之一，且与成果一致（不会出现掩膜是 A 成果是 B 的混搭）
    assert st["patches"] in ([{"id": "5,5", "size": 1}], [{"id": "6,6", "size": 1}])


# ---------------------------------------------------------------- 重算期间旧成果可查

def test_old_results_visible_during_recompute(client, monkeypatch):
    import app as app_module

    upload(client, 0, 0, csv_of({(1, 1)}))
    orig = app_module.compute_patches
    started = threading.Event()
    release = threading.Event()

    def slow_compute(sheets):
        started.set()
        release.wait(5)
        return orig(sheets)

    monkeypatch.setattr(app_module, "compute_patches", slow_compute)

    def worker():
        c = client.application.test_client()
        c.post("/upload", data={"sheet_row": 0, "sheet_col": 1, "text": csv_of({(2, 2)})})

    t = threading.Thread(target=worker)
    t.start()
    assert started.wait(5)
    # 重算进行中：读到的仍是完整旧成果（v1，只有旧斑块）
    st = state(client)
    assert st["version"] == 1
    assert st["patches"] == [{"id": "1,1", "size": 1}]
    release.set()
    t.join()
    # 重算提交后：掩膜与成果一并切到新版本
    st = state(client)
    assert st["version"] == 2
    assert {p["id"] for p in st["patches"]} == {"1,1", "2,18"}


# ---------------------------------------------------------------- 页面与演示

def test_sheet_page_renders(client):
    upload(client, 0, 0, csv_of({(8, c) for c in range(10, 16)}))
    upload(client, 0, 1, csv_of({(8, c) for c in range(0, 6)}))
    resp = client.get("/sheet/0/1")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "8,10" in html          # 斑块号
    assert "全图共 12 格" in html   # 全图总格数
    assert client.get("/sheet/9/9").status_code == 404


def test_index_page_renders(client):
    upload(client, 0, 0, csv_of({(0, 0)}))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "v1" in resp.get_data(as_text=True)


def test_demo_merge_then_split(client):
    client.post("/demo/merge")
    st = state(client)
    assert st["patches"] == [{"id": "8,10", "size": 12}]
    client.post("/demo/split")
    st = state(client)
    assert st["patches"] == [{"id": "8,10", "size": 6}, {"id": "9,16", "size": 6}]
    client.post("/demo/clear")
    st = state(client)
    assert st["sheets"] == [] and st["patches"] == []


def test_mask_and_results_switch_together(client):
    upload(client, 0, 0, csv_of({(8, c) for c in range(10, 16)}))
    upload(client, 0, 1, csv_of({(8, c) for c in range(0, 6)}))
    st = state(client)
    sh = sheet(client, 0, 1)
    assert sh["version"] == st["version"]
    # 该片掩膜中的每个 1 都应在成果里出现，且数量一致
    assert sh["mask"].count("1") == 6 == len(sh["cells"])


def test_mask_from_cells_helper():
    m = mask_from_cells([(0, 0), (15, 15)])
    assert len(m) == 256 and m[0] == "1" and m[255] == "1" and m.count("1") == 2


# ---------------------------------------------------------------- 样方表：上传 / 替换 / 对照

def post_samples(client, text=""):
    return client.post("/samples", data={"text": text})


def samples_api(client):
    return client.get("/api/samples").get_json()


def comparison(client):
    return client.get("/api/comparison").get_json()


def test_parse_samples_rules():
    assert parse_samples("1,2,1") == {(1, 2): 1}
    assert parse_samples("行,列,值\n1,2,0\n3,4,1") == {(1, 2): 0, (3, 4): 1}  # 表头
    with pytest.raises(ValueError, match="重复坐标"):
        parse_samples("1,2,1\n1,2,0")
    with pytest.raises(ValueError):
        parse_samples("1,2,3")        # 值非法（坐标是整数，不当表头）
    with pytest.raises(ValueError):
        parse_samples("1,2")          # 列数不足
    with pytest.raises(ValueError):
        parse_samples("")             # 空表
    with pytest.raises(ValueError):
        parse_samples("16000,0,1")    # 越界


def test_samples_upload_and_comparison(client):
    upload(client, 0, 0, csv_of({(8, 10), (8, 12)}))          # v1
    resp = post_samples(client, "8,10,1\n8,12,0\n4,4,1\n20,3,0")
    assert resp.status_code == 302
    sa = samples_api(client)
    assert sa["sample_version"] == 1
    assert sa["entries"] == [
        {"row": 4, "col": 4, "value": 1},
        {"row": 8, "col": 10, "value": 1},
        {"row": 8, "col": 12, "value": 0},
        {"row": 20, "col": 3, "value": 0},
    ]
    cmp = comparison(client)
    assert cmp["map_version"] == 1 and cmp["sample_version"] == 1   # 注明地图版和样方表版
    assert cmp["consistent"] == [{"row": 8, "col": 10}]
    assert cmp["mismatched"] == [{"row": 4, "col": 4}, {"row": 8, "col": 12}]
    assert cmp["missing"] == [{"row": 20, "col": 3}]                # 图幅 (1,0) 未交付 -> 缺测


def test_samples_replace_bumps_version(client):
    upload(client, 0, 0, csv_of({(1, 1)}))
    post_samples(client, "行,列,值\n1,1,1")
    assert samples_api(client)["sample_version"] == 1
    post_samples(client, "1,1,0\n2,2,1")                            # 整表替换
    sa = samples_api(client)
    assert sa["sample_version"] == 2
    assert sa["entries"] == [{"row": 1, "col": 1, "value": 0},
                             {"row": 2, "col": 2, "value": 1}]


def test_duplicate_sample_coords_rejected(client):
    upload(client, 0, 0, csv_of({(1, 1)}))
    post_samples(client, "1,1,1")
    before = samples_api(client)
    resp = post_samples(client, "2,2,1\n2,2,0")
    assert resp.status_code == 400
    assert "重复坐标" in resp.get_data(as_text=True)
    assert samples_api(client) == before                            # 原表继续生效


def test_invalid_sample_csv_rejected_and_old_table_kept(client):
    upload(client, 0, 0, csv_of({(1, 1)}))
    post_samples(client, "1,1,1")
    before = samples_api(client)
    bad_inputs = [
        "",                       # 空表
        "1,1",                    # 列数不足
        "1,1,2",                  # 值非法
        "1,1,1,1",                # 列数过多
        "1,1,1\n99999,0,1",       # 坐标越界
        "1,1,1\n-1,0,1",          # 负坐标
        "行,列,值",                # 只有表头
    ]
    for bad in bad_inputs:
        resp = post_samples(client, bad)
        assert resp.status_code == 400, bad
    assert samples_api(client) == before     # 换表失败则继续使用原表
    assert state(client)["version"] == 1     # 地图侧完全不受影响


# ---------------------------------------------------------------- 样方守护：退回换片

def test_publish_breaking_consistent_cell_rejected(client):
    upload(client, 0, 0, csv_of({(8, 10), (8, 12)}))     # v1
    post_samples(client, "8,10,1\n8,12,1\n4,4,1")        # 一致 2 · 不符 1
    before = state(client)
    # 换片把 (8,10) 改成 0：原本一致 -> 不符，整片退回并列出坐标
    resp = upload(client, 0, 0, csv_of({(8, 12)}))
    assert resp.status_code == 409
    html = resp.get_data(as_text=True)
    assert "(8,10)" in html and "退回" in html
    after = state(client)
    assert after == before                                # 原掩膜与斑块成果照常可查
    assert sheet(client, 0, 0)["mask"].count("1") == 2
    assert client.get("/sheet/0/0").status_code == 200
    cmp = comparison(client)                              # 对照仍按旧版：两格一致
    assert cmp["consistent"] == [{"row": 8, "col": 10}, {"row": 8, "col": 12}]


def test_consistent_to_missing_rejected_on_sheet_removal(client):
    upload(client, 0, 0, csv_of({(1, 1)}))
    post_samples(client, "1,1,1")
    resp = client.post("/demo/clear")                     # 清空会让 (1,1) 变为缺测
    assert resp.status_code == 409
    assert "(1,1)" in resp.get_data(as_text=True)
    assert len(state(client)["sheets"]) == 1              # 原掩膜不变


def test_batched_corrections_accepted(client):
    upload(client, 0, 0, csv_of({(8, 10)}))               # v1
    post_samples(client, "8,10,1\n4,4,1\n4,5,1")          # 一致 1 · 不符 2
    # 第一批：只修 (4,4)，留下 (4,5)
    resp = upload(client, 0, 0, csv_of({(8, 10), (4, 4)}))
    assert resp.status_code == 302
    cmp = comparison(client)
    assert cmp["mismatched"] == [{"row": 4, "col": 5}]
    # 第二批：再修 (4,5)
    resp = upload(client, 0, 0, csv_of({(8, 10), (4, 4), (4, 5)}))
    assert resp.status_code == 302
    cmp = comparison(client)
    assert cmp["mismatched"] == [] and len(cmp["consistent"]) == 3


def test_fix_does_not_offset_regression(client):
    upload(client, 0, 0, csv_of({(8, 10)}))               # (8,10) 一致, (4,4) 不符
    post_samples(client, "8,10,1\n4,4,1")
    # 同一版修好 (4,4) 但改错 (8,10)：净一致数不变，仍须整片退回
    resp = upload(client, 0, 0, csv_of({(4, 4)}))
    assert resp.status_code == 409
    assert "(8,10)" in resp.get_data(as_text=True)
    assert state(client)["version"] == 1


def test_mismatched_or_missing_cells_not_protected(client):
    upload(client, 0, 0, csv_of({(8, 10)}))
    post_samples(client, "4,4,1\n20,3,0")                 # 不符 + 缺测，无一致格
    # 缺测格所在图幅交付且值不符：允许（只守护原本一致的格）
    resp = upload(client, 1, 0, csv_of({(4, 3)}))         # 全图 (20,3)=1 -> 不符
    assert resp.status_code == 302
    cmp = comparison(client)
    assert cmp["missing"] == []
    assert {"row": 20, "col": 3} in cmp["mismatched"]


def test_sample_replace_during_recompute_and_guard_uses_commit_time_table(client, monkeypatch):
    import app as app_module

    upload(client, 0, 0, csv_of({(1, 1)}))                # v1
    post_samples(client, "5,5,0")                          # sv1：(5,5) 一致

    orig = app_module.compute_patches
    started = threading.Event()
    release = threading.Event()

    def slow_compute(sheets):
        started.set()
        release.wait(5)
        return orig(sheets)

    monkeypatch.setattr(app_module, "compute_patches", slow_compute)

    result = {}

    def worker():
        c = client.application.test_client()
        # 换片把 (1,1) 改 0；按 sv1 判断可通过，按 sv2 判断须退回
        result["resp"] = c.post("/upload", data={
            "sheet_row": 0, "sheet_col": 0, "text": csv_of(set())})

    t = threading.Thread(target=worker)
    t.start()
    assert started.wait(5)
    # 重算斑块进行中：换样方表不被阻塞，立即生效
    resp = post_samples(client, "5,5,0\n1,1,1")
    assert resp.status_code == 302
    assert samples_api(client)["sample_version"] == 2
    release.set()
    t.join()
    # 交片按实际入库时生效的 sv2 判断：(1,1) 原本一致 -> 不符，退回
    assert result["resp"].status_code == 409
    assert "(1,1)" in result["resp"].get_data(as_text=True)
    st = state(client)
    assert st["version"] == 1                               # 原掩膜与成果不变
    assert st["patches"] == [{"id": "1,1", "size": 1}]


# ---------------------------------------------------------------- 样方演示与页面

def test_demo_sample_scenario(client):
    client.post("/demo/check_load")
    cmp = comparison(client)
    assert (len(cmp["consistent"]), len(cmp["mismatched"]), len(cmp["missing"])) == (3, 2, 1)
    client.post("/demo/check_fix1")                        # 分次修正第一批
    assert len(comparison(client)["mismatched"]) == 1
    client.post("/demo/check_fix2")                        # 分次修正第二批
    assert len(comparison(client)["mismatched"]) == 0
    client.post("/demo/check_cover")                       # 补交图幅，缺测转一致
    cmp = comparison(client)
    assert cmp["missing"] == [] and len(cmp["consistent"]) == 6
    # 退回换片演示：把已对齐的 (8,10) 改错 -> 409 并列出坐标
    before = state(client)
    resp = client.post("/demo/check_bad")
    assert resp.status_code == 409
    assert "(8,10)" in resp.get_data(as_text=True)
    assert state(client) == before
    # 演示重置（样方表换代）后可重新载入
    client.post("/demo/check_reset")
    client.post("/demo/check_load")
    assert len(comparison(client)["mismatched"]) == 2


def test_index_shows_versions_and_comparison(client):
    upload(client, 0, 0, csv_of({(8, 10)}))
    post_samples(client, "8,10,1\n4,4,1\n20,3,0")
    html = client.get("/").get_data(as_text=True)
    assert "地图版 v1" in html and "样方表版 sv1" in html
    assert "(8,10)" in html and "(4,4)" in html and "(20,3)" in html


def test_sheet_page_shows_sample_check(client):
    upload(client, 0, 0, csv_of({(8, 10)}))
    post_samples(client, "8,10,1\n4,4,1")
    html = client.get("/sheet/0/0").get_data(as_text=True)
    assert "本片样方对照" in html and "一致" in html and "不符" in html


def test_api_state_includes_sample_info(client):
    upload(client, 0, 0, csv_of({(8, 10)}))
    post_samples(client, "8,10,1\n4,4,1")
    st = state(client)
    assert st["sample_version"] == 1
    assert st["comparison"] == {"consistent": 1, "mismatched": 1, "missing": 0}
