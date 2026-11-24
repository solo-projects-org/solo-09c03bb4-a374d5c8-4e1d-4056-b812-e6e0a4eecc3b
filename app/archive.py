# -*- coding: utf-8 -*-
"""
炉壁复验 · 旧测温表档案复原。

电子档只剩供数块（本工具下载的供数 CSV），纸档还能补出部分原值。本模块
实现与现行格网完全独立的“档案复原”：导入供数 CSV 建档、收录纸档补证、
流式下载复原 CSV。只读写 archive / archive_block / evidence 三张表，
绝不触碰 grid / cells / nodes / batch / block_op / blocks_current。

补证入库前校验（任一不满足则整份退回、分毫不写）
--------------------------------------------
1. 坐标须落在某供数块内（块外即当年未测格，纸档不可能有原值）；
2. 温度不得超出该块所报 [temp_min, temp_max]；
3. 与库中已有补证同格异值（同格同值重传幂等跳过，不重复记）；
4. 合并后每块都能“实际达到”所报最高与最低温，即存在一组与证据一致的
   格值赋值，使块内实测最高恰为所报最高、最低恰为所报最低。

复原 CSV 每格四态
----------------
纸档原值        补证直接给出的温度；
唯一能推回      未补证但被约束唯一确定的温度：块区间退化为单值
              （temp_min==temp_max），或块内仅剩一格未补证且所报
              最高/最低温只能由该格承担；
已测但原值不明  格在块内（当年测过）但原值无法唯一确定，温度留空；
未测            格不在任何供数块内，温度留空。
"""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

from . import db
from .csvutil import (CSVParseError, HEADER_TOKENS, MAX_SIDE,
                      TEMP_MAX, TEMP_MIN)
from .quadtree import is_power_of_two

STATUS_PAPER = "纸档原值"
STATUS_DEDUCED = "唯一能推回"
STATUS_MEASURED_UNKNOWN = "已测但原值不明"
STATUS_UNMEASURED = "未测"

# (row0, col0, side, temp_max, temp_min, cell_count)，坐标 0 基
BlockRec = Tuple[int, int, int, int, int, int]
# (row0, col0, temp)，坐标 0 基
EvRec = Tuple[int, int, int]

# 与本工具 /download 下载的供数 CSV 表头一致（block_id 列忽略，按位置重排）
REQUIRED_BLOCK_COLUMNS = ("row_start", "col_start", "side",
                          "temp_max_c", "temp_min_c", "cell_count", "grid_side")


class ArchiveReject(ValueError):
    """档案/补证业务校验失败：调用方保证整份回滚，库中数据保持原样。"""


class ArchiveStale(ArchiveReject):
    """页面令牌与当前档案不一致：旧页面向新档写入被拒绝。"""


# ----------------------------- 解析 --------------------------------

def _decode(raw: bytes) -> str:
    if not raw or not raw.strip():
        raise CSVParseError("文件为空")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            raise CSVParseError("文件编码无法识别，请使用 UTF-8 或 GBK 编码的 CSV")


