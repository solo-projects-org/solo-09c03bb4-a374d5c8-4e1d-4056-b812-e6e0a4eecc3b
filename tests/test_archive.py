# -*- coding: utf-8 -*-
"""
档案复原（供数档案 + 纸档补证 + 复原下载）自测。

覆盖：
1. 供数 CSV 解析：空文件/空档案拒绝、表头缺列、未对齐、重叠、越界、
   grid_side 不一致、温度区间非法、覆盖格数不符；
2. 补证 CSV 解析：表头跳过、同格同值去重、同格异值报错、非整数拒绝；
3. 补证入库：落块校验、温度区间校验、同格同值幂等、同格异值整份退回、
   合并后块无法达到所报极值整份退回（且分毫不写）；
4. 档案令牌：旧页面向新档写入被拒；
5. 复原下载：四态齐全且温度留空规则正确、唯一推回值等于真值（随机对照）、
   流式快照——下载期间补证成功但本文件不含新证据、重启后可按已收证据复现；
6. 复原不改现行格网：blocks_current 与供数下载全程不变。

运行：PYTHONPATH=. python3 -m unittest tests.test_archive -v
"""

import csv
import io
import os
import random
import tempfile
import unittest

from app import archive, csvutil, db
from app.web import create_app


def make_blocks_csv(n, blocks):
    """blocks: (row1, col1, side, tmax, tmin)，1 基；生成与本工具下载一致的 CSV。"""
    lines = ["block_id,row_start,col_start,side,temp_max_c,temp_min_c,"
             "cell_count,grid_side"]
    for i, (r, c, s, tx, ti) in enumerate(blocks, start=1):
        lines.append(f"{i},{r},{c},{s},{tx},{ti},{s * s},{n}")
    return ("\n".join(lines) + "\n").encode()


def ev_csv(rows, header=True):
    """rows: (row1, col1, temp)；生成补证 CSV 字节。"""
    lines = ["row,col,temp"] if header else []
    lines += [f"{r},{c},{t}" for (r, c, t) in rows]
    return ("\n".join(lines) + "\n").encode()


def restore_rows(text):
    """把复原 CSV 文本解析成 {(row1, col1): (temp_str, status)}。"""
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["row", "col", "temp_c", "status"]
    return {(int(r[0]), int(r[1])): (r[2], r[3]) for r in rows[1:]}


class BlocksParseTests(unittest.TestCase):

    def test_empty_rejected(self):
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(b"")
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(b"   \n")
        # 只有表头：空档案不予导入
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(
                b"block_id,row_start,col_start,side,temp_max_c,temp_min_c,"
                b"cell_count,grid_side\n")

    def test_roundtrip_ok(self):
        raw = make_blocks_csv(4, [(1, 1, 2, 100, 90), (1, 3, 2, 50, 50),
                                  (3, 1, 2, 70, 60), (3, 3, 1, 42, 42)])
        n, blocks = archive.parse_blocks_csv(raw)
        self.assertEqual(n, 4)
        self.assertEqual(len(blocks), 4)
        self.assertIn((0, 0, 2, 100, 90, 4), blocks)
        self.assertIn((2, 2, 1, 42, 42, 1), blocks)

    def test_bad_rows_rejected(self):
        base = [(1, 1, 2, 100, 90)]
        # 缺列表头
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(b"row_start,col_start,side\n1,1,2\n")
        # 未按四叉树对齐
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(make_blocks_csv(4, [(2, 1, 2, 100, 90)]))
        # 块间重叠
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(
                make_blocks_csv(4, [(1, 1, 2, 100, 90), (1, 1, 1, 95, 95)]))
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(
                make_blocks_csv(4, [(1, 1, 4, 100, 90), (3, 3, 2, 95, 90)]))
        # 完全重复
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(make_blocks_csv(4, base + base))
        # 超出网格
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(make_blocks_csv(4, [(3, 3, 4, 100, 90)]))
        # grid_side 不一致
        raw = ("block_id,row_start,col_start,side,temp_max_c,temp_min_c,"
               "cell_count,grid_side\n"
               "1,1,1,2,100,90,4,4\n2,3,3,2,80,70,4,8\n").encode()
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(raw)
        # 温度区间非法（最低高于最高）
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(make_blocks_csv(4, [(1, 1, 2, 90, 100)]))
        # 覆盖格数与边长不符
        raw = ("block_id,row_start,col_start,side,temp_max_c,temp_min_c,"
               "cell_count,grid_side\n1,1,1,2,100,90,3,4\n").encode()
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(raw)
        # 非 2 的幂边长
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(make_blocks_csv(4, [(1, 1, 3, 100, 90)]))


