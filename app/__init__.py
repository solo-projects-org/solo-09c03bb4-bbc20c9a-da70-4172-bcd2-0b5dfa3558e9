"""芦苇斑块跨图幅调查系统。

按图幅行列上传/替换 16x16 的 0/1 掩膜，跨图幅共边连通，
斑块号 = 该斑块最北一行中最西一格的全图行列号（行、列均从 0 起计）。
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
        conn.commit()
    finally:
        conn.close()


def _read_version(conn):
    return int(conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()["value"])


def _read_sheets(conn, version):
    rows = conn.execute(
        "SELECT sheet_row, sheet_col, mask FROM sheets WHERE version=?", (version,)
    ).fetchall()
    return {(r["sheet_row"], r["sheet_col"]): r["mask"] for r in rows}


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


# ---------------------------------------------------------------- 应用工厂

def create_app(db_path=None):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "reed-survey-dev")
    db = db_path or os.environ.get("APP_DB") or os.path.join(os.getcwd(), "data", "app.db")
    app.config["DB_PATH"] = db
    _init_db(db)

    write_lock = threading.Lock()  # 进程内串行化；跨进程由 BEGIN IMMEDIATE + 版本校验兜底

    def update_state(mutate, retries=30):
        """读当前发布版本 -> 变更 -> 重算 -> 同一事务内原子切换掩膜与成果。

        并发交片按实际入库（提交）顺序生效：版本被抢先则整体重试。
        任何一步失败都回滚，已发布数据不变。
        """
        with write_lock:
            for _ in range(retries):
                conn = _connect(db)
                try:
                    ver = _read_version(conn)
                    sheets = _read_sheets(conn, ver)
                    mutate(sheets)
                    patches = compute_patches(sheets)
                    new_ver = ver + 1
                    conn.execute("BEGIN IMMEDIATE")
                    if _read_version(conn) != ver:
                        conn.rollback()
                        continue  # 期间有新交片入库，基于最新成果重来
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
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
            raise RuntimeError("并发冲突过多，请重试")

    # ------------------------------------------------------------ 视图数据

    def index_context():
        conn = _connect(db)
        try:
            ver = _read_version(conn)
            sheets = _read_sheets(conn, ver)
            sizes = _read_patch_sizes(conn, ver)
            cell_patch = _read_cell_patches(conn, ver)
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

        return dict(
            version=ver,
            sheets=sheet_list,
            patches=patch_list,
            total_reeds=sum(s["reeds"] for s in sheet_list),
            map_rows=map_rows,
            map_too_big=map_too_big,
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

        new_ver, _ = update_state(lambda s: s.__setitem__((sr, sc), mask))
        flash(f"图幅 ({sr},{sc}) 已入库（同位置为替换），发布版本 v{new_ver}", "ok")
        return redirect(url_for("index"))

    # 演示场景：跨片合并 / 拆开
    DEMO_MERGE_A = mask_from_cells([(8, c) for c in range(10, 16)])   # (0,0) 第8行 列10-15
    DEMO_MERGE_B = mask_from_cells([(8, c) for c in range(0, 6)])     # (0,1) 第8行 列0-5，与 A 跨缝相接
    DEMO_SPLIT_B = mask_from_cells([(9, c) for c in range(0, 6)])     # 右片错到第9行，连接断开

    @app.post("/demo/<name>")
    def demo(name):
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
        else:
            abort(404)
        new_ver, _ = update_state(m)
        flash(f"{msg}（版本 v{new_ver}）", "ok")
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
        return render_template("sheet.html", version=ver, sr=sr, sc=sc,
                               grid=grid, summary=summary)

    # ------------------------------------------------------------ JSON API

    @app.get("/api/state")
    def api_state():
        conn = _connect(db)
        try:
            ver = _read_version(conn)
            sheets = _read_sheets(conn, ver)
            sizes = _read_patch_sizes(conn, ver)
        finally:
            conn.close()
        return jsonify(
            version=ver,
            sheets=[{"row": sr, "col": sc, "reed_cells": m.count("1")}
                    for (sr, sc), m in sorted(sheets.items())],
            patches=[{"id": pid, "size": sz} for pid, sz in sorted(
                sizes.items(), key=lambda kv: tuple(map(int, kv[0].split(","))))],
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
        finally:
            conn.close()
        in_sheet = {}
        for pid in cell_patch.values():
            in_sheet[pid] = in_sheet.get(pid, 0) + 1
        return jsonify(
            version=ver,
            sheet_row=sr,
            sheet_col=sc,
            mask=mask,
            cells=[{"row": gr, "col": gc, "patch_id": pid}
                   for (gr, gc), pid in sorted(cell_patch.items())],
            patches=[{"id": pid, "cells_in_sheet": n, "total_size": sizes[pid]}
                     for pid, n in sorted(in_sheet.items(),
                                          key=lambda kv: tuple(map(int, kv[0].split(","))))],
        )

    return app