def parse_blocks_csv(raw: bytes) -> Tuple[int, List[BlockRec]]:
    """
    解析本工具下载的供数 CSV，返回 (grid_side, blocks)。
    任一校验失败抛 CSVParseError，调用方不得写入任何数据。

    校验：非空（至少一块）、表头齐全、grid_side 全文件一致且为 2 的幂、
    块四叉树对齐、块在网格内、温度区间合法、覆盖格数与边长相符、
    块间互不重叠。
    """
    text = _decode(raw)
    rows = [row for row in csv.reader(io.StringIO(text))]
    while rows and all((c is None or c.strip() == "") for c in rows[-1]):
        rows.pop()
    if not rows:
        raise CSVParseError("供数 CSV 为空")

    header = [h.strip().lower() for h in rows[0]]
    idx: Dict[str, int] = {}
    for name in REQUIRED_BLOCK_COLUMNS:
        if name not in header:
            raise CSVParseError(
                f"供数 CSV 表头缺少必要列 “{name}”，"
                "请使用本工具“下载供数 CSV”得到的文件")
        idx[name] = header.index(name)

    data_rows = rows[1:]
    if not data_rows:
        raise CSVParseError("供数 CSV 没有任何块记录，空档案不予导入")

    n: Optional[int] = None
    blocks: List[BlockRec] = []
    seen = set()
    for ln, row in enumerate(data_rows, start=2):
        if len(row) != len(header):
            raise CSVParseError(
                f"第 {ln} 行有 {len(row)} 列，与表头 {len(header)} 列不一致")
        try:
            vals = {name: int(row[i].strip()) for name, i in idx.items()}
        except ValueError:
            raise CSVParseError(f"第 {ln} 行存在非整数字段")
        r1, c1, side = vals["row_start"], vals["col_start"], vals["side"]
        tmax, tmin, cnt = vals["temp_max_c"], vals["temp_min_c"], vals["cell_count"]
        gside = vals["grid_side"]

        if n is None:
            n = gside
            if not is_power_of_two(n) or n > MAX_SIDE:
                raise CSVParseError(
                    f"grid_side {n} 非法：须为 2 的幂且不超过 {MAX_SIDE}")
        elif gside != n:
            raise CSVParseError(
                f"第 {ln} 行 grid_side={gside} 与首行 {n} 不一致")
        if side < 1 or not is_power_of_two(side) or side > n:
            raise CSVParseError(f"第 {ln} 行块边长 {side} 非法")
        r0, c0 = r1 - 1, c1 - 1
        if r0 < 0 or c0 < 0 or r0 + side > n or c0 + side > n:
            raise CSVParseError(
                f"第 {ln} 行块 ({r1},{c1}) 边长 {side} 超出 {n}x{n} 网格")
        if r0 % side or c0 % side:
            raise CSVParseError(
                f"第 {ln} 行块 ({r1},{c1}) 边长 {side} 未按四叉树对齐")
        if not (TEMP_MIN <= tmin <= tmax <= TEMP_MAX):
            raise CSVParseError(
                f"第 {ln} 行温度区间 [{tmin},{tmax}] 非法"
                f"（须在 [{TEMP_MIN},{TEMP_MAX}] 内且最低不高于最高）")
        if cnt != side * side:
            raise CSVParseError(
                f"第 {ln} 行覆盖格数 {cnt} 与边长 {side} 不符（应为 {side * side}）")
        key = (r0, c0, side)
        if key in seen:
            raise CSVParseError(f"第 {ln} 行块 ({r1},{c1}) 边长 {side} 重复出现")
        seen.add(key)
        blocks.append((r0, c0, side, tmax, tmin, cnt))

    # 不重叠校验：位图逐格标记（n<=512，至多 256K 格，一次导入可承受）
    cover = bytearray(n * n)
    for (r0, c0, side, _tx, _ti, _c) in blocks:
        for rr in range(r0, r0 + side):
            base = rr * n
            for cc in range(c0, c0 + side):
                if cover[base + cc]:
                    raise CSVParseError(
                        f"块 ({r0 + 1},{c0 + 1}) 边长 {side} 与其他块重叠")
                cover[base + cc] = 1
    return n, blocks


def parse_evidence_csv(raw: bytes) -> List[EvRec]:
    """
    解析补证 CSV：每行 行,列,温度（行、列从 1 开始，整数摄氏度），
    首行可为表头（首格为 row/行 等关键字时整行跳过）。
    返回按坐标排序的 [(row0, col0, temp)]；文件内同格同值去重，
    同格异值直接报错（整份退回）。
    """
    text = _decode(raw)
    rows = [row for row in csv.reader(io.StringIO(text))]
    while rows and all((c is None or c.strip() == "") for c in rows[-1]):
        rows.pop()
    if not rows:
        raise CSVParseError("补证 CSV 为空")

    if rows[0] and rows[0][0].strip().lower() in HEADER_TOKENS:
        rows = rows[1:]

    out: Dict[Tuple[int, int], int] = {}
    for ln, row in enumerate(rows, start=1):
        if len(row) != 3:
            raise CSVParseError(
                f"第 {ln} 行应有 3 列（行,列,温度），实际 {len(row)} 列")
        try:
            r1 = int(row[0].strip())
            c1 = int(row[1].strip())
            temp = int(row[2].strip())
        except ValueError:
            raise CSVParseError(
                f"第 {ln} 行存在非整数字段：行、列、温度都必须是整数")
        if r1 < 1 or c1 < 1:
            raise CSVParseError(f"第 {ln} 行行、列号从 1 开始，不得小于 1")
        if not (TEMP_MIN <= temp <= TEMP_MAX):
            raise CSVParseError(
                f"第 {ln} 行温度 {temp} 超出合理范围 [{TEMP_MIN},{TEMP_MAX}] ℃")
        key = (r1 - 1, c1 - 1)
        if key in out:
            if out[key] != temp:
                raise CSVParseError(
                    f"第 {ln} 行与前面同格 ({r1},{c1}) 温度不一致，整份退回")
            continue
        out[key] = temp
    if not out:
        raise CSVParseError("补证 CSV 没有有效数据行")
    return sorted((r, c, t) for (r, c), t in out.items())


