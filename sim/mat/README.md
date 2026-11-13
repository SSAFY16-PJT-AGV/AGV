# sim/mat/ — 가상 매트 시뮬레이터

실물 없이 탐색 주행부터 맵 생성까지 시험하는 도구다. 위에서 본 매트 그림에서 로봇 카메라 시점 영상(원근 포함)을 잘라 만들고, 실물과 똑같은 `agv/runner.py`에 넣어 모터 명령을 받는다. 그 명령으로 궤도형 차체를 움직이며, 미끄러짐과 속도 오차도 넣는다.

```
pip install opencv-python numpy        # 노트북
python3 sim/mat/run_sim.py --out data/sim_run --video
python3 mapping/build_map.py data/sim_run/events.jsonl -o data/sim_run/map.json
```

| 옵션 | 결과 |
| --- | --- |
| (기본) | `events.jsonl`·`trace.jsonl`(실물과 같은 형식), `gt.json`(정답 노드 위치·경로 길이·궤적), `mat_topdown.png` |
| `--video` | `run.mp4`: 왼쪽은 검출 결과를 그린 카메라 시점, 오른쪽은 위에서 본 궤적 |
| `--raw-video` | `camera.mp4`: 원본 카메라 영상. `python3 -m agv.explore --camera video:...`로 재생 시험할 때 쓴다 |
| `--seed N` | 노이즈·미끄러짐 난수 |
| `--speed-scale 0.8 --speed-drift 0.05` | 실제 속도를 설정보다 20% 느리게, 1분마다 5%씩 더 느리게(배터리 흉내) |
| `--patch-length 159.3` | 패치 길이로 속도 보정을 켬(`ssafy_v2` 도안 패치의 진행 방향 길이 평균) |
| `--turn-slip 0.3` | 궤도 미끄러짐: 좌우 속도 차이로 계산한 회전의 70%만 돎(실효 궤도 폭 = 140 / 0.7 = 200mm). 맵 모양 계산 시험용 |
| `--mat 파일` | 매트 선택. 기본은 `mats/ssafy_mat.json`(첫 매트 한 장) |
| `--start x,y,각도` | 시작 자세(mm, 도). 특정 구간만 시험할 때 |
| `--right-first` | 갈림길에서 오른쪽부터 탐색 |

`mats/ssafy_mat.json`은 `docs/images/track-mat.jpg` 사진을 눈대중으로 본뜬 매트다. 큰 루프 하나에 분기 두 곳(노랑 패치 위 갈라짐, 보라 패치 위 합쳐짐)이 있다. 실측 치수가 나오면 이 파일을 고친다. 경로는 점을 지나는 부드러운 곡선(Catmull-Rom)으로 그린다.

`mats/ssafy_v2.json`은 매트 두 장을 이은 2m × 1m 도안(2026-11-06)이고, `mats/ssafy_v2_fix.json`은 도안에서 탐색이 막힌 지점을 옮긴 수정안이다. 둘 다 `mats/gen_ssafy_v2.py`로 만들고, 수정 내용은 그 파일 맨 위에 있다. 도안 그대로도 탐색이 끝나고 수정안과 같은 위상(패치 6, 갈라짐 3, 합류 3, 교차 2)이 나온다. S자 선 시작점은 V자 처리로, 빨강 스퍼는 되돌아 나오기로 들어간다. 다만 보라 스퍼가 렌즈 분기에 너무 가까워 가끔 렌즈를 스퍼로 보고 경고가 하나 남는다.

```
python3 sim/mat/run_sim.py --mat sim/mat/mats/ssafy_v2_fix.json --out data/v2f --video
```

카메라 시야는 `run_sim.py`의 `CAM`이다(앞 90~380mm, 폭 180~460mm). 실물 카메라 자세를 정한 뒤 실제 시야로 맞추면 시뮬레이션이 더 실물에 가까워진다.
