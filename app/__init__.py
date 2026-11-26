"""芦苇斑块跨图幅调查系统。

按图幅行列上传/替换 16x16 的 0/1 掩膜，跨图幅共边连通，
斑块号 = 该斑块最北一行中最西一格的全图行列号（行、列均从 0 起计）。

样方核查：调查员按全图行、列登记现场判定的 0/1，样方表整体替换、独立编版。
对照现行图幅把样方格分为 一致 / 不符 / 缺测（缺测 = 该格所在图幅尚未交付）。
每次发布掩膜时，按实际入库时生效的样方表与上一发布版逐格判断：原本一致的
格若变为不符或缺测，列出这些格的坐标并拒绝发布，原掩膜与斑块成果照常可查。
原本不符或缺测的格可分次修正；逐格判定，修好一格不能抵消另一格的退步。
"""

import os
import sqlite3
import threading
import zlib

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, url_for)

SHEET = 16          # 每片图幅 16x16
MAX_SHEET_POS = 999 # 图幅行列号上限（含）
MAP_RENDER_LIMIT = 128  # 全图预览最多渲染的边长（格）
MAX_COORD = (MAX_SHEET_POS + 1) * SHEET - 1  # 全图行列号上限（含）：15999
MAX_SAMPLE_ROWS = 200000  # 样方表行数上限
CMP_DISPLAY_LIMIT = 200   # 页面对照清单每类最多展示的格数

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sheets(
  version   INTEGER NOT NULL,
  sheet_row INTEGER NOT NULL,
  sheet_col INTEGER NOT NULL,
  mask      TEXT NOT NULL,           -- 256 个 '0'/'1'，行优先
  PRIMARY KEY(version, sheet_row, sheet_col)
);
CREATE TABLE IF NOT EXISTS patches(
  version  INTEGER NOT NULL,
  patch_id TEXT NOT NULL,            -- "行,列"（全图坐标，0 起）
  size     INTEGER NOT NULL,         -- 全图总格数
  PRIMARY KEY(version, patch_id)
);
CREATE TABLE IF NOT EXISTS cell_patches(
  version  INTEGER NOT NULL,
  grow     INTEGER NOT NULL,         -- 全图行
  gcol     INTEGER NOT NULL,         -- 全图列
  patch_id TEXT NOT NULL,
  PRIMARY KEY(version, grow, gcol)
);
CREATE TABLE IF NOT EXISTS samples(
  version  INTEGER NOT NULL,         -- 样方表版（独立于地图版编号）
  grow     INTEGER NOT NULL,         -- 全图行
  gcol     INTEGER NOT NULL,         -- 全图列
  value    INTEGER NOT NULL,         -- 现场判定 0/1
  PRIMARY KEY(version, grow, gcol)
);
"""


# ---------------------------------------------------------------- 工具

def mask_from_cells(cells):
    """cells: [(r, c), ...]（片内坐标）-> 256 字符掩膜串。"""
    grid = [[0] * SHEET for _ in range(SHEET)]
    for r, c in cells:
        grid[r][c] = 1
    return "".join("".join(map(str, row)) for row in grid)


def parse_mask(text):
    """解析 16 行 x 16 列的 0/1 CSV，返回 256 字符掩膜串；非法则 ValueError。"""
    if text is None:
        raise ValueError("未提供掩膜内容")
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [ln for ln in lines if ln]
    if len(lines) != SHEET:
        raise ValueError(f"掩膜需要 {SHEET} 行，实际 {len(lines)} 行")
    bits = []
    for i, ln in enumerate(lines, 1):
        parts = [p.strip() for p in ln.split(",")]
        # 兼容无逗号的写法（连续 16 个 0/1 字符）
        if len(parts) == 1 and len(parts[0]) == SHEET and set(parts[0]) <= {"0", "1"}:
            parts = list(parts[0])
        if len(parts) != SHEET:
            raise ValueError(f"第 {i} 行需要 {SHEET} 列，实际 {len(parts)} 列")
        for p in parts:
            if p not in ("0", "1"):
                raise ValueError(f"第 {i} 行含非法值 {p!r}（只允许 0/1）")
        bits.extend(parts)
    return "".join(bits)


# ---------------------------------------------------------------- 样方表解析

def _looks_like_header(line):
    """首行若是「三列但坐标列不是整数」视为表头（如 行,列,值 / row,col,value）。"""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != 3:
        return False
    try:
        int(parts[0])
        int(parts[1])
    except ValueError:
        return True
    return False


def _parse_sample_row(line):
    """解析一行「全图行,全图列,值」-> (grow, gcol, value)；非法则 ValueError。"""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != 3:
        raise ValueError(f"样方行需为「全图行,全图列,值」三列：{line!r}")
    try:
        gr, gc = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"样方坐标需为整数：{line!r}")
    if parts[2] not in ("0", "1"):
        raise ValueError(f"样方值只允许 0/1：{line!r}")
    return gr, gc, int(parts[2])


def parse_samples(text):
    """解析样方 CSV：每行 全图行,全图列,0/1；首行可为表头。

    重复坐标、越界坐标、非法值、空表均整表拒收（ValueError），调用方沿用原表。
    返回 {(grow, gcol): value}。
    """
    if text is None:
        raise ValueError("未提供样方内容")
    text = text.lstrip("\ufeff")  # 去掉可能残留的 BOM
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        raise ValueError("样方表为空")
    if _looks_like_header(lines[0]):
        lines = lines[1:]
        if not lines:
            raise ValueError("样方表只有表头，没有数据行")
    entries = {}
    for ln in lines:
        gr, gc, val = _parse_sample_row(ln)
        if not (0 <= gr <= MAX_COORD and 0 <= gc <= MAX_COORD):
            raise ValueError(f"样方坐标 ({gr},{gc}) 超出全图范围 0–{MAX_COORD}")
        if (gr, gc) in entries:
            raise ValueError(f"重复坐标 ({gr},{gc})：同一格只能登记一次")
        entries[(gr, gc)] = val
    if len(entries) > MAX_SAMPLE_ROWS:
        raise ValueError(f"样方行数 {len(entries)} 超过上限 {MAX_SAMPLE_ROWS}")
    return entries


# ---------------------------------------------------------------- 对照与守护

def compare_with_map(sheets, samples):
    """对照图幅掩膜与样方表，把样方格分为 一致 / 不符 / 缺测 三类。

    缺测 = 该样方格所在图幅尚未交付，现行图幅无从核对。
    返回 {"consistent": [(gr,gc)...], "mismatched": [...], "missing": [...]}（各自按坐标排序）。
    """
    consistent, mismatched, missing = [], [], []
    for (gr, gc), val in sorted(samples.items()):
        mask = sheets.get((gr // SHEET, gc // SHEET))
        if mask is None:
            missing.append((gr, gc))
        elif int(mask[(gr % SHEET) * SHEET + (gc % SHEET)]) == val:
            consistent.append((gr, gc))
        else:
            mismatched.append((gr, gc))
    return {"consistent": consistent, "mismatched": mismatched, "missing": missing}


def guard_regressions(old_sheets, new_sheets, samples):
    """原本一致的格在新版图幅中变为不符或缺测 -> 回归格坐标清单（已排序）。

    逐格判定：只看出退步的格，修好其他格不能抵消任何一格的退步。
    """
    before = set(compare_with_map(old_sheets, samples)["consistent"])
    if not before:
        return []
    after = compare_with_map(new_sheets, samples)
    bad = set(after["mismatched"]) | set(after["missing"])
    return sorted(before & bad)


class SampleGuardError(Exception):
    """换片会把已与现场对上的格改错：携带回归格坐标与对照用的样方表版。"""

    def __init__(self, regressions, sample_version):
        self.regressions = regressions          # [(grow, gcol), ...] 已排序
        self.sample_version = sample_version
        shown = "、".join(f"({r},{c})" for r, c in regressions[:20])
        more = f" 等 {len(regressions)} 格" if len(regressions) > 20 else ""
        super().__init__(
            f"{len(regressions)} 个原本与现场一致的格将变为不符或缺测：{shown}{more}")


def compute_patches(sheets):
    """sheets: {(sr, sc): mask} -> patches: {patch_id: {"row","col","size","cells"}}。

    全图坐标 grow = sr*16 + r，gcol = sc*16 + c；共边（四邻）相连，跨片缝同理。
    斑块号 = 最北一行中最西一格的全图行列号。
    """
    parent = {}

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    cells = set()
    for (sr, sc), mask in sheets.items():
        base_r, base_c = sr * SHEET, sc * SHEET
        for idx, ch in enumerate(mask):
            if ch == "1":
                cells.add((base_r + idx // SHEET, base_c + idx % SHEET))
    for cell in cells:
        parent[cell] = cell
    for (gr, gc) in cells:
        for nb in ((gr + 1, gc), (gr, gc + 1)):
            if nb in cells:
                union((gr, gc), nb)

    comps = {}
    for cell in cells:
        comps.setdefault(find(cell), []).append(cell)

    patches = {}
    for members in comps.values():
        north = min(r for r, _ in members)
        west = min(c for r, c in members if r == north)
        pid = f"{north},{west}"
        patches[pid] = {"row": north, "col": west, "size": len(members), "cells": members}
    return patches


def patch_color(pid):
    """按斑块号稳定取色。"""
    hue = zlib.crc32(pid.encode()) % 360
    return f"hsl({hue}, 72%, 72%)"


# ---------------------------------------------------------------- 数据库

def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _init_db(db_path):
    d = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(d, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('version', '0')")
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('sample_version', '0')")
        conn.commit()
    finally:
        conn.close()


def _read_version(conn):
    return int(conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()["value"])


def _read_sample_version(conn):
    return int(conn.execute("SELECT value FROM meta WHERE key='sample_version'").fetchone()["value"])


def _read_sheets(conn, version):
    rows = conn.execute(
        "SELECT sheet_row, sheet_col, mask FROM sheets WHERE version=?", (version,)
    ).fetchall()
    return {(r["sheet_row"], r["sheet_col"]): r["mask"] for r in rows}


def _read_samples(conn, version):
    rows = conn.execute(
        "SELECT grow, gcol, value FROM samples WHERE version=?", (version,)
    ).fetchall()
    return {(r["grow"], r["gcol"]): r["value"] for r in rows}


def _read_patch_sizes(conn, version):
    rows = conn.execute("SELECT patch_id, size FROM patches WHERE version=?", (version,)).fetchall()
    return {r["patch_id"]: r["size"] for r in rows}


def _read_cell_patches(conn, version, r0=None, r1=None, c0=None, c1=None):
    sql = "SELECT grow, gcol, patch_id FROM cell_patches WHERE version=?"
    args = [version]
    if r0 is not None:
        sql += " AND grow>=? AND grow<? AND gcol>=? AND gcol<?"
        args += [r0, r1, c0, c1]
    rows = conn.execute(sql, args).fetchall()
    return {(r["grow"], r["gcol"]): r["patch_id"] for r in rows}


def _is_locked(exc):
    return "locked" in str(exc).lower()


# ---------------------------------------------------------------- 应用工厂

def create_app(db_path=None):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "reed-survey-dev")
    db = db_path or os.environ.get("APP_DB") or os.path.join(os.getcwd(), "data", "app.db")
    app.config["DB_PATH"] = db
    _init_db(db)

    write_lock = threading.Lock()    # 交片：进程内串行化；跨进程由 BEGIN IMMEDIATE + 版本校验兜底
    sample_lock = threading.Lock()   # 换样方表：与交片互不阻塞，重算斑块期间照常可换

    def update_state(mutate, retries=30):
        """读当前发布版本 -> 变更 -> 重算 -> 同一事务内原子切换掩膜与成果。

        并发交片按实际入库（提交）顺序生效：版本被抢先则整体重试。
        提交前以实际入库时生效的样方表校验：原本一致的格若在新版变为不符或
        缺测，整片退回（SampleGuardError），已发布数据不变。
        任何一步失败都回滚，已发布数据不变。
        """
        with write_lock:
            for _ in range(retries):
                conn = _connect(db)
                try:
                    ver = _read_version(conn)
                    old_sheets = _read_sheets(conn, ver)
                    sheets = dict(old_sheets)
                    mutate(sheets)
                    patches = compute_patches(sheets)
                    new_ver = ver + 1
                    conn.execute("BEGIN IMMEDIATE")
                    if _read_version(conn) != ver:
                        conn.rollback()
                        continue  # 期间有新交片入库，基于最新成果重来
                    # 样方守护：按入库时生效的样方表 + 上一发布版逐格判断
                    sver = _read_sample_version(conn)
                    if sver:
                        samples = _read_samples(conn, sver)
                        regress = guard_regressions(old_sheets, sheets, samples)
                        if regress:
                            conn.rollback()
                            raise SampleGuardError(regress, sver)
                    conn.executemany(
                        "INSERT INTO sheets(version, sheet_row, sheet_col, mask) VALUES (?,?,?,?)",
                        [(new_ver, sr, sc, m) for (sr, sc), m in sorted(sheets.items())],
                    )
                    conn.executemany(
                        "INSERT INTO patches(version, patch_id, size) VALUES (?,?,?)",
                        [(new_ver, pid, p["size"]) for pid, p in sorted(patches.items())],
                    )
                    conn.executemany(
                        "INSERT INTO cell_patches(version, grow, gcol, patch_id) VALUES (?,?,?,?)",
                        [(new_ver, gr, gc, pid)
                         for pid, p in patches.items() for (gr, gc) in p["cells"]],
                    )
                    conn.execute("UPDATE meta SET value=? WHERE key='version'", (str(new_ver),))
                    conn.commit()
                    return new_ver, patches
                except sqlite3.OperationalError as e:
                    conn.rollback()
                    if _is_locked(e):
                        continue  # 与换表等写事务相撞，busy_timeout 之后再试
                    raise
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
            raise RuntimeError("并发冲突过多，请重试")

    def replace_sample_table(entries, retries=10):
        """整表替换样方表：校验已在写库前完成，此处单事务换版。

        不占用交片写锁，重算斑块期间照常可换；任何失败都回滚，原表继续生效。
        """
        with sample_lock:
            for _ in range(retries):
                conn = _connect(db)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    new_sver = _read_sample_version(conn) + 1
                    conn.executemany(
                        "INSERT INTO samples(version, grow, gcol, value) VALUES (?,?,?,?)",
                        [(new_sver, gr, gc, v) for (gr, gc), v in sorted(entries.items())],
                    )
                    conn.execute("UPDATE meta SET value=? WHERE key='sample_version'",
                                 (str(new_sver),))
                    conn.commit()
                    return new_sver
                except sqlite3.OperationalError as e:
                    conn.rollback()
                    if _is_locked(e):
                        continue  # 与交片提交相撞，稍后重试
                    raise
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
            raise RuntimeError("换表冲突过多，请重试")

    # ------------------------------------------------------------ 视图数据

    def index_context():
        conn = _connect(db)
        try:
            ver = _read_version(conn)
            sheets = _read_sheets(conn, ver)
            sizes = _read_patch_sizes(conn, ver)
            cell_patch = _read_cell_patches(conn, ver)
            sver = _read_sample_version(conn)
            samples = _read_samples(conn, sver) if sver else {}
        finally:
            conn.close()

        sheet_list = []
        for (sr, sc), mask in sorted(sheets.items()):
            pids = {pid for (gr, gc), pid in cell_patch.items()
                    if sr * SHEET <= gr < (sr + 1) * SHEET and sc * SHEET <= gc < (sc + 1) * SHEET}
            sheet_list.append({
                "row": sr, "col": sc,
                "reeds": mask.count("1"),
                "patch_count": len(pids),
            })

        patch_list = sorted(
            ({"id": pid, "size": sz, "color": patch_color(pid)} for pid, sz in sizes.items()),
            key=lambda p: tuple(map(int, p["id"].split(","))),
        )

        map_rows = None
        map_too_big = False
        if sheets:
            srs = [s[0] for s in sheets]
            scs = [s[1] for s in sheets]
            r0, r1 = min(srs) * SHEET, (max(srs) + 1) * SHEET
            c0, c1 = min(scs) * SHEET, (max(scs) + 1) * SHEET
            if (r1 - r0) > MAP_RENDER_LIMIT or (c1 - c0) > MAP_RENDER_LIMIT:
                map_too_big = True
            else:
                map_rows = []
                for gr in range(r0, r1):
                    row = []
                    for gc in range(c0, c1):
                        present = (gr // SHEET, gc // SHEET) in sheets
                        pid = cell_patch.get((gr, gc))
                        style = []
                        if gc % SHEET == 0:
                            style.append("border-left:2px solid #555")
                        if gr % SHEET == 0:
                            style.append("border-top:2px solid #555")
                        if pid:
                            style.append(f"background:{patch_color(pid)}")
                            cls = "reed"
                            title = f"全图({gr},{gc}) 斑块({pid}) 共{sizes[pid]}格"
                        elif present:
                            cls = "water"
                            title = f"全图({gr},{gc})"
                        else:
                            cls = "gap"
                            title = "无图幅"
                        row.append({"cls": cls, "style": ";".join(style), "title": title})
                    map_rows.append(row)

        comparison = cmp_counts = None
        cmp_truncated = False
        if samples:
            cmp = compare_with_map(sheets, samples)
            cmp_counts = {k: len(v) for k, v in cmp.items()}
            comparison = {
                k: [f"({r},{c})" for r, c in cells[:CMP_DISPLAY_LIMIT]]
                for k, cells in cmp.items()
            }
            cmp_truncated = any(len(v) > CMP_DISPLAY_LIMIT for v in cmp.values())

        return dict(
            version=ver,
            sheets=sheet_list,
            patches=patch_list,
            total_reeds=sum(s["reeds"] for s in sheet_list),
            map_rows=map_rows,
            map_too_big=map_too_big,
            sample_version=sver,
            sample_count=len(samples),
            comparison=comparison,
            cmp_counts=cmp_counts,
            cmp_truncated=cmp_truncated,
        )

    # ------------------------------------------------------------ 页面

    @app.get("/")
    def index():
        return render_template("index.html", **index_context())

    @app.post("/upload")
    def upload():
        f = request.files.get("file")
        if f is not None and f.filename:
            text = f.read().decode("utf-8-sig", errors="replace")
        else:
            text = request.form.get("text", "")
        try:
            sr = int(request.form.get("sheet_row", ""))
            sc = int(request.form.get("sheet_col", ""))
            if not (0 <= sr <= MAX_SHEET_POS and 0 <= sc <= MAX_SHEET_POS):
                raise ValueError(f"图幅行列需在 0–{MAX_SHEET_POS} 之间")
            mask = parse_mask(text)
        except ValueError as e:
            # 校验失败：不写库，已发布数据原样保留
            flash(f"交片被拒绝：{e}", "error")
            return render_template("index.html", **index_context()), 400

        try:
            new_ver, _ = update_state(lambda s: s.__setitem__((sr, sc), mask))
        except SampleGuardError as e:
            # 换片会把已与现场对上的格改错：整片退回，已发布数据不变
            flash(f"换片被退回：{e}（对照生效样方表 sv{e.sample_version}）。"
                  "原掩膜与斑块成果照常可查。", "error")
            return render_template("index.html", **index_context()), 409
        flash(f"图幅 ({sr},{sc}) 已入库（同位置为替换），发布版本 v{new_ver}", "ok")
        return redirect(url_for("index"))

    @app.post("/samples")
    def samples_upload():
        f = request.files.get("file")
        if f is not None and f.filename:
            text = f.read().decode("utf-8-sig", errors="replace")
        else:
            text = request.form.get("text", "")
        try:
            entries = parse_samples(text)
        except ValueError as e:
            # 换表失败：不写库，原样方表继续生效
            flash(f"换表被拒绝（沿用原表）：{e}", "error")
            return render_template("index.html", **index_context()), 400
        new_sver = replace_sample_table(entries)
        flash(f"样方表 sv{new_sver} 已生效（{len(entries)} 格），原表已替换", "ok")
        return redirect(url_for("index"))

    # 演示场景：跨片合并 / 拆开
    DEMO_MERGE_A = mask_from_cells([(8, c) for c in range(10, 16)])   # (0,0) 第8行 列10-15
    DEMO_MERGE_B = mask_from_cells([(8, c) for c in range(0, 6)])     # (0,1) 第8行 列0-5，与 A 跨缝相接
    DEMO_SPLIT_B = mask_from_cells([(9, c) for c in range(0, 6)])     # 右片错到第9行，连接断开

    # 演示场景：样方核查与分次修正（按 ④→⑧ 顺序点击）
    DEMO_CHECK_BASE_A = mask_from_cells([(8, c) for c in range(10, 16)])
    DEMO_CHECK_BASE_B = mask_from_cells([(8, c) for c in range(0, 6)])
    DEMO_CHECK_FIX1_A = mask_from_cells([(8, c) for c in range(10, 16)] + [(4, 4)])
    DEMO_CHECK_FIX2_A = mask_from_cells([(8, c) for c in range(10, 16)] + [(4, 4), (4, 5)])
    DEMO_CHECK_COVER = mask_from_cells([(0, 0)])                      # 补交 (1,0)，(20,3) 保持 0
    DEMO_CHECK_BAD_A = mask_from_cells([(8, c) for c in range(11, 16)] + [(4, 4), (4, 5)])  # 丢掉 (8,10)
    DEMO_CHECK_SAMPLES = {  # 一致 3：(8,10)(8,12)(8,18)；不符 2：(4,4)(4,5)；缺测 1：(20,3)
        (8, 10): 1, (8, 12): 1, (8, 18): 1, (4, 4): 1, (4, 5): 1, (20, 3): 0,
    }

    @app.post("/demo/<name>")
    def demo(name):
        try:
            if name == "merge":
                def m(s):
                    s[(0, 0)] = DEMO_MERGE_A
                    s[(0, 1)] = DEMO_MERGE_B
                msg = "合并示例：两片条带跨缝相接 → 应合并为 1 个斑块（12 格）"
            elif name == "split":
                def m(s):
                    s[(0, 1)] = DEMO_SPLIT_B
                msg = "拆开示例：右片条带错开一行 → 应拆成 2 个斑块（各 6 格）"
            elif name == "clear":
                def m(s):
                    s.clear()
                msg = "已清空全部图幅"
            elif name == "check_fix1":
                def m(s):
                    s[(0, 0)] = DEMO_CHECK_FIX1_A
                msg = "第一批修正：图幅 (0,0) 补上 (4,4) → 不符格 2→1"
            elif name == "check_fix2":
                def m(s):
                    s[(0, 0)] = DEMO_CHECK_FIX2_A
                msg = "第二批修正：图幅 (0,0) 补上 (4,5) → 不符格清零"
            elif name == "check_cover":
                def m(s):
                    s[(1, 0)] = DEMO_CHECK_COVER
                msg = "补交图幅 (1,0)：缺测格 (20,3) 转为一致"
            elif name == "check_bad":
                def m(s):
                    s[(0, 0)] = DEMO_CHECK_BAD_A
                msg = "（演示）换片已入库：当前样方表未守护 (8,10)，请先 ④ 载入核查示例再看本步被退回"
            else:
                m = None

            if m is not None:
                new_ver, _ = update_state(m)
                flash(f"{msg}（版本 v{new_ver}）", "ok")
            elif name == "check_load":
                # 先换片（按现行样方表校验），再整表换入核查样方表
                new_ver, _ = update_state(
                    lambda s: s.update({(0, 0): DEMO_CHECK_BASE_A, (0, 1): DEMO_CHECK_BASE_B}))
                sver = replace_sample_table(dict(DEMO_CHECK_SAMPLES))
                flash(f"核查示例已载入：图幅 (0,0)/(0,1) + 样方表 sv{sver}"
                      f"（6 格：一致 3 · 不符 2 · 缺测 1）（地图版 v{new_ver}）", "ok")
            elif name == "check_reset":
                # 样方表换代：新表生效后按新表守护，旧表约束解除，可重新演示
                sver = replace_sample_table({(0, 0): 1})
                new_ver, _ = update_state(lambda s: s.clear())
                flash(f"演示重置：样方表换代为 sv{sver}（旧表守护随之解除），"
                      f"图幅已清空（版本 v{new_ver}），可重新从 ④ 开始", "ok")
            else:
                abort(404)
        except SampleGuardError as e:
            # 演示「退回换片」：原本一致的格被改错，发布被拒并列出坐标
            flash(f"换片被退回：{e}（对照生效样方表 sv{e.sample_version}）。"
                  "原掩膜与斑块成果照常可查。", "error")
            return render_template("index.html", **index_context()), 409
        return redirect(url_for("index"))

    @app.get("/sheet/<int:sr>/<int:sc>")
    def sheet_view(sr, sc):
        conn = _connect(db)
        try:
            ver = _read_version(conn)
            sheets = _read_sheets(conn, ver)
            mask = sheets.get((sr, sc))
            if mask is None:
                abort(404)
            sizes = _read_patch_sizes(conn, ver)
            cell_patch = _read_cell_patches(
                conn, ver, sr * SHEET, (sr + 1) * SHEET, sc * SHEET, (sc + 1) * SHEET)
            sver = _read_sample_version(conn)
            samples = _read_samples(conn, sver) if sver else {}
        finally:
            conn.close()

        grid = []
        for lr in range(SHEET):
            row = []
            for lc in range(SHEET):
                pid = cell_patch.get((sr * SHEET + lr, sc * SHEET + lc))
                row.append({
                    "bit": mask[lr * SHEET + lc],
                    "pid": pid,
                    "color": patch_color(pid) if pid else None,
                    "size": sizes.get(pid) if pid else None,
                    "grow": sr * SHEET + lr,
                    "gcol": sc * SHEET + lc,
                })
            grid.append(row)

        in_sheet = {}
        for (gr, gc), pid in cell_patch.items():
            in_sheet[pid] = in_sheet.get(pid, 0) + 1
        summary = sorted(
            ({"id": pid, "cells_here": n, "total": sizes[pid], "color": patch_color(pid)}
             for pid, n in in_sheet.items()),
            key=lambda p: tuple(map(int, p["id"].split(","))),
        )

        samples_here = []
        for (gr, gc), val in sorted(samples.items()):
            if sr * SHEET <= gr < (sr + 1) * SHEET and sc * SHEET <= gc < (sc + 1) * SHEET:
                bit = int(mask[(gr % SHEET) * SHEET + (gc % SHEET)])
                samples_here.append({
                    "grow": gr, "gcol": gc, "field": val, "map": bit,
                    "status": "一致" if bit == val else "不符",
                })
        return render_template("sheet.html", version=ver, sr=sr, sc=sc,
                               grid=grid, summary=summary,
                               sample_version=sver, samples_here=samples_here)

    # ------------------------------------------------------------ JSON API

    @app.get("/api/state")
    def api_state():
        conn = _connect(db)
        try:
            ver = _read_version(conn)
            sheets = _read_sheets(conn, ver)
            sizes = _read_patch_sizes(conn, ver)
            sver = _read_sample_version(conn)
            samples = _read_samples(conn, sver) if sver else {}
        finally:
            conn.close()
        cmp_counts = None
        if samples:
            cmp = compare_with_map(sheets, samples)
            cmp_counts = {k: len(v) for k, v in cmp.items()}
        return jsonify(
            version=ver,
            sample_version=sver,
            sheets=[{"row": sr, "col": sc, "reed_cells": m.count("1")}
                    for (sr, sc), m in sorted(sheets.items())],
            patches=[{"id": pid, "size": sz} for pid, sz in sorted(
                sizes.items(), key=lambda kv: tuple(map(int, kv[0].split(","))))],
            comparison=cmp_counts,
        )

    @app.get("/api/sheet/<int:sr>/<int:sc>")
    def api_sheet(sr, sc):
        conn = _connect(db)
        try:
            ver = _read_version(conn)
            sheets = _read_sheets(conn, ver)
            mask = sheets.get((sr, sc))
            if mask is None:
                abort(404)
            sizes = _read_patch_sizes(conn, ver)
            cell_patch = _read_cell_patches(
                conn, ver, sr * SHEET, (sr + 1) * SHEET, sc * SHEET, (sc + 1) * SHEET)
            sver = _read_sample_version(conn)
            samples = _read_samples(conn, sver) if sver else {}
        finally:
            conn.close()
        in_sheet = {}
        for pid in cell_patch.values():
            in_sheet[pid] = in_sheet.get(pid, 0) + 1
        samples_here = []
        for (gr, gc), val in sorted(samples.items()):
            if sr * SHEET <= gr < (sr + 1) * SHEET and sc * SHEET <= gc < (sc + 1) * SHEET:
                bit = int(mask[(gr % SHEET) * SHEET + (gc % SHEET)])
                samples_here.append({
                    "row": gr, "col": gc, "field": val, "map": bit,
                    "status": "一致" if bit == val else "不符",
                })
        return jsonify(
            version=ver,
            sample_version=sver,
            sheet_row=sr,
            sheet_col=sc,
            mask=mask,
            cells=[{"row": gr, "col": gc, "patch_id": pid}
                   for (gr, gc), pid in sorted(cell_patch.items())],
            patches=[{"id": pid, "cells_in_sheet": n, "total_size": sizes[pid]}
                     for pid, n in sorted(in_sheet.items(),
                                          key=lambda kv: tuple(map(int, kv[0].split(","))))],
            samples=samples_here,
        )

    @app.get("/api/samples")
    def api_samples():
        conn = _connect(db)
        try:
            sver = _read_sample_version(conn)
            samples = _read_samples(conn, sver) if sver else {}
        finally:
            conn.close()
        return jsonify(
            sample_version=sver,
            entries=[{"row": gr, "col": gc, "value": v}
                     for (gr, gc), v in sorted(samples.items())],
        )

    @app.get("/api/comparison")
    def api_comparison():
        conn = _connect(db)
        try:
            ver = _read_version(conn)
            sheets = _read_sheets(conn, ver)
            sver = _read_sample_version(conn)
            samples = _read_samples(conn, sver) if sver else {}
        finally:
            conn.close()
        cmp = compare_with_map(sheets, samples)
        return jsonify(
            map_version=ver,
            sample_version=sver,
            consistent=[{"row": r, "col": c} for r, c in cmp["consistent"]],
            mismatched=[{"row": r, "col": c} for r, c in cmp["mismatched"]],
            missing=[{"row": r, "col": c} for r, c in cmp["missing"]],
        )

    return app