# ----------------------------- 推断 --------------------------------

def block_attainable(tmin: int, tmax: int, cell_count: int,
                     ev_temps: List[int]) -> bool:
    """
    合并后块内是否存在与证据一致的联合赋值，使实测最高恰为 tmax、
    最低恰为 tmin。证据温度入库前已被限制在 [tmin, tmax] 内。
    """
    u = cell_count - len(ev_temps)   # 未补证格数
    if u < 0:
        return False                 # 防御：证据格数不可能超过块内格数
    if tmin == tmax:
        return True                  # 区间退化为单值，空格取该值即可
    has_max = any(t == tmax for t in ev_temps)
    has_min = any(t == tmin for t in ev_temps)
    if u == 0:
        return has_max and has_min
    if u == 1:
        # 唯一空格只能取一个值：最高、最低至少其一已由证据承担
        return has_max or has_min
    return True                      # 两个以上空格可分别承担最高与最低


@dataclass
class _ActiveBlock:
    """下载扫行时的在册块及其证据统计。"""
    row0: int
    col0: int
    side: int
    tmax: int
    tmin: int
    u: int          # 未补证格数
    has_max: bool   # 证据中已出现所报最高温
    has_min: bool   # 证据中已出现所报最低温


# ----------------------------- 服务 --------------------------------

