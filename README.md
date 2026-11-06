# 스마트 마트 물류 자동화 시스템

AGV 주행, 재고 인식, 관제 서버와 디지털 트윈을 함께 개발하는 팀 저장소입니다.

## 구성

- agv/: JETANK 차체 제어 및 주행 실험 (라인 추종, 색 패치·갈림길 검출, 탐색 주행) — [agv/README.md](agv/README.md)
- mapping/: 이벤트 로그 → 위상 맵 JSON, 디버그 뷰어 — [mapping/README.md](mapping/README.md)
- sim/mat/: 가상 매트 카메라 시뮬레이터 (실물 없이 탐색·맵 생성 시험) — [sim/mat/README.md](sim/mat/README.md)
- maps/: 맵 JSON (`sim_ssafy_mat.json`은 시뮬레이션 예시)

비전, 서버, 시뮬레이션 및 디지털 트윈의 폴더 구조는 각 담당자와 협의하여 추가합니다.
