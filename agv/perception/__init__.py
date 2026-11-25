# -*- coding: utf-8 -*-
"""선반 집기·적재 perception: ArUco 마커 검출(직접 구현), 선반 칸 판정, 장면 시뮬레이션.

설계: docs/perception/shelf-perception.md, 기본값: agv/config/perception.json.
로봇(Python 3.6 / OpenCV 4.1, cv2.aruco 없음)에서 돌도록 OpenCV 기본 모듈과 numpy만 쓴다.
"""