def current_archive(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """当前唯一档案（全表至多一行，取 id 最大者兜底）。"""
    return conn.execute(
        "SELECT * FROM archive ORDER BY id DESC LIMIT 1").fetchone()


def evidence_count(conn: sqlite3.Connection, archive_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE archive_id=?",
        (archive_id,)).fetchone()[0]


def import_archive(conn: sqlite3.Connection, n: int,
                   blocks: List[BlockRec]) -> int:
    """
    整档替换：旧档案、旧块、旧补证在同一事务内清除，新档生效。
    失败回滚，旧档案与既有补证原样保留，仍可下载。返回新档案 id。
    """
    with db.transaction(conn):
        conn.execute("DELETE FROM evidence")
        conn.execute("DELETE FROM archive_block")
        conn.execute("DELETE FROM archive")
        cur = conn.execute(
            "INSERT INTO archive(grid_side,block_count,created_at) VALUES(?,?,?)",
            (n, len(blocks), db.utc_now()))
        aid = cur.lastrowid
        conn.executemany(
            "INSERT INTO archive_block(archive_id,row_idx,col_idx,side,"
            "temp_max,temp_min,cell_count) VALUES(?,?,?,?,?,?,?)",
            [(aid, r, c, s, tx, ti, cnt)
             for (r, c, s, tx, ti, cnt) in blocks])
        db.log_event(conn, "archive_import",
                     f"导入供数档案 #{aid}：{n}x{n}，{len(blocks)} 块，"
                     "旧档及其补证已整档替换")
    return aid


def _find_block(bidx: Dict[Tuple[int, int, int], Tuple[int, int, int]],
                n: int, r: int, c: int):
    """按四叉树对齐坐标查找覆盖 (r,c) 的块；块间不重叠，至多命中一个。"""
    s = 1
    while s <= n:
        key = ((r // s) * s, (c // s) * s, s)
        if key in bidx:
            return key, bidx[key]
        s *= 2
    return None


def apply_evidence(conn: sqlite3.Connection, archive_id: int,
                   rows: List[EvRec]) -> Dict[str, int]:
    """
    合并一份补证（已解析、文件内已去重）。任一校验失败抛
    ArchiveReject / ArchiveStale，事务整体回滚，库中证据保持原样。
    同格同值重传幂等跳过。返回 {inserted, duplicates, blocks_checked}。
    """
    with db.transaction(conn):
        arch = current_archive(conn)
        if arch is None:
            raise ArchiveReject("尚未导入供数档案，请先导入再补证")
        if arch["id"] != archive_id:
            raise ArchiveStale(
                f"页面档案 #{archive_id} 已失效（当前档案 #{arch['id']}），"
                "请刷新档案复原页后重试")
        aid, n = arch["id"], arch["grid_side"]

        blocks = conn.execute(
            "SELECT row_idx,col_idx,side,temp_max,temp_min,cell_count "
            "FROM archive_block WHERE archive_id=?", (aid,)).fetchall()
        bidx = {(b["row_idx"], b["col_idx"], b["side"]):
                (b["temp_max"], b["temp_min"], b["cell_count"]) for b in blocks}

        # 1) 坐标落块 + 温度不超该块所报范围
        located: List[Tuple[int, int, int, Tuple[int, int, int]]] = []
        for (r, c, t) in rows:
            if r >= n or c >= n:
                raise ArchiveReject(f"坐标 ({r + 1},{c + 1}) 超出 {n}x{n} 网格")
            hit = _find_block(bidx, n, r, c)
            if hit is None:
                raise ArchiveReject(
                    f"坐标 ({r + 1},{c + 1}) 不在任何供数块内"
                    "（该格当年未测，纸档不可能有原值）")
            bk, (tmax, tmin, _cnt) = hit
            if not (tmin <= t <= tmax):
                raise ArchiveReject(
                    f"坐标 ({r + 1},{c + 1}) 温度 {t} 超出所在块所报范围 "
                    f"[{tmin},{tmax}] ℃")
            located.append((r, c, t, bk))

        # 2) 与库中证据比对：同格异值整份退回；同格同值幂等跳过
        conn.execute("CREATE TEMP TABLE t_ev(r INT, c INT)")
        try:
            conn.executemany("INSERT INTO t_ev VALUES(?,?)",
                             [(r, c) for (r, c, _t, _b) in located])
            old = {(row["row_idx"], row["col_idx"]): row["temp"]
                   for row in conn.execute(
                       "SELECT e.row_idx, e.col_idx, e.temp FROM evidence e "
                       "JOIN t_ev w ON e.row_idx=w.r AND e.col_idx=w.c "
                       "WHERE e.archive_id=?", (aid,))}
        finally:
            conn.execute("DROP TABLE t_ev")

        new_rows: List[Tuple[int, int, int, Tuple[int, int, int]]] = []
        dup = 0
        for (r, c, t, bk) in located:
            if (r, c) in old:
                if old[(r, c)] != t:
                    raise ArchiveReject(
                        f"坐标 ({r + 1},{c + 1}) 已有补证温度 {old[(r, c)]} ℃，"
                        f"本次提交 {t} ℃，同格异值，整份退回")
                dup += 1
            else:
                new_rows.append((r, c, t, bk))

        # 3) 合并后每块仍能实际达到所报最高/最低温（只核查本次触及的块；
        #    库中原状态可行，未触及的块不受影响）
        new_by_block: Dict[Tuple[int, int, int], List[int]] = {}
        for (_r, _c, t, bk) in new_rows:
            new_by_block.setdefault(bk, []).append(t)
        for bk, temps in new_by_block.items():
            tmax, tmin, cnt = bidx[bk]
            ev_temps = list(temps)
            for row in conn.execute(
                    "SELECT temp FROM evidence WHERE archive_id=? "
                    "AND blk_row=? AND blk_col=? AND blk_side=?",
                    (aid, bk[0], bk[1], bk[2])):
                ev_temps.append(row["temp"])
            if not block_attainable(tmin, tmax, cnt, ev_temps):
                raise ArchiveReject(
                    f"块 ({bk[0] + 1},{bk[1] + 1}) 边长 {bk[2]} 合并本份补证后，"
                    f"无法实际达到所报最高 {tmax} / 最低 {tmin} ℃，整份退回")

        # 4) 入库
        now = db.utc_now()
        conn.executemany(
            "INSERT INTO evidence(archive_id,row_idx,col_idx,temp,"
            "blk_row,blk_col,blk_side,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [(aid, r, c, t, bk[0], bk[1], bk[2], now)
             for (r, c, t, bk) in new_rows])
        db.log_event(
            conn, "evidence",
            f"档案 #{aid} 收录补证 {len(new_rows)} 格"
            + (f"，同格同值幂等跳过 {dup} 格" if dup else ""))
        return {"inserted": len(new_rows), "duplicates": dup,
                "blocks_checked": len(new_by_block)}


# --------------------------- 复原下载 -------------------------------

def _fetchone(cur: sqlite3.Cursor) -> Optional[sqlite3.Row]:
    return cur.fetchone()


def iter_restore_csv(conn: sqlite3.Connection, archive_id: int,
                     n: int) -> Iterator[str]:
    """
    流式生成复原 CSV：逐网格行产出，内存只与“跨行在册块数 + 单行证据”
    成正比，不铺开整张方阵。

    调用方须保证 conn 处于读事务中：本函数的多路游标（块、分块证据统计、
    逐格证据）共享同一快照，内容固定为下载开始时已入库的档案与读数；
    WAL 模式下下载期间其他连接仍可补证。
    """
    buf = io.StringIO()
    csv.writer(buf).writerow(["row", "col", "temp_c", "status"])
    yield buf.getvalue()

    blocks_cur = conn.execute(
        "SELECT row_idx,col_idx,side,temp_max,temp_min,cell_count "
        "FROM archive_block WHERE archive_id=? ORDER BY row_idx, col_idx",
        (archive_id,))
    agg_cur = conn.execute(
        "SELECT blk_row, blk_col, COUNT(*) AS ev_cnt, "
        "MAX(temp) AS ev_max, MIN(temp) AS ev_min "
        "FROM evidence WHERE archive_id=? "
        "GROUP BY blk_row, blk_col, blk_side ORDER BY blk_row, blk_col",
        (archive_id,))
    ev_cur = conn.execute(
        "SELECT row_idx, col_idx, temp FROM evidence WHERE archive_id=? "
        "ORDER BY row_idx, col_idx", (archive_id,))

    next_block = _fetchone(blocks_cur)
    next_agg = _fetchone(agg_cur)
    next_ev = _fetchone(ev_cur)

    active: List[_ActiveBlock] = []
    for r in range(n):
        # 剔除不再覆盖本行的块
        if active:
            active = [b for b in active if b.row0 + b.side > r]
        # 激活本行开始的块，并并入该块的证据统计（两路游标同序归并）
        while next_block is not None and next_block["row_idx"] == r:
            b = next_block
            u = b["cell_count"]
            has_max = has_min = False
            if (next_agg is not None
                    and next_agg["blk_row"] == b["row_idx"]
                    and next_agg["blk_col"] == b["col_idx"]):
                u = b["cell_count"] - next_agg["ev_cnt"]
                has_max = (next_agg["ev_max"] == b["temp_max"])
                has_min = (next_agg["ev_min"] == b["temp_min"])
                next_agg = _fetchone(agg_cur)
            active.append(_ActiveBlock(b["row_idx"], b["col_idx"], b["side"],
                                       b["temp_max"], b["temp_min"],
                                       u, has_max, has_min))
            next_block = _fetchone(blocks_cur)
        # 本行的逐格证据
        ev_row: Dict[int, int] = {}
        while next_ev is not None and next_ev["row_idx"] == r:
            ev_row[next_ev["col_idx"]] = next_ev["temp"]
            next_ev = _fetchone(ev_cur)

        active.sort(key=lambda b: b.col0)

        buf = io.StringIO()
        w = csv.writer(buf)
        bi = 0                      # 块指针只右移：块按列排序且互不重叠
        nb = len(active)
        for c in range(n):
            t = ev_row.get(c)
            if t is not None:
                w.writerow([r + 1, c + 1, t, STATUS_PAPER])
                continue
            while bi < nb and active[bi].col0 + active[bi].side <= c:
                bi += 1
            if bi < nb and active[bi].col0 <= c:
                b = active[bi]
                if b.tmin == b.tmax:
                    # 区间退化为单值：全块唯一确定
                    w.writerow([r + 1, c + 1, b.tmin, STATUS_DEDUCED])
                elif b.u == 1 and not b.has_max:
                    # 仅剩本格未补证，所报最高温只能由它承担
                    w.writerow([r + 1, c + 1, b.tmax, STATUS_DEDUCED])
                elif b.u == 1 and not b.has_min:
                    # 仅剩本格未补证，所报最低温只能由它承担
                    w.writerow([r + 1, c + 1, b.tmin, STATUS_DEDUCED])
                else:
                    w.writerow([r + 1, c + 1, "", STATUS_MEASURED_UNKNOWN])
            else:
                w.writerow([r + 1, c + 1, "", STATUS_UNMEASURED])
        yield buf.getvalue()
