# 스마트 마트 물류 자동화 시스템

AGV 주행, 재고 인식, 관제 서버와 디지털 트윈을 함께 개발하는 팀 저장소입니다.

## 구성

[설계 문서 v0.2](docs/design.md)의 아키텍처를 따라 구성요소별로 최상위 폴더를 둡니다. 폴더는 해당 작업을 시작할 때 만듭니다.

| 폴더 | 내용 | 상태 |
| --- | --- | --- |
| `agv/` | JETANK 차체 제어, 라인 주행, 마커 PnP, MQTT 어댑터 | `feature/agv-motor`에서 진행 중 |
| `server/` | 중앙 관제 서버: 작업 배정, 경로, 구역 예약, MQTT 로거 | 예정 |
| `mapping/` | 탐색 기반 매핑: 캘리브레이션, 관측 수집, pose graph 최적화 | 예정 |
| `vision/` | 판매대 고정 카메라 YOLO 재고 감지 | 예정 |
| `sim/` | Python 가상 AGV, 로그 재생기 | 예정 |
| `maps/` | 맵 JSON (샘플, 실측) | 예정 |
| `docs/` | 설계 문서, 로드맵, 보조 문서 | 진행 중 |

- 언리얼 디지털 트윈은 바이너리 에셋(Git LFS) 때문에 별도 레포로 관리합니다. 메시지·맵 JSON 규격은 이 레포의 `docs/design.md`를 기준으로 합니다.
- 녹화 영상, 실측 로그 같은 대용량 데이터는 git에 올리지 않고 팀 드라이브로 공유합니다.

## 문서

- [docs/design.md](docs/design.md): 설계 문서 v0.2 (아키텍처, MQTT 규격, 맵 데이터, 협동 매핑, 충돌 방지)
- [docs/roadmap.md](docs/roadmap.md): 8주 로드맵, GitHub Milestone·Issue 운영 계획
- [CLAUDE.md](CLAUDE.md): Claude Code 작업 시 공유하는 프로젝트 맥락·제약·규칙