class EvidenceParseTests(unittest.TestCase):

    def test_header_and_dedup(self):
        rows = archive.parse_evidence_csv(ev_csv([(1, 1, 100), (1, 1, 100),
                                                  (2, 3, 55)]))
        self.assertEqual(rows, [(0, 0, 100), (1, 2, 55)])

    def test_no_header_ok(self):
        rows = archive.parse_evidence_csv(ev_csv([(3, 4, 60)], header=False))
        self.assertEqual(rows, [(2, 3, 60)])

    def test_conflict_within_file(self):
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_evidence_csv(ev_csv([(1, 1, 100), (1, 1, 101)]))

    def test_bad_rows(self):
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_evidence_csv(b"")
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_evidence_csv(b"row,col,temp\n")  # 只有表头
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_evidence_csv(b"1,2\n")           # 列数不足
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_evidence_csv(b"1,2,abc\n")       # 非整数
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_evidence_csv(b"0,2,50\n")        # 1 基以下
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_evidence_csv(b"1,2,99999\n")     # 超合理温区


class ArchiveServiceTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "arc.db")
        db.init_db(self.db_path)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _import(self, n=4, blocks=None):
        if blocks is None:
            blocks = [(1, 1, 2, 100, 90), (1, 3, 2, 50, 50),
                      (3, 1, 2, 70, 60), (3, 3, 1, 42, 42),
                      (3, 4, 1, 43, 43), (4, 3, 1, 44, 44)]
        gn, parsed = archive.parse_blocks_csv(make_blocks_csv(n, blocks))
        self.assertEqual(gn, n)
        return archive.import_archive(self.conn, gn, parsed)

    def _restore(self, aid, n):
        self.conn.execute("BEGIN")
        try:
            text = "".join(archive.iter_restore_csv(self.conn, aid, n))
        finally:
            self.conn.execute("ROLLBACK")
        return restore_rows(text)

    def test_import_and_idempotent_evidence(self):
        aid = self._import()
        m = archive.apply_evidence(self.conn, aid, [(0, 0, 100), (0, 1, 95)])
        self.assertEqual((m["inserted"], m["duplicates"]), (2, 0))
        # 同格同值重传：幂等跳过，不重复记
        m = archive.apply_evidence(self.conn, aid, [(0, 0, 100), (0, 1, 95)])
        self.assertEqual((m["inserted"], m["duplicates"]), (0, 2))
        self.assertEqual(archive.evidence_count(self.conn, aid), 2)

    def test_same_cell_different_value_rejected_atomically(self):
        aid = self._import()
        archive.apply_evidence(self.conn, aid, [(0, 0, 100)])
        # 文件里一格冲突 + 一格全新合法：整份退回，新格也不写入
        with self.assertRaises(archive.ArchiveReject):
            archive.apply_evidence(self.conn, aid, [(0, 0, 99), (1, 1, 95)])
        self.assertEqual(archive.evidence_count(self.conn, aid), 1)
        got = self._restore(aid, 4)
        self.assertEqual(got[(1, 1)], ("100", archive.STATUS_PAPER))
        self.assertEqual(got[(2, 2)][1], archive.STATUS_MEASURED_UNKNOWN)

    def test_out_of_block_and_out_of_range_rejected(self):
        aid = self._import()
        # (4,4) 1 基不在任何块内（该格当年未测）
        with self.assertRaises(archive.ArchiveReject):
            archive.apply_evidence(self.conn, aid, [(3, 3, 50)])
        # 块 (1,1,2) 报 [90,100]，温度 85 超范围
        with self.assertRaises(archive.ArchiveReject):
            archive.apply_evidence(self.conn, aid, [(0, 0, 85)])
        # 超出网格
        with self.assertRaises(archive.ArchiveReject):
            archive.apply_evidence(self.conn, aid, [(9, 9, 50)])
        self.assertEqual(archive.evidence_count(self.conn, aid), 0)

    def test_unattainable_extremes_rejected(self):
        # 2x2 单块报 [90,100]：三格都补 95 后，唯一空格无法同时承担最高与最低
        aid = self._import(n=2, blocks=[(1, 1, 2, 100, 90)])
        archive.apply_evidence(self.conn, aid, [(0, 0, 95)])
        archive.apply_evidence(self.conn, aid, [(0, 1, 95)])
        with self.assertRaises(archive.ArchiveReject):
            archive.apply_evidence(self.conn, aid, [(1, 0, 95)])
        self.assertEqual(archive.evidence_count(self.conn, aid), 2)
        # 补 90（最低温由证据承担）则可行
        archive.apply_evidence(self.conn, aid, [(1, 0, 90)])
        # 此时唯一空格被迫承担最高温 100：补 97 仍不可行，补 100 才可行
        with self.assertRaises(archive.ArchiveReject):
            archive.apply_evidence(self.conn, aid, [(1, 1, 97)])
        archive.apply_evidence(self.conn, aid, [(1, 1, 100)])
        self.assertEqual(archive.evidence_count(self.conn, aid), 4)

    def test_unattainable_without_evidence_extremes(self):
        # 一块 2x2 报 [90,100]，证据全不触及极值且只剩一个空格 -> 不可行
        aid = self._import(n=2, blocks=[(1, 1, 2, 100, 90)])
        with self.assertRaises(archive.ArchiveReject):
            archive.apply_evidence(
                self.conn, aid, [(0, 0, 95), (0, 1, 95), (1, 0, 95)])
        self.assertEqual(archive.evidence_count(self.conn, aid), 0)

    def test_stale_token_rejected(self):
        aid1 = self._import()
        aid2 = self._import()   # 整档替换，旧档补证随之清除
        self.assertNotEqual(aid1, aid2)
        with self.assertRaises(archive.ArchiveStale):
            archive.apply_evidence(self.conn, aid1, [(0, 0, 100)])
        m = archive.apply_evidence(self.conn, aid2, [(0, 0, 100)])
        self.assertEqual(m["inserted"], 1)
        self.assertEqual(archive.evidence_count(self.conn, aid2), 1)

    def test_failed_import_keeps_old_archive(self):
        aid = self._import()
        archive.apply_evidence(self.conn, aid, [(0, 0, 100)])
        before = self._restore(aid, 4)
        # 重叠块档案：解析即失败，旧档不动
        with self.assertRaises(csvutil.CSVParseError):
            archive.parse_blocks_csv(
                make_blocks_csv(4, [(1, 1, 4, 100, 90), (3, 3, 2, 95, 90)]))
        cur = archive.current_archive(self.conn)
        self.assertEqual(cur["id"], aid)
        self.assertEqual(self._restore(aid, 4), before)

    def test_restore_statuses(self):
        aid = self._import()
        # 块 (1,1,2) 报 [90,100]：补三格，留 (2,2) 唯一空格，
        # 最高 100 已有证据承担、最低 90 无证据承担 -> (2,2) 唯一推回 90
        archive.apply_evidence(
            self.conn, aid, [(0, 0, 100), (0, 1, 95), (1, 0, 95)])
        got = self._restore(aid, 4)
        self.assertEqual(len(got), 16)
        # 纸档原值
        self.assertEqual(got[(1, 1)], ("100", archive.STATUS_PAPER))
        self.assertEqual(got[(1, 2)], ("95", archive.STATUS_PAPER))
        self.assertEqual(got[(2, 1)], ("95", archive.STATUS_PAPER))
        # 唯一能推回：剩余空格被迫承担最低温
        self.assertEqual(got[(2, 2)], ("90", archive.STATUS_DEDUCED))
        # 块 (1,3,2) 报 [50,50]：区间退化，全块唯一推回
        for pos in [(1, 3), (1, 4), (2, 3), (2, 4)]:
            self.assertEqual(got[pos], ("50", archive.STATUS_DEDUCED))
        # 1x1 块：所报即原值
        self.assertEqual(got[(3, 3)], ("42", archive.STATUS_DEDUCED))
        # 块 (3,1,2) 报 [60,70] 无证据：已测但原值不明，温度留空
        for pos in [(3, 1), (3, 2), (4, 1), (4, 2)]:
            self.assertEqual(got[pos], ("", archive.STATUS_MEASURED_UNKNOWN))
        # (4,4) 不在任何块内：未测，温度留空
        self.assertEqual(got[(4, 4)], ("", archive.STATUS_UNMEASURED))

    def test_deduced_max_when_min_evidenced(self):
        # 最低温有证据、最高温无证据且只剩一个空格 -> 推回最高温
        aid = self._import(n=2, blocks=[(1, 1, 2, 100, 90)])
        archive.apply_evidence(
            self.conn, aid, [(0, 0, 90), (0, 1, 95), (1, 0, 96)])
        got = self._restore(aid, 2)
        self.assertEqual(got[(2, 2)], ("100", archive.STATUS_DEDUCED))

    def test_snapshot_isolation_and_concurrent_evidence(self):
        """下载读事务期间：另一连接补证照常提交；本下载只见开始时快照。"""
        aid = self._import()
        archive.apply_evidence(self.conn, aid, [(0, 0, 100)])

        c2 = db.connect(self.db_path)
        try:
            c2.execute("BEGIN")
            arch = archive.current_archive(c2)   # 快照固定于此
            # 下载进行中，另一连接补证成功（期间仍可补证）
            m = archive.apply_evidence(self.conn, aid, [(0, 1, 95)])
            self.assertEqual(m["inserted"], 1)
            # 本次下载不含新证据
            text = "".join(archive.iter_restore_csv(c2, aid, 4))
            got = restore_rows(text)
            self.assertEqual(got[(1, 1)], ("100", archive.STATUS_PAPER))
            self.assertEqual(got[(1, 2)][1], archive.STATUS_MEASURED_UNKNOWN)
            c2.execute("ROLLBACK")
            # 重新下载则包含
            c2.execute("BEGIN")
            got = restore_rows("".join(archive.iter_restore_csv(c2, aid, 4)))
            c2.execute("ROLLBACK")
            self.assertEqual(got[(1, 2)], ("95", archive.STATUS_PAPER))
        finally:
            c2.close()

    def test_restart_persistence(self):
        """关掉全部连接（模拟重启）后，按已收证据重新下载结果一致。"""
        aid = self._import()
        archive.apply_evidence(self.conn, aid, [(0, 0, 100), (0, 1, 95)])
        before = self._restore(aid, 4)
        self.conn.close()
        self.conn = db.connect(self.db_path)
        arch = archive.current_archive(self.conn)
        self.assertIsNotNone(arch)
        self.assertEqual(self._restore(arch["id"], 4), before)

    def test_grid_untouched_by_archive_ops(self):
        """复原流程不触碰现行格网：物化块与供数下载保持不变。"""
        n = 4
        data = [[10, 11, 50, 51], [12, 13, 52, 53],
                [60, 61, 70, 71], [62, 63, 72, 73]]
        db.create_grid(self.conn, n, data, 5)
        blocks_before = db.load_current_blocks(self.conn)
        csv_before = "".join(csvutil.iter_blocks_csv(blocks_before, n))

        aid = self._import()
        archive.apply_evidence(self.conn, aid, [(0, 0, 100)])
        self._restore(aid, 4)

        blocks_after = db.load_current_blocks(self.conn)
        self.assertEqual([(b.row, b.col, b.size, b.tmax, b.tmin, b.count)
                          for b in blocks_before],
                         [(b.row, b.col, b.size, b.tmax, b.tmin, b.count)
                          for b in blocks_after])
        self.assertEqual("".join(csvutil.iter_blocks_csv(blocks_after, n)),
                         csv_before)

    def test_roundtrip_with_real_grid(self):
        """真实格网 → 供数下载 → 建档 → 真值补证 → 复原下载逐格对照。"""
        rng = random.Random(2024)
        n = 8
        data = [[None if rng.random() < 0.25 else rng.randint(400, 460)
                 for _ in range(n)] for _ in range(n)]
        db.create_grid(self.conn, n, data, 6)
        blocks = db.load_current_blocks(self.conn)
        # 用本工具真实的供数下载格式建档
        csv_text = "".join(csvutil.iter_blocks_csv(blocks, n))
        gn, parsed = archive.parse_blocks_csv(csv_text.encode())
        aid = archive.import_archive(self.conn, gn, parsed)

        # 随机挑约四成实测格，用纸档真值补证（必然合法）
        ev = [(r, c, data[r][c]) for r in range(n) for c in range(n)
              if data[r][c] is not None and rng.random() < 0.4]
        m = archive.apply_evidence(self.conn, aid, ev)
        self.assertEqual(m["inserted"], len(ev))

        got = self._restore(aid, n)
        self.assertEqual(len(got), n * n)
        evset = {(r, c): t for (r, c, t) in ev}
        for r in range(n):
            for c in range(n):
                temp_s, status = got[(r + 1, c + 1)]
                if (r, c) in evset:
                    self.assertEqual(status, archive.STATUS_PAPER)
                    self.assertEqual(int(temp_s), evset[(r, c)])
                elif data[r][c] is None:
                    self.assertEqual(status, archive.STATUS_UNMEASURED)
                    self.assertEqual(temp_s, "")
                elif status == archive.STATUS_DEDUCED:
                    # 唯一推回必等于真值（真值赋值本身就是可行解）
                    self.assertEqual(int(temp_s), data[r][c])
                else:
                    self.assertEqual(status, archive.STATUS_MEASURED_UNKNOWN)
                    self.assertEqual(temp_s, "")


class ArchiveWebTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "web.db")
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _import(self, n=4, blocks=None, tol=None):
        if blocks is None:
            blocks = [(1, 1, 2, 100, 90), (1, 3, 2, 50, 50),
                      (3, 1, 2, 70, 60), (3, 3, 1, 42, 42),
                      (3, 4, 1, 43, 43), (4, 3, 1, 44, 44)]
        return self.client.post(
            "/archive/import",
            data={"csvfile": (io.BytesIO(make_blocks_csv(n, blocks)),
                              "blocks.csv")},
            content_type="multipart/form-data")

    def _evidence(self, rows, archive_id=1, follow=False):
        return self.client.post(
            "/archive/evidence",
            data={"archive_id": str(archive_id),
                  "csvfile": (io.BytesIO(ev_csv(rows)), "ev.csv")},
            content_type="multipart/form-data", follow_redirects=follow)

    def test_full_flow(self):
        # 空档案页
        resp = self.client.get("/archive")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("尚未导入", resp.get_data(as_text=True))
        self.assertEqual(self.client.get("/archive/download").status_code, 404)

        # 无档案时补证被拒且不写库
        resp = self._evidence([(1, 1, 100)], follow=True)
        self.assertIn("尚未导入供数档案", resp.get_data(as_text=True))

        # 建档
        self.assertEqual(self._import().status_code, 302)
        resp = self.client.get("/archive")
        self.assertIn("档案号：<b>#1</b>", resp.get_data(as_text=True))

        # 补证
        resp = self._evidence([(1, 1, 100), (1, 2, 95), (2, 1, 95)],
                              follow=True)
        self.assertIn("补证收录 3 格", resp.get_data(as_text=True))
        # 幂等重传
        resp = self._evidence([(1, 1, 100)], follow=True)
        self.assertIn("幂等跳过 1 格", resp.get_data(as_text=True))

        # 下载复原 CSV
        dl = self.client.get("/archive/download")
        self.assertEqual(dl.status_code, 200)
        self.assertIn("attachment", dl.headers["Content-Disposition"])
        got = restore_rows(dl.get_data(as_text=True))
        self.assertEqual(got[(1, 1)], ("100", "纸档原值"))
        self.assertEqual(got[(2, 2)], ("90", "唯一能推回"))
        self.assertEqual(got[(3, 1)], ("", "已测但原值不明"))
        self.assertEqual(got[(4, 4)], ("", "未测"))

    def test_conflict_and_failed_import_keep_downloadable(self):
        self._import()
        self._evidence([(1, 1, 100)])
        before = self.client.get("/archive/download").get_data(as_text=True)

        # 同格异值：整份退回，页面提示，下载不变
        resp = self._evidence([(1, 1, 99), (2, 2, 95)], follow=True)
        self.assertIn("同格异值", resp.get_data(as_text=True))
        self.assertIn("整份退回", resp.get_data(as_text=True))
        self.assertEqual(
            self.client.get("/archive/download").get_data(as_text=True),
            before)

        # 无法达到所报极值：整份退回
        resp = self._evidence([(1, 2, 95), (2, 1, 95), (2, 2, 95)],
                              follow=True)
        self.assertIn("无法实际达到", resp.get_data(as_text=True))
        self.assertEqual(
            self.client.get("/archive/download").get_data(as_text=True),
            before)

        # 坏供数 CSV 换档失败：旧档仍可下载
        resp = self.client.post(
            "/archive/import",
            data={"csvfile": (io.BytesIO(b"not,a,csv\n1,2,3\n"), "bad.csv")},
            content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("旧档案保持不变", resp.get_data(as_text=True))
        self.assertEqual(
            self.client.get("/archive/download").get_data(as_text=True),
            before)

    def test_stale_page_cannot_write_new_archive(self):
        self._import()
        # 换档：档案 #2 生效
        self._import(blocks=[(1, 1, 2, 100, 90), (1, 3, 2, 50, 50),
                             (3, 1, 2, 70, 60), (3, 3, 2, 40, 40)])
        # 旧页面（令牌 #1）的补证不能写入新档
        resp = self._evidence([(1, 1, 100)], archive_id=1, follow=True)
        self.assertIn("已失效", resp.get_data(as_text=True))
        # 新档补证计数仍为 0
        conn = db.connect(self.db_path)
        try:
            arch = archive.current_archive(conn)
            self.assertEqual(arch["id"], 2)
            self.assertEqual(archive.evidence_count(conn, 2), 0)
        finally:
            conn.close()

    def test_download_snapshot_while_evidence_commits(self):
        """Web 层：下载响应生成期间提交补证，本份文件仍是开始时快照。"""
        self._import()
        self._evidence([(1, 1, 100)])
        resp = self.client.get("/archive/download")   # 视图已执行，流未消费
        self._evidence([(1, 2, 95)])                  # 下载期间补证成功
        got = restore_rows(resp.get_data(as_text=True))
        self.assertEqual(got[(1, 1)], ("100", "纸档原值"))
        self.assertEqual(got[(1, 2)][1], "已测但原值不明")  # 不含新证据
        # 重新下载则包含
        got = restore_rows(
            self.client.get("/archive/download").get_data(as_text=True))
        self.assertEqual(got[(1, 2)], ("95", "纸档原值"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
