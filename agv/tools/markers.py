# -*- coding: utf-8 -*-
"""선반·물건 ArUco 마커 인쇄 파일(PNG, 가능하면 PDF도)을 만든다. 노트북에서 돌린다.

    python3 -m agv.tools.markers                  # data/markers/ 에 shelves_*.png, items.png(+ markers.pdf)
    python3 -m agv.tools.markers --items-each 6 --out data/markers

- 선반 띠: 선반 하나에 칸 마커 4개를 칸 간격(설정 shelf.slot_width_mm, 70mm)으로 한 띠에. 띠를 통째로 잘라
  선반 앞판에 붙이면 마커 간격이 정확하다. 띠 높이는 앞판 높이(약 35mm)에 맞춤.
- 물건: 종류마다 마커(우유 30, 휴지 31, 계란 32, 주스 33)를 여러 장. 박스 앞면 가운데에 붙인다.
- 크기와 ID는 agv/config/perception.json(markers.size_mm, shelf.shelf_ids, objects)에서 읽는다.
- 마커는 검은 테두리 포함 6x6칸(DICT_4X4_50). 바깥 흰 여백 한 칸 이상은 자르지 않는다.
- **실제 크기(100%)로 인쇄**하고 페이지의 100mm 눈금을 자로 잰다. 다르면 프린터 설정을 고치거나,
  잰 마커 한 변을 markers.size_mm에 적는다.

그림 안 글자는 영어(OpenCV 글꼴). Python 3.6 호환.
"""
from __future__ import division, print_function

import argparse
import os

import cv2
import numpy as np

from ..perception.scene import marker_cells
from ..perception.shelf import ShelfLayout, load_config

DPI = 300.0
A4_LAND = (297.0, 210.0)       # mm (가로, 세로)
A4_PORT = (210.0, 297.0)


def px(mm):
    return int(round(mm * DPI / 25.4))


def draw_marker(page, mid, cx_mm, cy_mm, size_mm):
    """page에 마커 하나(가운데 cx, cy mm)를 그린다. 칸 경계를 픽셀 정수로 맞춘다."""
    cells = marker_cells(mid)
    n = cells.shape[0]
    x0, y0 = px(cx_mm - size_mm / 2.0), px(cy_mm - size_mm / 2.0)
    side = px(size_mm)
    edges = [int(round(i * side / float(n))) for i in range(n + 1)]
    for r in range(n):
        for c in range(n):
            if not cells[r, c]:
                page[y0 + edges[r]:y0 + edges[r + 1], x0 + edges[c]:x0 + edges[c + 1]] = 0


def text(page, s, x_mm, y_mm, scale=1.0, thick=2):
    cv2.putText(page, s, (px(x_mm), px(y_mm)), cv2.FONT_HERSHEY_SIMPLEX, scale * DPI / 300.0 * 1.2, 0,
                int(thick * DPI / 300.0), cv2.LINE_AA)


def ruler(page, x_mm, y_mm, length_mm=100.0):
    """길이 확인용 눈금."""
    y = px(y_mm)
    cv2.line(page, (px(x_mm), y), (px(x_mm + length_mm), y), 0, max(1, px(0.3)))
    for i in range(int(length_mm / 10) + 1):
        xx = px(x_mm + 10 * i)
        cv2.line(page, (xx, y - px(2.5 if i % 5 else 4)), (xx, y), 0, max(1, px(0.3)))
    text(page, "100 mm - measure this with a ruler (print at 100%)", x_mm, y_mm + 6, 0.7, 1)


def dashed_rect(page, x0, y0, x1, y1, dash_mm=3.0):
    """자를 선(점선)."""
    t = max(1, px(0.2))
    for (a, b, horiz, fixed) in ((x0, x1, True, y0), (x0, x1, True, y1), (y0, y1, False, x0), (y0, y1, False, x1)):
        p = a
        while p < b:
            q = min(b, p + dash_mm)
            if horiz:
                cv2.line(page, (px(p), px(fixed)), (px(q), px(fixed)), 128, t)
            else:
                cv2.line(page, (px(fixed), px(p)), (px(fixed), px(q)), 128, t)
            p += 2 * dash_mm


