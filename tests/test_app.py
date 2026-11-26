"""关键流程自测：掩膜入库、跨片合并/拆开、编号规则、并发与失败隔离。"""

import threading

import pytest

from app import create_app, mask_from_cells


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