def shelf_pages(layout, size_mm, strip_h_mm=35.0, per_page=3):
    """선반 띠 페이지들(A4 가로)."""
    W, H = A4_LAND
    pages = []
    shelves = sorted(layout.shelves)
    strip_w = layout.n * layout.slot_w
    for p0 in range(0, len(shelves), per_page):
        page = np.full((px(H), px(W)), 255, np.uint8)
        text(page, "Shelf slot markers  (marker %.0f mm, slot pitch %.0f mm, strip height %.0f mm)"
             % (size_mm, layout.slot_w, strip_h_mm), 10, 12, 0.8, 2)
        y = 22.0
        for sid in shelves[p0:p0 + per_page]:
            ids = layout.shelves[sid]
            x0 = (W - strip_w) / 2.0
            dashed_rect(page, x0, y, x0 + strip_w, y + strip_h_mm)
            for k, mid in enumerate(ids):
                # 칸 1은 선반을 마주 본 로봇의 왼쪽 = 띠를 앞에서 볼 때 왼쪽
                cx = x0 + layout.slot_w * (k + 0.5)
                draw_marker(page, mid, cx, y + strip_h_mm / 2.0, size_mm)
            for k, mid in enumerate(ids):
                cx = x0 + layout.slot_w * (k + 0.5)
                text(page, "%s slot%d id%d" % (sid, k + 1, mid), cx - 15, y + strip_h_mm + 5, 0.7, 2)
            y += strip_h_mm + 22.0
        ruler(page, 10, H - 14)
        pages.append(page)
    return pages


def items_pages(objects, size_mm, each=6, cell_mm=40.0):
    """물건 마커 페이지들(A4 세로). 칸 하나 = 박스 앞면 크기(40mm) 점선 사각형. 넘치면 다음 페이지."""
    W, H = A4_PORT
    cols = int((W - 20) // (cell_mm + 6))
    pages = []

    def new_page():
        pg = np.full((px(H), px(W)), 255, np.uint8)
        text(pg, "Item markers (marker %.0f mm, cut on the dashed %.0f mm square)" % (size_mm, cell_mm), 10, 12, 0.7, 2)
        ruler(pg, 10, H - 14)
        pages.append(pg)
        return pg, 22.0
    page, y = new_page()
    for name, mid in objects:
        rows = (each - 1) // cols + 1
        need = 7 + rows * (cell_mm + 6) + 3
        if y + need > H - 24:
            page, y = new_page()
        text(page, "%s  id%d" % (name, mid), 10, y + 4, 0.7, 2)
        y += 7
        for i in range(each):
            c, r = i % cols, i // cols
            x0 = 10 + c * (cell_mm + 6)
            yy = y + r * (cell_mm + 6)
            dashed_rect(page, x0, yy, x0 + cell_mm, yy + cell_mm)
            draw_marker(page, mid, x0 + cell_mm / 2.0, yy + cell_mm / 2.0, size_mm)
        y += rows * (cell_mm + 6) + 3
    return pages


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join("data", "markers"))
    ap.add_argument("--items-each", type=int, default=6, help="물건 종류마다 장 수")
    ap.add_argument("--strip-height", type=float, default=35.0, help="선반 띠 높이(앞판 높이, mm)")
    ap.add_argument("--config", default=None, help="perception.json(기본 agv/config/perception.json)")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    layout = ShelfLayout(cfg)
    size = float(cfg["markers"]["size_mm"])
    objects = sorted(((n, int(o["ids"][0])) for n, o in cfg["objects"].items() if not n.startswith("_")),
                     key=lambda t: t[1])
    if not os.path.isdir(a.out):
        os.makedirs(a.out)
    sp = shelf_pages(layout, size, a.strip_height)
    ip = items_pages(objects, size, a.items_each)
    pages = sp + ip
    names = ["shelves_%d.png" % (i + 1) for i in range(len(sp))] + ["items_%d.png" % (i + 1) for i in range(len(ip))]
    for name, page in zip(names, pages):
        cv2.imwrite(os.path.join(a.out, name), page)
    pdf = None
    try:
        from PIL import Image
        imgs = [Image.fromarray(p).convert("L") for p in pages]
        pdf = os.path.join(a.out, "markers.pdf")
        imgs[0].save(pdf, save_all=True, append_images=imgs[1:], resolution=DPI)
    except ImportError:
        pass
    print(u"마커 인쇄 파일: %s%s" % (", ".join(os.path.join(a.out, n) for n in names),
                                 u"\nPDF: %s" % pdf if pdf else u" (PDF는 Pillow가 있으면 만듦)"))
    print(u"실제 크기(100%%, '페이지 맞춤' 끔)로 인쇄하고 페이지 아래 100mm 눈금을 자로 재세요. 마커 한 변 %.0fmm." % size)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
